//! The swap-provider's `htlc_accepted` hook: decode what a candidate leg-1
//! HTLC's `payment_metadata` is asking for, price it live, atomically
//! claim it, run leg 2, and resolve or fail leg 1 — plus the
//! restart-replay idempotency path for an HTLC we already committed to
//! before a prior process exited. There's no prior quote to look up: the
//! provider decides live, on arrival, whether this HTLC is profitable at
//! its current price (see pay.rs's doc comment for why).

use crate::protocol::{
    asset_sha, decode_swap_metadata, fetch_price_tiers, is_lbtc, quote_key, QuoteRecord,
};
use crate::rpc::new_rpc;
use crate::State;
use anyhow::{anyhow, Error};
use cln_plugin::Plugin;
use cln_rpc::model::requests::{
    DatastoreMode, DatastoreRequest, DecodeRequest, ListdatastoreRequest,
    ListpeerchannelsRequest, ListsendpaysRequest, WaitsendpayRequest, XpayRequest,
};
use cln_rpc::model::responses::ListsendpaysPaymentsStatus;
use cln_rpc::primitives::{Secret, ShortChannelId};
use cln_rpc::ClnRpc;
use serde_json::{json, Value};
use std::str::FromStr;

/// How long to wait on a still-`pending` leg-2 payment during restart-replay
/// idempotency handling before giving up and failing leg 1. See
/// `resolve_from_leg2_status`.
const RESTART_LEG2_WAIT_SECS: u32 = 30;

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

async fn lookup_quote(rpc: &mut ClnRpc, payment_hash: &str) -> Result<Option<QuoteRecord>, Error> {
    let res = rpc
        .call_typed(&ListdatastoreRequest {
            key: Some(quote_key(payment_hash)),
        })
        .await?;
    match res.datastore.into_iter().next() {
        Some(d) => {
            let s = d.string.ok_or_else(|| anyhow!("quote record has no string value"))?;
            Ok(Some(serde_json::from_str(&s)?))
        }
        None => Ok(None),
    }
}

async fn channel_asset(rpc: &mut ClnRpc, scid: &str) -> Result<Option<String>, Error> {
    let short = ShortChannelId::from_str(scid)?;
    let res = rpc
        .call_typed(&ListpeerchannelsRequest {
            channel_id: None,
            id: None,
            short_channel_id: Some(short),
        })
        .await?;
    Ok(res.channels.into_iter().next().and_then(|c| c.liquid_asset))
}

/// Atomic claim via datastore's generation CAS: only the first HTLC for a
/// given payment_hash wins, so a replayed/duplicated HTLC can't
/// double-commit (and a second attempt after an earlier "failed" verdict
/// is allowed to retry, rather than being permanently wedged). Returns the
/// record's new generation on success, so a caller can chain a further
/// state transition ("claimed" -> "failed") without a second round-trip to
/// learn it.
async fn claim_quote_atomically(
    rpc: &mut ClnRpc,
    payment_hash: &str,
    rec: &QuoteRecord,
) -> Result<Option<u64>, Error> {
    let cur = rpc
        .call_typed(&ListdatastoreRequest {
            key: Some(quote_key(payment_hash)),
        })
        .await?;
    let existing = cur.datastore.into_iter().next();
    let mode = if existing.is_some() { DatastoreMode::MUST_REPLACE } else { DatastoreMode::MUST_CREATE };
    let generation = existing.and_then(|d| d.generation);
    let claimed = QuoteRecord {
        state: "claimed".into(),
        ..rec.clone()
    };
    match rpc
        .call_typed(&DatastoreRequest {
            generation,
            hex: None,
            mode: Some(mode),
            string: Some(serde_json::to_string(&claimed)?),
            key: quote_key(payment_hash),
        })
        .await
    {
        Ok(res) => Ok(Some(res.generation)),
        Err(_) => Ok(None),
    }
}

