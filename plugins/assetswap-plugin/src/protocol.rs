//! What a payer packs into leg 1's `payment_metadata` to tell the provider
//! what to pay for leg 2 (no prior negotiation -- the provider prices and
//! accepts/rejects live, in `htlc.rs`'s `htlc_accepted` hook), and the
//! datastore-backed record of a swap the provider has committed to.

use anyhow::{anyhow, Error};
use cln_rpc::ClnRpc;
use serde::{Deserialize, Serialize};

/// How this plugin names what a payment is in: an issued Liquid asset by
/// its 32-byte hex id (as elements-cli prints it), anything else ("lbtc",
/// "eth", ...) by a label for the coin native to some node's chain.  A
/// native coin needs nothing special to invoice or pay in; an issued asset
/// goes through contrib/assetchan (see `rpc::asset_layers`).  On a Liquid
/// node the native coin's label must be "lbtc", as assetchan calls it.
pub(crate) fn is_asset_id(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub(crate) struct Tier {
    pub(crate) name: String,
    pub(crate) rate_ppm: u64,
    pub(crate) from_amount_msat: u64,
}

/// What a payer embeds in leg 1's `payment_metadata` (hex-encoded JSON) --
/// everything the provider needs to look up leg 2 and price it live. No
/// request/reply round trip beforehand: the provider either accepts this
/// on arrival (profitable at its current price) or fails the HTLC.
#[derive(Serialize, Deserialize, Debug)]
pub(crate) struct SwapMetadata {
    pub(crate) final_bolt11: String,
    pub(crate) to_asset: String,
}

pub(crate) fn encode_swap_metadata(m: &SwapMetadata) -> Result<String, Error> {
    Ok(hex::encode(serde_json::to_vec(m)?))
}

pub(crate) fn decode_swap_metadata(payload_hex: &str) -> Result<SwapMetadata, Error> {
    let bytes = hex::decode(payload_hex)?;
    Ok(serde_json::from_slice(&bytes)?)
}

/// What a swap provider remembers about a swap it has committed to, keyed
/// by the final recipient's payment_hash in the datastore (see
/// `quote_key`). Only ever written once leg 1's HTLC has already been
/// validated and accepted -- not a pre-negotiated offer, just a durable
/// record of a decision already made, so a restart-replay of the same HTLC
/// re-derives its outcome instead of re-deciding or re-running leg 2 (see
/// `htlc.rs::resolve_from_leg2_status`).
#[derive(Serialize, Deserialize, Clone, Debug)]
pub(crate) struct QuoteRecord {
    pub(crate) state: String, // "claimed" | "failed"
    pub(crate) from_asset: String,
    pub(crate) to_asset: String,
    pub(crate) final_bolt11: String,
}

pub(crate) fn quote_key(payment_hash: &str) -> Vec<String> {
    vec!["assetswap".into(), "quote".into(), payment_hash.into()]
}

/// What `plugins/pricetiers-plugin`'s `pricetiers` RPC returns: tiers for
/// `to_asset`/`to_amount_msat`, plus the `from_asset` it discovered from
/// its own matching configured pair and the reference `rate_ppm` behind
/// those tiers (used for the `htlc_accepted` hook's live-price recheck,
/// which only needs the rate, not a concrete amount).
#[derive(Serialize, Deserialize, Debug)]
pub(crate) struct PriceTiers {
    pub(crate) from_asset: String,
    pub(crate) rate_ppm: u64,
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
