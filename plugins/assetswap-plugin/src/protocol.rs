//! The swap-quote wire protocol (carried over `custommsg`) and the
//! datastore-backed quote record a provider keeps for each one it offers.

use anyhow::{anyhow, Error};
use cln_rpc::primitives::Sha256;
use cln_rpc::ClnRpc;
use serde::{Deserialize, Serialize};
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

/// Every asset string this plugin's own protocol/config carries (quote
/// messages, datastore records, `--assetswap-pair`/`-provider`, the
/// `assetinvoice`/`payassetinvoice` RPCs) uses the literal string `"lbtc"`
/// for L-BTC, never a resolved asset id — `xpay`/`getroutes`/`invoice`'s
/// own `asset` parameter needs `None` (omitted) for L-BTC, not an explicit
/// hash (L-BTC has no Liquid asset tag; passing one is a different,
/// unrelated id and breaks routing/invoice matching). `asset_sha` is the
/// single point doing that translation before any such RPC call.
pub(crate) fn is_lbtc(s: &str) -> bool {
    s.eq_ignore_ascii_case("lbtc")
}

pub(crate) fn asset_sha(s: &str) -> Result<Option<Sha256>, Error> {
    if is_lbtc(s) {
        return Ok(None);
    }
    Ok(Some(
        Sha256::from_str(s).map_err(|e| anyhow!("invalid asset id {s}: {e}"))?,
    ))
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub(crate) struct Tier {
    pub(crate) name: String,
    pub(crate) rate_ppm: u64,
    pub(crate) from_amount_msat: u64,
}

#[derive(Serialize, Deserialize, Debug)]
pub(crate) struct QuoteRequest {
    pub(crate) req_id: String,
    pub(crate) final_bolt11: String,
    pub(crate) to_asset: String,
}

#[derive(Serialize, Deserialize, Debug)]
pub(crate) struct QuoteReply {
    pub(crate) req_id: String,
    pub(crate) ok: bool,
    #[serde(default)]
    pub(crate) reason: Option<String>,
    #[serde(default)]
    pub(crate) from_asset: Option<String>,
    #[serde(default)]
    pub(crate) to_asset: Option<String>,
    #[serde(default)]
    pub(crate) payment_hash: Option<String>,
    #[serde(default)]
    pub(crate) tiers: Option<Vec<Tier>>,
    #[serde(default)]
    pub(crate) quote_expiry_unix: Option<u64>,
    /// The minimum cltv_expiry_relative leg 1 must carry for the provider's
    /// up-front CLTV sanity gate to ever pass (leg2_final_cltv +
    /// safety_blocks + min_hop_budget) — the payer must request at least
    /// this as its own `final_cltv` when routing leg 1.
    #[serde(default)]
    pub(crate) min_leg1_cltv: Option<u32>,
}

/// What a swap provider remembers about a quote it offered, keyed by the
/// final recipient's payment_hash in the datastore (see `quote_key`).
#[derive(Serialize, Deserialize, Clone, Debug)]
pub(crate) struct QuoteRecord {
    pub(crate) state: String, // "quoted" | "claimed" | "failed"
    pub(crate) from_asset: String,
    pub(crate) to_asset: String,
    pub(crate) final_bolt11: String,
    pub(crate) quote_expiry_unix: u64,
    pub(crate) leg2_final_cltv: u32,
    pub(crate) tiers: Vec<Tier>,
}

pub(crate) fn quote_key(payment_hash: &str) -> Vec<String> {
    vec!["assetswap".into(), "quote".into(), payment_hash.into()]
}

pub(crate) fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

static REQ_COUNTER: AtomicU64 = AtomicU64::new(0);

pub(crate) fn new_req_id() -> String {
    let n = REQ_COUNTER.fetch_add(1, Ordering::Relaxed);
    let t = now_unix();
    format!("{t:x}-{n:x}")
}

/// What `plugins/pricetiers-plugin`'s `pricetiers` RPC returns: tiers for
/// `to_asset`/`to_amount_msat`, plus the `from_asset` it discovered from
/// its own matching configured pair and the reference `rate_ppm` behind
/// those tiers (used for the `htlc_accepted` hook's live-price recheck,
/// which only needs the rate, not a concrete amount).
#[derive(Deserialize, Debug)]
pub(crate) struct PriceTiers {
    pub(crate) from_asset: String,
    pub(crate) rate_ppm: u64,
    #[allow(dead_code)]
    pub(crate) fee_base_msat: u64,
    pub(crate) tiers: Vec<Tier>,
}

#[derive(Serialize, Debug)]
struct PriceTiersRequest {
    to_asset: String,
    to_amount_msat: u64,
}

/// Calls `pricetiers-plugin`'s `pricetiers` RPC method as a plain local
/// RPC call -- it's not a core lightningd method, so this goes through
/// `call_raw` rather than `call_typed`. Fails (as "no pair configured")
/// if that plugin isn't loaded, or isn't quoting this `to_asset`, exactly
/// as if the pair were simply unsupported.
pub(crate) async fn fetch_price_tiers(
    rpc: &mut ClnRpc,
    to_asset: &str,
    to_amount_msat: u64,
) -> Result<PriceTiers, Error> {
    rpc.call_raw(
        "pricetiers",
        &PriceTiersRequest {
            to_asset: to_asset.to_string(),
            to_amount_msat,
        },
    )
    .await
    .map_err(|e| anyhow!("pricetiers-plugin: {e}"))
}

pub(crate) fn encode_custommsg(msgtype: u16, body: &impl Serialize) -> Result<String, Error> {
    let mut bytes = msgtype.to_be_bytes().to_vec();
    bytes.extend_from_slice(&serde_json::to_vec(body)?);
    Ok(hex::encode(bytes))
}

pub(crate) fn decode_custommsg(payload_hex: &str) -> Result<(u16, Vec<u8>), Error> {
    let bytes = hex::decode(payload_hex)?;
    if bytes.len() < 2 {
        return Err(anyhow!("custommsg payload too short"));
    }
    Ok((u16::from_be_bytes([bytes[0], bytes[1]]), bytes[2..].to_vec()))
}