/// Move a quote record to a terminal "failed" state after leg 2 doesn't go
/// through, so it's never left dangling in "claimed" with no resolution
/// (which would look like an in-flight swap forever).
async fn mark_quote_failed(
    rpc: &mut ClnRpc,
    payment_hash: &str,
    rec: &QuoteRecord,
    claimed_generation: u64,
) {
    let failed = QuoteRecord {
        state: "failed".into(),
        ..rec.clone()
    };
    let Ok(string) = serde_json::to_string(&failed) else {
        return;
    };
    if let Err(e) = rpc
        .call_typed(&DatastoreRequest {
            generation: Some(claimed_generation),
            hex: None,
            mode: Some(DatastoreMode::MUST_REPLACE),
            string: Some(string),
            key: quote_key(payment_hash),
        })
        .await
    {
        log::warn!("assetswap: couldn't mark quote {payment_hash} as failed: {e}");
    }
}

fn resolve_with_preimage(preimage: Secret) -> Value {
    match serde_json::to_value(preimage) {
        Ok(Value::String(hex)) => json!({"result": "resolve", "payment_key": hex}),
        _ => fail_incorrect_details(),
    }
}

/// Idempotency for the restart-replay case: a "claimed" record means we
/// already decided to attempt leg 2 before a prior process exited (crash
/// or restart) without lightningd ever getting our verdict. Re-derive the
/// right answer from leg 2's own payment status instead of guessing or
/// re-running it. `complete` means it already paid out, so resolve leg 1
/// with that preimage. A still-`pending` attempt means leg 2 was genuinely
/// in flight at restart (the common case right after a crash) — rather
/// than failing leg 1 prematurely while leg 2 might still land a moment
/// later (which would leave us out of pocket, having paid leg 2 with
/// nothing to show for it), wait on it with `waitsendpay` before deciding.
/// No attempt found, or an already-`failed` one, means it's safe to fail
/// leg 1 outright.
///
/// **Operational note**: this logic only ever runs if the plugin is
/// loaded before lightningd replays the pending HTLC — which requires the
/// provider to load it from persistent config (`plugin=` in its `config`
/// file), not `plugin start`. See CROSS_ASSET_SWAP.md §7.
async fn resolve_from_leg2_status(rpc: &mut ClnRpc, rec: &QuoteRecord) -> Value {
    let res = rpc
        .call_typed(&ListsendpaysRequest {
            bolt11: Some(rec.final_bolt11.clone()),
            index: None,
            limit: None,
            payment_hash: None,
            start: None,
            status: None,
        })
        .await;
    let Ok(res) = res else {
        return fail_incorrect_details();
    };

    if let Some(pm) = res
        .payments
        .iter()
        .find(|pm| pm.status == ListsendpaysPaymentsStatus::COMPLETE)
    {
        return match pm.payment_preimage {
            Some(preimage) => resolve_with_preimage(preimage),
            None => fail_incorrect_details(),
        };
    }

    if let Some(pm) = res
        .payments
        .iter()
        .find(|pm| pm.status == ListsendpaysPaymentsStatus::PENDING)
    {
        let outcome = rpc
            .call_typed(&WaitsendpayRequest {
                groupid: Some(pm.groupid),
                partid: pm.partid,
                timeout: Some(RESTART_LEG2_WAIT_SECS),
                payment_hash: pm.payment_hash,
            })
            .await;
        return match outcome {
            Ok(res) => match res.payment_preimage {
                Some(preimage) => resolve_with_preimage(preimage),
                None => fail_incorrect_details(),
            },
            Err(_) => fail_incorrect_details(), // failed, or timed out waiting
        };
    }

    fail_incorrect_details() // no leg-2 attempt found at all
}

