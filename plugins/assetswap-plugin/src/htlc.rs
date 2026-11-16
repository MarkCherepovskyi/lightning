//! The swap-provider's `htlc_accepted` hook: decode what a candidate leg-1
//! HTLC's `payment_metadata` is asking for, price it live, atomically
//! claim it, run leg 2, and resolve or fail leg 1 -- plus the
//! restart-replay path for an HTLC we already committed to before a prior
//! process exited. There's no prior quote to look up: the provider decides
//! live, on arrival, whether this HTLC is profitable at its current price
//! (see pay.rs's doc comment for why).
//!
//! The one rule everything here keeps: leg 1 is never failed while leg 2
//! may still succeed (that would pay the payee for nothing). Once leg 2
//! may have been sent (record "paying", see `protocol::QuoteRecord`), leg
//! 1 is settled only by `settle`, from what the node paying leg 2 says of
//! it, holding leg 1 for as long as that takes, past leg 1's own expiry if
//! need be: leg 2 bounded by its `maxdelay` (`cltv::leg2_cltv_gate`) only
//! makes it *expire* first; its payee can still settle it until our timeout
//! of it confirms. The hook runs in a task of its own per HTLC
//! (cln-plugin spawns one per request), so holding one HTLC doesn't hold
//! up any other.
//!
//! **Operational note**: replay only works if the plugin is loaded before
//! lightningd replays the pending HTLC -- from persistent config (`plugin=`
//! in its `config` file), not `plugin start` (this plugin isn't dynamic).

use crate::cltv::{block_seconds, giveup_margin, leg2_cltv_gate};
use crate::protocol::{
    check_asset, decode_swap_metadata, fetch_price_tiers, is_asset_id, leg2_maxfee, quote_key, QuoteRecord,
    RECORD_VERSION,
};
use crate::rpc::{asset_layers, channel_asset, getinfo, leg2_path, leg2_rpc, new_rpc, rpc_path};
use crate::State;
use anyhow::{anyhow, Error};
use cln_plugin::Plugin;
use cln_rpc::model::requests::{
    DatastoreMode, DatastoreRequest, DecodeRequest, ListdatastoreRequest, ListsendpaysRequest, XpayRequest,
};
use cln_rpc::model::responses::ListsendpaysPaymentsStatus;
use cln_rpc::primitives::{Amount, Secret, Sha256};
use cln_rpc::ClnRpc;
use serde_json::{json, Value};
use std::str::FromStr;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Polling leg 2's status: first after this, then twice as long each time
/// up to `POLL_MAX`.
const POLL_MIN: Duration = Duration::from_secs(2);
const POLL_MAX: Duration = Duration::from_secs(30);
/// At most one log line per held HTLC this often.
const LOG_EVERY: Duration = Duration::from_secs(60);

/// How long xpay keeps starting new attempts (passed as its `retry_for`).
const XPAY_RETRY_FOR: u32 = 60;
/// After an `xpay` call that didn't return a verdict (e.g. the connection
/// to the remote node dropped), xpay may still be routing: until this long
/// after "paying", leg 2 having no live part isn't taken as failed.
const XPAY_GRACE_SECS: u64 = 2 * XPAY_RETRY_FOR as u64;

/// xpay's errors for a payment it gave up on (and so stopped sending):
/// invalid parameters, and PAY_DESTINATION_PERM_FAIL, PAY_ROUTE_NOT_FOUND,
/// PAY_ROUTE_TOO_EXPENSIVE, PAY_INVOICE_EXPIRED, PAY_UNSPECIFIED_ERROR
/// (its "failed after N attempts"), PAY_STOPPED_RETRYING,
/// PAY_INVOICE_REQUEST_INVALID, PAY_INSUFFICIENT_FUNDS, PAY_UNREACHABLE,
/// PAY_DESTINATION_INSUFFICIENT_CAPACITY. Even then, leg 1 is only failed
/// once `listsendpays` shows no part pending or complete: xpay may return
/// before every part it sent has settled.
const XPAY_GAVE_UP: [i32; 11] = [-32602, 203, 205, 206, 207, 209, 210, 212, 215, 216, 220];

