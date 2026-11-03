//! The swap-quote wire protocol (carried over `custommsg`) and the
//! datastore-backed quote record a provider keeps for each one it offers.

use crate::config::SwapPair;
use anyhow::{anyhow, Error};
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

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

pub(crate) fn make_tiers(pair: &SwapPair, to_amount_msat: u64) -> Vec<Tier> {
    // Margins in parts-per-thousand over today's reference rate: a looser
    // (higher) cap leaves more room before a live-price recheck can ever
    // fail, at the cost of a worse worst-case rate for the payer.
    const MARGINS: [(&str, u64); 3] = [("low", 1005), ("medium", 1015), ("high", 1030)];
    MARGINS
        .iter()
        .map(|(name, permille)| {
            let rate_ppm = pair.rate_ppm * permille / 1000;
            let from_amount_msat = to_amount_msat * rate_ppm / 1_000_000 + pair.fee_base_msat;
            Tier {
                name: name.to_string(),
                rate_ppm,
                from_amount_msat,
            }
        })
        .collect()
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