pub(crate) async fn on_htlc_accepted(p: Plugin<State>, v: Value) -> Result<Value, Error> {
    // Only a final hop (no onion.short_channel_id/next hop) can be a swap
    // settlement — never second-guess an HTLC we're meant to forward on.
    let is_final = v
        .get("onion")
        .and_then(|o| o.get("short_channel_id"))
        .is_none();
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

    // Restart-replay idempotency: if we already committed to this swap
    // before a prior process exited, don't re-decide -- re-derive the
    // verdict from leg 2's own status (see resolve_from_leg2_status). A
    // "failed" record means we already gave a verdict in a prior run; let
    // a fresh attempt (same payment_hash, e.g. the payer retrying with a
    // bigger amount) fall through and be judged again from scratch.
    match lookup_quote(&mut rpc, &payment_hash).await {
        Ok(Some(r)) if r.state == "claimed" => {
            return Ok(resolve_from_leg2_status(&mut rpc, &r).await);
        }
        Ok(_) => {}
        Err(e) => {
            log::warn!("assetswap: datastore lookup failed for {payment_hash}: {e}");
            return Ok(json!({"result": "continue"}));
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

    let decoded = rpc
        .call_typed(&DecodeRequest {
            string: meta.final_bolt11.clone(),
        })
        .await?;
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
    // money, since nothing was pre-approved. Ask pricetiers-plugin for
    // today's rate and require at least its cheapest tier -- anything above
    // that is just extra profit, there's no fixed amount to match exactly.
    let priced = match fetch_price_tiers(&mut rpc, &meta.to_asset, to_amount_msat).await {
        Ok(p) => p,
        Err(_) => return Ok(fail_incorrect_details()), // not a pair we quote
    };
    let Some(floor_msat) = priced.tiers.iter().map(|t| t.from_amount_msat).min() else {
        return Ok(fail_incorrect_details());
    };
    if amount_msat < floor_msat {
        return Ok(fail_incorrect_details()); // we'd lose money at our own current price
    }

    // CLN's own `liquid_asset` field is `None` for an L-BTC channel, not
    // the literal "lbtc" our own protocol uses — bridge the two here.
    let chan_asset = channel_asset(&mut rpc, &scid).await.ok().flatten();
    let expected_chan_asset = if is_lbtc(&priced.from_asset) { None } else { Some(priced.from_asset.as_str()) };
    let budget = leg2_final_cltv + p.state().config.safety_blocks + p.state().config.min_hop_budget;
    if chan_asset.as_deref() != expected_chan_asset || cltv_expiry_relative < budget {
        return Ok(fail_incorrect_details());
    }

    let rec = QuoteRecord {
        state: "claimed".into(), // claim_quote_atomically always writes this state regardless
        from_asset: priced.from_asset.clone(),
        to_asset: meta.to_asset.clone(),
        final_bolt11: meta.final_bolt11.clone(),
    };
    let Some(claimed_generation) = claim_quote_atomically(&mut rpc, &payment_hash, &rec)
        .await
        .unwrap_or(None)
    else {
        return Ok(fail_incorrect_details()); // replay: someone already claimed this hash
    };

    let maxdelay = cltv_expiry_relative.saturating_sub(leg2_final_cltv + p.state().config.safety_blocks);
    let to_asset_sha = match asset_sha(&rec.to_asset) {
        Ok(a) => a,
        Err(_) => {
            mark_quote_failed(&mut rpc, &payment_hash, &rec, claimed_generation).await;
            return Ok(fail_incorrect_details());
        }
    };
    match rpc
        .call_typed(&XpayRequest {
            invstring: rec.final_bolt11.clone(),
            amount_msat: None,
            asset: to_asset_sha,
            dev_use_shadow: None,
            label: None,
            localinvreqid: None,
            maxdelay: Some(maxdelay),
            maxfee: None,
            partial_msat: None,
            payer_note: None,
            retry_for: None,
            layers: None,
        })
        .await
    {
        Ok(res) => Ok(resolve_with_preimage(res.payment_preimage)),
        Err(e) => {
            log::warn!("assetswap: leg 2 (xpay) failed for {payment_hash}: {e}");
            mark_quote_failed(&mut rpc, &payment_hash, &rec, claimed_generation).await;
            Ok(fail_incorrect_details())
        }
    }
}
