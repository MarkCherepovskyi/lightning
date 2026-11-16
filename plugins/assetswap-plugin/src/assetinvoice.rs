//! The `assetinvoice` RPC method (payee side): wraps the existing `invoice`
//! RPC into one self-contained string that also carries the asset it
//! requires, since that asset isn't part of the signed bolt11 bytes.

use crate::cltv::block_seconds;
use crate::protocol::{check_asset, is_asset_id};
use crate::rpc::{new_rpc, param, rpc_path};
use crate::State;
use anyhow::{anyhow, Error};
use base64::Engine as _;
use cln_plugin::Plugin;
use cln_rpc::model::requests::{DecodeRequest, InvoiceRequest};
use cln_rpc::model::responses::DecodeType;
use cln_rpc::ClnRpc;
use cln_rpc::primitives::{Amount, AmountOrAny};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Serialize, Deserialize)]
pub(crate) struct AssetInvoiceWrapper {
    pub(crate) v: u8,
    pub(crate) bolt11: String,
    pub(crate) payment_hash: String,
    pub(crate) asset: String,
    pub(crate) amount_msat: u64,
    /// Seconds per block of the payee's chain, for the payer to convert
    /// the invoice's final CLTV into its own chain's blocks. Absent from
    /// older wrappers (then taken to be the payer's own).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(crate) block_seconds: Option<u64>,
}

pub(crate) fn encode_wrapper(w: &AssetInvoiceWrapper) -> Result<String, Error> {
    let bytes = serde_json::to_vec(w)?;
    Ok(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes))
}

pub(crate) fn decode_wrapper(s: &str) -> Result<AssetInvoiceWrapper, Error> {
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(s)
        .map_err(|e| anyhow!("invalid assetinvoice string: {e}"))?;
    Ok(serde_json::from_slice(&bytes)?)
}

/// Longest block time a wrapper may claim for the payee's chain (a Bitcoin
/// block is 600 s). It sets leg 1's CLTV, so an absurd one would lock the
/// payer's funds for that long.
const MAX_BLOCK_SECONDS: u64 = 3600;

/// Decode a wrapper and check it against the signed bolt11 inside it. The
/// wrapper's own fields are not signed: the hash leg 1 is sent on, and the
/// amount it is priced from, must be the invoice's own, or whoever handed us
/// the wrapper could take leg 1 without the invoice ever being paid, or
/// price it far above the invoice.
pub(crate) async fn checked_wrapper(rpc: &mut ClnRpc, s: &str) -> Result<AssetInvoiceWrapper, Error> {
    let w = decode_wrapper(s)?;
    let d = rpc
        .call_typed(&DecodeRequest {
            string: w.bolt11.clone(),
        })
        .await?;
    if !matches!(d.item_type, DecodeType::BOLT11_INVOICE) || !d.valid {
        return Err(anyhow!("assetinvoice does not hold a valid bolt11 invoice"));
    }
    if !d.payment_hash.is_some_and(|h| h.to_string().eq_ignore_ascii_case(&w.payment_hash)) {
        return Err(anyhow!("assetinvoice's payment_hash is not its invoice's"));
    }
    if d.amount_msat.map(|a| a.msat()) != Some(w.amount_msat) {
        return Err(anyhow!("assetinvoice's amount_msat is not its invoice's"));
    }
    if w.block_seconds.is_some_and(|s| s == 0 || s > MAX_BLOCK_SECONDS) {
        return Err(anyhow!("assetinvoice's block_seconds is out of range (1..={MAX_BLOCK_SECONDS})"));
    }
    Ok(w)
}

pub(crate) async fn assetinvoice(p: Plugin<State>, args: Value) -> Result<Value, Error> {
    let amount_msat = param(&args, 0, "amount_msat")
        .and_then(Value::as_u64)
        .ok_or_else(|| anyhow!("Missing or invalid amount_msat"))?;
    let label = param(&args, 1, "label")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing label"))?
        .to_owned();
    let description = param(&args, 2, "description")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing description"))?
        .to_owned();
    let asset = param(&args, 3, "asset")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing asset (this node's native coin, e.g. 'lbtc' or 'eth', or the id of the asset you want to be paid in)"))?
        .to_owned();
    check_asset(&asset)?;

    let mut rpc = new_rpc(&p).await?;
    // A native coin: an ordinary invoice. An issued asset: contrib/
    // assetchan's, which remembers the asset so its htlc_accepted hook only
    // takes the payment over a channel in it, and hints only at those.
    let (bolt11, payment_hash) = if is_asset_id(&asset) {
        if amount_msat % 1000 != 0 {
            return Err(anyhow!("amount_msat must be whole units of {asset}"));
        }
        let inv: Value = rpc
            .call_raw(
                "assetchan-invoice",
                &json!({"amount": amount_msat / 1000, "asset": asset, "label": label, "description": description}),
            )
            .await
            .map_err(|e| anyhow!("assetchan-invoice: {e}"))?;
        let field = |k: &str| {
            inv[k]
                .as_str()
                .map(str::to_string)
                .ok_or_else(|| anyhow!("assetchan-invoice returned no {k}"))
        };
        (field("bolt11")?, field("payment_hash")?)
    } else {
        let inv = rpc
            .call_typed(&InvoiceRequest {
                amount_msat: AmountOrAny::Amount(Amount::from_msat(amount_msat)),
                description,
                label,
                cltv: None,
                deschashonly: None,
                expiry: None,
                preimage: None,
                exposeprivatechannels: None,
                fallbacks: None,
            })
            .await?;
        (inv.bolt11, inv.payment_hash.to_string())
    };

    // Best effort: without it a payer assumes its own chain's block time.
    let block_seconds = match block_seconds(&p, &asset, &rpc_path(&p), &mut rpc).await {
        Ok(s) => Some(s),
        Err(e) => {
            log::warn!("assetswap: assetinvoice without block_seconds: {e}");
            None
        }
    };
    let wrapper = AssetInvoiceWrapper {
        v: 1,
        bolt11,
        payment_hash,
        asset,
        amount_msat,
        block_seconds,
    };
    Ok(json!({ "assetinvoice": encode_wrapper(&wrapper)? }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrapper_block_seconds_is_optional() {
        // A wrapper from before block_seconds still decodes...
        let old = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .encode(r#"{"v":1,"bolt11":"lnbcrt1","payment_hash":"00","asset":"eth","amount_msat":5}"#);
        assert_eq!(decode_wrapper(&old).unwrap().block_seconds, None);
        // ...and a new one round-trips it.
        let w = AssetInvoiceWrapper {
            v: 1,
            bolt11: "lnbcrt1".into(),
            payment_hash: "00".into(),
            asset: "eth".into(),
            amount_msat: 5,
            block_seconds: Some(2),
        };
        assert_eq!(decode_wrapper(&encode_wrapper(&w).unwrap()).unwrap().block_seconds, Some(2));
    }
}