/// incorrect_or_unknown_payment_details (BOLT4 0x400f), with a zeroed
/// htlc_msat+height tail — real nodes accept a bogus tail here (see
/// tests/plugins/endlesswaitblockheight.py in this tree) and this failure
/// deliberately doesn't tell the payer *which* check failed.
fn fail_incorrect_details() -> Value {
    json!({"result": "fail", "failure_message": format!("400f{}", "0".repeat(24))})
}

fn parse_msat(v: &Value) -> Option<u64> {
    match v {
        Value::Number(n) => n.as_u64(),
        Value::String(s) => s.strip_suffix("msat").unwrap_or(s).parse().ok(),
        _ => None,
    }
}

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn secret_hex(s: Secret) -> Option<String> {
    match serde_json::to_value(s) {
        Ok(Value::String(hex)) => Some(hex),
        _ => None,
    }
}

fn resolve_with_hex(preimage: Option<&str>) -> Value {
    match preimage {
        Some(hex) if hex.len() == 64 && hex.bytes().all(|b| b.is_ascii_hexdigit()) => {
            json!({"result": "resolve", "payment_key": hex})
        }
        _ => fail_incorrect_details(),
    }
}

/// The quote record for `payment_hash` and its datastore generation.
async fn lookup_quote(rpc: &mut ClnRpc, payment_hash: &str) -> Result<Option<(QuoteRecord, Option<u64>)>, Error> {
    let res = rpc
        .call_typed(&ListdatastoreRequest {
            key: Some(quote_key(payment_hash)),
        })
        .await?;
    match res.datastore.into_iter().next() {
        Some(d) => {
            let s = d.string.ok_or_else(|| anyhow!("quote record has no string value"))?;
            Ok(Some((serde_json::from_str(&s)?, d.generation)))
        }
        None => Ok(None),
    }
}

/// Write `rec` with the datastore's CAS: create it (`prev` None), or
/// replace the one at generation `prev`. Returns the new generation.
async fn write_record(
    rpc: &mut ClnRpc,
    payment_hash: &str,
    rec: &QuoteRecord,
    prev: Option<Option<u64>>,
) -> Result<u64, Error> {
    let (mode, generation) = match prev {
        None => (DatastoreMode::MUST_CREATE, None),
        Some(g) => (DatastoreMode::MUST_REPLACE, g),
    };
    let res = rpc
        .call_typed(&DatastoreRequest {
            generation,
            hex: None,
            mode: Some(mode),
            string: Some(serde_json::to_string(rec)?),
            key: quote_key(payment_hash),
        })
        .await?;
    Ok(res.generation)
}

/// Move the record to "complete" (with the preimage) or "failed",
/// re-reading it so a concurrent writer isn't overwritten blindly.
/// "complete" is final: nothing moves a record out of it.
async fn finalize(p: &Plugin<State>, payment_hash: &str, state: &str, preimage: Option<String>) {
    for _ in 0..5 {
        let res = async {
            let mut rpc = new_rpc(p).await?;
            let Some((cur, generation)) = lookup_quote(&mut rpc, payment_hash).await? else {
                return Err(anyhow!("no record"));
            };
            if cur.state == "complete" || cur.state == state {
                return Ok(());
            }
            let rec = QuoteRecord {
                state: state.into(),
                preimage: preimage.clone().or(cur.preimage.clone()),
                ..cur
            };
            write_record(&mut rpc, payment_hash, &rec, Some(generation)).await.map(|_| ())
        }
        .await;
        match res {
            Ok(()) => return,
            Err(e) => log::debug!("assetswap: marking {payment_hash} {state}: {e}, retrying"),
        }
    }
    log::warn!("assetswap: couldn't mark quote {payment_hash} as {state}");
}

/// What a node's `listsendpays` says of leg 2.
#[derive(Debug, PartialEq)]
enum Leg2<T> {
    /// A part completed: here is the preimage.
    Complete(T),
    /// Every part (`parts` of them, maybe none) failed for good.
    Failed { parts: usize },
    /// A part is still in flight.
    Pending,
    /// The node couldn't be asked, or answered oddly.
    Unknown(String),
    /// The node configured for the asset isn't the one that paid leg 2.
    WrongNode(String),
}

