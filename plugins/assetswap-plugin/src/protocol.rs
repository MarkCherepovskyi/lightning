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

/// The native-coin labels this plugin knows: L-BTC on Liquid (as
/// assetchan calls it), ETH on an EVM node, BTC on Bitcoin.
pub(crate) const NATIVE_LABELS: [&str; 3] = ["lbtc", "eth", "btc"];

/// An asset this plugin can invoice or pay in: an issued asset's id or a
/// known native-coin label. Anything else is refused, not guessed at.
pub(crate) fn check_asset(s: &str) -> Result<(), Error> {
    if is_asset_id(s) || NATIVE_LABELS.contains(&s) {
        Ok(())
    } else {
        Err(anyhow!(
            "unknown asset {s:?}: expected a 64-hex asset id or one of {}",
            NATIVE_LABELS.join(", ")
        ))
    }
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
/// `quote_key`). Written only once leg 1's HTLC has passed every check,
/// and moved on with the datastore's generation CAS:
///
/// ```text
///   claimed --> paying --> complete
///      |          |
///      +----------+-----> failed --(no leg-2 part pending or complete)--> claimed
/// ```
///
/// "claimed": this HTLC won the hash; leg 2 not attempted yet. "paying":
/// written right before `xpay`, naming the node that pays leg 2; from here
/// on leg 1 is only resolved by what that node's `listsendpays` says (see
/// `htlc.rs::settle`). "complete": leg 2 paid, `preimage` kept, and any
/// later HTLC with this hash is resolved with it, never paid again.
/// "failed": leg 1 was failed.
///
/// A record without `v` predates this state machine: its "claimed" may
/// have reached `xpay`, so it is treated as "paying" on the node now
/// configured for its asset.
#[derive(Serialize, Deserialize, Clone, Debug)]
pub(crate) struct QuoteRecord {
    pub(crate) state: String, // "claimed" | "paying" | "complete" | "failed"
    pub(crate) from_asset: String,
    pub(crate) to_asset: String,
    pub(crate) final_bolt11: String,
    #[serde(default)]
    pub(crate) v: u8,
    /// Node id of the node that pays leg 2 (set from "paying" on).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) leg2_node: Option<String>,
    /// Unix time "paying" was written.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) paying_since: Option<u64>,
    /// Leg 2's preimage, hex (set in "complete").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) preimage: Option<String>,
}

pub(crate) const RECORD_VERSION: u8 = 2;

/// The most leg 2 may pay in routing fees: `base` msat plus `ppm` parts
/// per million of `amount_msat`. `None` if it doesn't fit in a u64.
pub(crate) fn leg2_maxfee(amount_msat: u64, base_msat: u64, ppm: u64) -> Option<u64> {
    let prop = u128::from(amount_msat) * u128::from(ppm) / 1_000_000;
    u64::try_from(prop).ok()?.checked_add(base_msat)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maxfee() {
        // Defaults on 100,000 sat: 5000 + 0.5% = 505,000 msat.
        assert_eq!(leg2_maxfee(100_000_000, 5000, 5000), Some(505_000));
        assert_eq!(leg2_maxfee(10_000_000, 5000, 5000), Some(55_000));
        // The proportional part rounds down.
        assert_eq!(leg2_maxfee(199, 0, 5000), Some(0));
        assert_eq!(leg2_maxfee(200, 0, 5000), Some(1));
        assert_eq!(leg2_maxfee(0, 7, 5000), Some(7));
        assert_eq!(leg2_maxfee(123, 7, 0), Some(7));
        // No overflow in the product...
        assert_eq!(leg2_maxfee(u64::MAX, 0, 1_000_000), Some(u64::MAX));
        assert_eq!(leg2_maxfee(u64::MAX, 0, 1000), Some(u64::MAX / 1000));
        // ...but a fee that can't be a u64 is None.
        assert_eq!(leg2_maxfee(u64::MAX, 0, 2_000_000), None);
        assert_eq!(leg2_maxfee(u64::MAX, 1, 1_000_000), None);
    }

    #[test]
    fn assets() {
        assert!(check_asset("lbtc").is_ok());
        assert!(check_asset("eth").is_ok());
        assert!(check_asset(&"ab".repeat(32)).is_ok());
        assert!(check_asset("LBTC").is_err());
        assert!(check_asset("usdt").is_err());
        assert!(check_asset(&"ab".repeat(31)).is_err());
        assert!(check_asset("").is_err());
    }

    #[test]
    fn legacy_record() {
        let r: QuoteRecord =
            serde_json::from_str(r#"{"state":"claimed","from_asset":"a","to_asset":"b","final_bolt11":"x"}"#).unwrap();
        assert_eq!(r.v, 0);
        assert!(r.leg2_node.is_none() && r.preimage.is_none() && r.paying_since.is_none());
    }
}