fn classify<T>(parts: impl IntoIterator<Item = (ListsendpaysPaymentsStatus, Option<T>)>) -> Leg2<T> {
    let (mut n, mut pending, mut odd) = (0, false, false);
    for (status, preimage) in parts {
        n += 1;
        match (status, preimage) {
            (ListsendpaysPaymentsStatus::COMPLETE, Some(pre)) => return Leg2::Complete(pre),
            (ListsendpaysPaymentsStatus::COMPLETE, None) => odd = true,
            (ListsendpaysPaymentsStatus::PENDING, _) => pending = true,
            (ListsendpaysPaymentsStatus::FAILED, _) => {}
        }
    }
    if odd {
        Leg2::Unknown("a complete part without a preimage".into())
    } else if pending {
        Leg2::Pending
    } else {
        Leg2::Failed { parts: n }
    }
}

/// Ask the node that pays leg 2 for `rec`'s asset what became of it.
async fn leg2_status(p: &Plugin<State>, payment_hash: &str, rec: &QuoteRecord) -> Leg2<Secret> {
    let mut rpc2 = match leg2_rpc(p, &rec.to_asset).await {
        Ok(r) => r,
        Err(e) => return Leg2::Unknown(e.to_string()),
    };
    if let Some(want) = &rec.leg2_node {
        match getinfo(&mut rpc2).await {
            Ok(info) if info["id"].as_str() == Some(want.as_str()) => {}
            Ok(info) => {
                return Leg2::WrongNode(format!(
                    "leg 2 was paid by node {want}, but the node now configured for {} is {}: \
                     restore its assetswap-remote to settle leg 1",
                    rec.to_asset, info["id"]
                ))
            }
            Err(e) => return Leg2::Unknown(e.to_string()),
        }
    }
    let Ok(hash) = Sha256::from_str(payment_hash) else {
        return Leg2::Unknown(format!("bad payment_hash {payment_hash}"));
    };
    match rpc2
        .call_typed(&ListsendpaysRequest {
            bolt11: None,
            index: None,
            limit: None,
            payment_hash: Some(hash),
            start: None,
            status: None,
        })
        .await
    {
        Ok(res) => classify(res.payments.into_iter().map(|pm| (pm.status, pm.payment_preimage))),
        Err(e) => Leg2::Unknown(format!("listsendpays: {e}")),
    }
}

/// Settle leg 1 (expiring at block `cltv_expiry`) from leg 2's outcome,
/// for a record that may have reached `xpay`: resolve it with leg 2's
/// preimage once a part completes; fail it once every part has failed
/// (and xpay can't send another: `xpay_done`, or `XPAY_GRACE_SECS` after
/// "paying"); otherwise hold it and ask again, whatever the error.
///
/// Never on a deadline: a leg 2 that is still pending can still be settled
/// by the payee, off-chain or on-chain, even after its CLTV, until our
/// timeout of it confirms. Failing leg 1 then would pay the payee for
/// nothing (invariant S1). This is what any forwarding node does with an
/// HTLC whose outgoing side is unresolved: past leg 1's expiry the payer
/// may close the channel to time leg 1 out on chain, and if leg 2 does
/// complete, the preimage still claims it there. From `giveup_margin`
/// blocks before leg 1 expires the wait is only logged as an error.
async fn settle(p: &Plugin<State>, payment_hash: &str, cltv_expiry: u32, rec: &QuoteRecord, xpay_done: bool) -> Value {
    let late_height = cltv_expiry.saturating_sub(giveup_margin(p.state().config.safety_blocks));
    let mut delay = POLL_MIN;
    let mut logged: Option<Instant> = None;
    let mut logged_error = false;
    loop {
        let (why, error) = match leg2_status(p, payment_hash, rec).await {
            Leg2::Complete(pre) => {
                let hex = secret_hex(pre);
                finalize(p, payment_hash, "complete", hex.clone()).await;
                return resolve_with_hex(hex.as_deref());
            }
            Leg2::Failed { parts } => {
                let xpay_may_send = !xpay_done && rec.paying_since.is_some_and(|t| now_unix() < t + XPAY_GRACE_SECS);
                if !xpay_may_send {
                    log::info!("assetswap: leg 2 for {payment_hash} failed ({parts} parts): failing leg 1");
                    finalize(p, payment_hash, "failed", None).await;
                    return fail_incorrect_details();
                }
                (format!("leg 2 has no live part ({parts} failed), xpay may still send"), false)
            }
            Leg2::Pending => ("leg 2 is pending".to_string(), false),
            Leg2::Unknown(e) => (format!("leg 2's status is unknown: {e}"), false),
            Leg2::WrongNode(e) => (e, true),
        };

        let height = async { getinfo(&mut new_rpc(p).await?).await }
            .await
            .and_then(|i| i["blockheight"].as_u64().ok_or_else(|| anyhow!("getinfo has no blockheight")));
        let (why, error) = match height {
            Ok(h) if h >= u64::from(late_height) => (
                format!(
                    "{why} at block {h}, leg 1 expires at {cltv_expiry}: still holding it, as leg 2 may \
                     still be settled (the payer may close the channel to time leg 1 out on chain)"
                ),
                true,
            ),
            Ok(h) => (format!("{why} (block {h}, leg 1 expires at {cltv_expiry})"), error),
            Err(e) => (format!("{why} (block height unknown: {e})"), error),
        };

        // Once a minute, and at once when it turns into an error.
        if logged.is_none_or(|t| t.elapsed() >= LOG_EVERY) || (error && !logged_error) {
            logged_error = error;
            if error {
                log::error!("assetswap: holding leg 1 of {payment_hash}: {why}");
            } else {
                log::warn!("assetswap: holding leg 1 of {payment_hash}: {why}");
            }
            logged = Some(Instant::now());
        }
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(POLL_MAX);
    }
}

/// Marks `payment_hash` as handled by a live hook call of this process
/// while it lives, so a "claimed" record is told apart from one a prior
/// run left behind.
struct Inflight<'a> {
    p: &'a Plugin<State>,
    hash: String,
}

impl Drop for Inflight<'_> {
    fn drop(&mut self) {
        self.p.state().inflight.lock().unwrap_or_else(|e| e.into_inner()).remove(&self.hash);
    }
}

fn is_inflight(p: &Plugin<State>, hash: &str) -> bool {
    p.state().inflight.lock().unwrap_or_else(|e| e.into_inner()).contains(hash)
}

pub(crate) async fn on_htlc_accepted(p: Plugin<State>, v: Value) -> Result<Value, Error> {
    // Only a final hop (no onion.short_channel_id, nor a blinded path's
    // next_node_id) can be a swap settlement: never second-guess an HTLC
    // we're meant to forward on.
    let onion = v.get("onion");
    let is_final = ["short_channel_id", "next_node_id"]
        .iter()
        .all(|k| onion.and_then(|o| o.get(*k)).is_none());
    if !is_final {
        return Ok(json!({"result": "continue"}));
    }

    let htlc = v.get("htlc").cloned().unwrap_or(Value::Null);
    let payment_hash = htlc
        .get("payment_hash")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let amount_msat = htlc.get("amount_msat").and_then(parse_msat).unwrap_or(0);
    let cltv_expiry_relative = htlc
        .get("cltv_expiry_relative")
        .and_then(Value::as_u64)
        .unwrap_or(0) as u32;
    let scid = htlc
        .get("short_channel_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();

    if payment_hash.is_empty() {
        return Ok(json!({"result": "continue"}));
    }

    let mut rpc = new_rpc(&p).await?;

    // Leg 1's absolute expiry, which bounds how long we may hold it.
    let cltv_expiry = match htlc.get("cltv_expiry").and_then(Value::as_u64) {
        Some(c) => c as u32,
        None => {
            let h = getinfo(&mut rpc).await?["blockheight"].as_u64().unwrap_or(0) as u32;
            h + cltv_expiry_relative
        }
    };

    // What we already know of this hash. A record on the way to leg 2, or
    // past it, is settled from leg 2's own status (restart replay, or
    // another HTLC with the same hash), never decided afresh.
    // Fail closed: if we can't read the record, it may say leg 2 is being
    // paid, and a `continue` would have lightningd fail leg 1 (no invoice).
    // An error here makes lightningd stop instead, like our other RPC errors.
    let existing = lookup_quote(&mut rpc, &payment_hash)
        .await
        .map_err(|e| anyhow!("datastore lookup failed for {payment_hash}: {e}"))?;
    if let Some((rec, generation)) = &existing {
        match rec.state.as_str() {
            "complete" => return Ok(resolve_with_hex(rec.preimage.as_deref())),
            "paying" => return Ok(settle(&p, &payment_hash, cltv_expiry, rec, false).await),
            // From before "paying" existed: leg 2 may have been sent, on
            // the node configured for the asset (there's no other).
            "claimed" if rec.v < RECORD_VERSION => {
                return Ok(settle(&p, &payment_hash, cltv_expiry, rec, false).await)
            }
            "claimed" => {
                // Not ours now: a prior run claimed it and stopped before
                // leg 2 (or "paying" would say so). Nothing was sent.
                if !is_inflight(&p, &payment_hash) {
                    log::info!("assetswap: {payment_hash} was claimed but leg 2 never attempted: failing leg 1");
                    let failed = QuoteRecord {
                        state: "failed".into(),
                        ..rec.clone()
                    };
                    if let Err(e) = write_record(&mut rpc, &payment_hash, &failed, Some(*generation)).await {
                        log::warn!("assetswap: couldn't mark quote {payment_hash} as failed: {e}");
                    }
                }
                return Ok(fail_incorrect_details());
            }
            "failed" => {} // judged afresh below, after a leg-2 check
            other => {
                log::warn!("assetswap: quote {payment_hash} in unknown state {other:?}");
                return Ok(fail_incorrect_details());
            }
        }
    }

    // No prior negotiation: the payer packs what we need directly into
    // leg 1's payment_metadata. No metadata (or garbage) means this isn't
    // a swap HTLC at all -- not ours to second-guess.
    let onion = v.get("onion").cloned().unwrap_or(Value::Null);
    let Some(metadata_hex) = onion.get("payment_metadata").and_then(Value::as_str) else {
        return Ok(json!({"result": "continue"}));
    };
    let Ok(meta) = decode_swap_metadata(metadata_hex) else {
        return Ok(json!({"result": "continue"}));
    };
    // A new swap: only for a provider that lightningd won't outlive.
    if !p.state().provider {
        log::info!("assetswap: leg 1 {payment_hash} for a swap, but we're not an important plugin: not taking it");
        return Ok(fail_incorrect_details());
    }
    if check_asset(&meta.to_asset).is_err() {
        return Ok(fail_incorrect_details());
    }

    // Leg 2 is paid by this node, or, for an asset on another chain, by
    // the node of ours `assetswap-remote` names (its invoice is for that
    // chain's network, its CLTVs in that chain's blocks).
    let mut rpc2 = match leg2_rpc(&p, &meta.to_asset).await {
        Ok(r) => r,
        Err(e) => {
            log::warn!("assetswap: {e}");
            return Ok(fail_incorrect_details());
        }
    };
    let Ok(decoded) = rpc2
        .call_typed(&DecodeRequest {
            string: meta.final_bolt11.clone(),
        })
        .await
    else {
        return Ok(fail_incorrect_details());
    };
    let payee = decoded.payee.map(|k| k.to_string());
    let (Some(to_amount_msat), Some(final_payment_hash), Some(leg2_final_cltv)) = (
        decoded.amount_msat.map(|a| a.msat()),
        decoded.payment_hash.map(|h| h.to_string()),
        decoded.min_final_cltv_expiry,
    ) else {
        return Ok(fail_incorrect_details());
    };
    // The hash-link the whole swap's atomicity rests on: leg 1 must carry
    // the SAME payment_hash as the invoice it's meant to settle, or we
    // could pay leg 2 and be left holding a preimage leg 1 won't accept.
    if final_payment_hash != payment_hash {
        return Ok(fail_incorrect_details());
    }

    // Live pricing: this is the only check that decides whether we'd lose
    // money, since nothing was pre-approved. Leg 2 costs its amount plus up
    // to `maxfee` in routing fees; ask pricetiers-plugin for today's rate
    // on both and require at least its cheapest tier -- anything above
    // that is just extra profit, there's no fixed amount to match exactly.
    let cfg = &p.state().config;
    let Some(maxfee_msat) = leg2_maxfee(to_amount_msat, cfg.leg2_maxfee_base_msat, cfg.leg2_maxfee_ppm) else {
        return Ok(fail_incorrect_details());
    };
    let Some(cost_msat) = to_amount_msat.checked_add(maxfee_msat) else {
        return Ok(fail_incorrect_details());
    };
    let priced = match fetch_price_tiers(&mut rpc, &meta.to_asset, cost_msat).await {
        Ok(p) => p,
        Err(_) => return Ok(fail_incorrect_details()), // not a pair we quote
    };
    let Some(floor_msat) = priced.tiers.iter().map(|t| t.from_amount_msat).min() else {
        return Ok(fail_incorrect_details());
    };
    if amount_msat < floor_msat {
        return Ok(fail_incorrect_details()); // we'd lose money at our own current price
    }

    // Leg 1 must have come in the asset we priced it in: listpeerchannels
    // shows no asset for a channel in the native coin.
    let Ok(chan_asset) = channel_asset(&mut rpc, &scid).await else {
        return Ok(fail_incorrect_details());
    };
    let expected_chan_asset = is_asset_id(&priced.from_asset).then_some(priced.from_asset.as_str());
    if chan_asset.as_deref() != expected_chan_asset {
        return Ok(fail_incorrect_details());
    }

    // CLTVs by time: leg 1's in this chain's blocks, leg 2's in its own.
    let s1 = block_seconds(&p, &priced.from_asset, &rpc_path(&p), &mut rpc).await;
    let s2 = block_seconds(&p, &meta.to_asset, &leg2_path(&p, &meta.to_asset), &mut rpc2).await;
    let (s1, s2) = match (s1, s2) {
        (Ok(a), Ok(b)) => (a, b),
        (Err(e), _) | (_, Err(e)) => {
            log::warn!("assetswap: {payment_hash}: {e}");
            return Ok(fail_incorrect_details());
        }
    };
    let Some(maxdelay) = leg2_cltv_gate(
        cltv_expiry_relative,
        leg2_final_cltv,
        cfg.safety_blocks,
        cfg.min_hop_budget,
        s1,
        s2,
    ) else {
        log::debug!(
            "assetswap: {payment_hash}: leg 1's {cltv_expiry_relative} blocks of {s1}s leave too little for \
             leg 2's final {leg2_final_cltv} blocks of {s2}s"
        );
        return Ok(fail_incorrect_details());
    };

    // Which node pays leg 2, as "paying" will record it.
    let leg2_node = match getinfo(&mut rpc2).await {
        Ok(info) => match info["id"].as_str() {
            Some(id) => id.to_string(),
            None => return Ok(fail_incorrect_details()),
        },
        Err(e) => {
            log::warn!("assetswap: {payment_hash}: node paying leg 2: {e}");
            return Ok(fail_incorrect_details());
        }
    };

    // Leg 2 must pay someone else. An invoice of our own (either node)
    // would be "paid" by our own funds -- marked paid in exchange for a
    // leg 1 priced in another asset -- and xpay to ourselves never even
    // leaves the node, so no channel's asset check sees it.
    let leg1_node = match getinfo(&mut rpc).await {
        Ok(info) => info["id"].as_str().map(str::to_string),
        Err(_) => None,
    };
    match (&payee, &leg1_node) {
        (Some(payee), Some(leg1_node)) if payee != &leg2_node && payee != leg1_node => {}
        _ => {
            log::info!("assetswap: {payment_hash}: leg 2's invoice is ours (or has no payee): refused");
            return Ok(fail_incorrect_details());
        }
    }

    // A hash whose earlier swap failed may be tried again only if nothing
    // of that leg 2 is still in flight or got through: else a payer could
    // have us pay the payee twice. One that did get through is settled.
    if let Some((rec, _)) = &existing {
        match leg2_status(&p, &payment_hash, rec).await {
            Leg2::Failed { .. } => {}
            Leg2::Complete(pre) => {
                log::info!("assetswap: {payment_hash}: an earlier leg 2 completed after all: resolving leg 1");
                let hex = secret_hex(pre);
                finalize(&p, &payment_hash, "complete", hex.clone()).await;
                return Ok(resolve_with_hex(hex.as_deref()));
            }
            other => {
                log::warn!("assetswap: {payment_hash}: not retrying, earlier leg 2: {other:?}");
                return Ok(fail_incorrect_details());
            }
        }
    }

    // Claim the hash (CAS): only one HTLC goes on to pay leg 2.
    let _inflight = {
        p.state().inflight.lock().unwrap_or_else(|e| e.into_inner()).insert(payment_hash.clone());
        Inflight {
            p: &p,
            hash: payment_hash.clone(),
        }
    };
    let mut rec = QuoteRecord {
        state: "claimed".into(),
        from_asset: priced.from_asset.clone(),
        to_asset: meta.to_asset.clone(),
        final_bolt11: meta.final_bolt11.clone(),
        v: RECORD_VERSION,
        leg2_node: None,
        paying_since: None,
        preimage: None,
    };
    let prev = existing.as_ref().map(|(_, g)| *g);
    let Ok(claimed_generation) = write_record(&mut rpc, &payment_hash, &rec, prev).await else {
        return Ok(fail_incorrect_details()); // someone else claimed this hash
    };

    let layers = match asset_layers(&mut rpc2, &rec.to_asset).await {
        Ok(l) => l,
        Err(e) => {
            log::warn!("assetswap: leg 2 for {payment_hash}: {e}");
            finalize(&p, &payment_hash, "failed", None).await;
            return Ok(fail_incorrect_details());
        }
    };

    // From here on leg 2 may be sent: only `settle` fails leg 1.
    rec.state = "paying".into();
    rec.leg2_node = Some(leg2_node);
    rec.paying_since = Some(now_unix());
    if let Err(e) = write_record(&mut rpc, &payment_hash, &rec, Some(Some(claimed_generation))).await {
        log::warn!("assetswap: {payment_hash}: couldn't record paying: {e}");
        finalize(&p, &payment_hash, "failed", None).await;
        return Ok(fail_incorrect_details());
    }

    match rpc2
        .call_typed(&XpayRequest {
            invstring: rec.final_bolt11.clone(),
            amount_msat: None,
            dev_use_shadow: None,
            label: None,
            localinvreqid: None,
            maxdelay: Some(maxdelay),
            maxfee: Some(Amount::from_msat(maxfee_msat)),
            partial_msat: None,
            payer_note: None,
            retry_for: Some(XPAY_RETRY_FOR),
            layers: Some(layers),
        })
        .await
    {
        Ok(res) => {
            let hex = secret_hex(res.payment_preimage);
            finalize(&p, &payment_hash, "complete", hex.clone()).await;
            Ok(resolve_with_hex(hex.as_deref()))
        }
        Err(e) => {
            log::warn!("assetswap: leg 2 (xpay) failed for {payment_hash}: {e}");
            let gave_up = e.code.is_some_and(|c| XPAY_GAVE_UP.contains(&c));
            Ok(settle(&p, &payment_hash, cltv_expiry, &rec, gave_up).await)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ListsendpaysPaymentsStatus::{COMPLETE, FAILED, PENDING};

    #[test]
    fn classify_parts() {
        assert_eq!(classify::<u8>([]), Leg2::Failed { parts: 0 });
        assert_eq!(classify::<u8>([(FAILED, None), (FAILED, None)]), Leg2::Failed { parts: 2 });
        assert_eq!(classify::<u8>([(FAILED, None), (PENDING, None)]), Leg2::Pending);
        // Any complete part settles it, even with others pending or failed.
        assert_eq!(classify([(FAILED, None), (PENDING, None), (COMPLETE, Some(7u8))]), Leg2::Complete(7));
        assert!(matches!(classify::<u8>([(COMPLETE, None)]), Leg2::Unknown(_)));
    }

    #[test]
    fn preimage_hex() {
        let hex = "11".repeat(32);
        assert_eq!(resolve_with_hex(Some(&hex))["result"], "resolve");
        assert_eq!(resolve_with_hex(Some("zz"))["result"], "fail");
        assert_eq!(resolve_with_hex(None)["result"], "fail");
    }
}
