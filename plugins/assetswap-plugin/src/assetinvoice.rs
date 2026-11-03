//! The `assetinvoice` RPC method (payee side): wraps the existing `invoice`
//! RPC into one self-contained string that also carries the asset it
//! requires, since that asset isn't part of the signed bolt11 bytes.

use crate::protocol::is_lbtc;
use crate::rpc::{new_rpc, param};
use crate::State;
use anyhow::{anyhow, Error};
use base64::Engine as _;
use cln_plugin::Plugin;
use cln_rpc::model::requests::InvoiceRequest;
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
        .ok_or_else(|| anyhow!("Missing asset ('lbtc', or the id of the asset you want to be paid in)"))?
        .to_owned();

    let mut rpc = new_rpc(&p).await?;
    let inv = rpc
        .call_typed(&InvoiceRequest {
            amount_msat: AmountOrAny::Amount(Amount::from_msat(amount_msat)),
            description,
            label,
            asset: if is_lbtc(&asset) { None } else { Some(asset.clone()) },
            cltv: None,
            deschashonly: None,
            expiry: None,
            preimage: None,
            exposeprivatechannels: None,
            fallbacks: None,
        })
        .await?;

    let wrapper = AssetInvoiceWrapper {
        v: 1,
        bolt11: inv.bolt11,
        payment_hash: inv.payment_hash.to_string(),
        asset,
        amount_msat,
    };
    Ok(json!({ "assetinvoice": encode_wrapper(&wrapper)? }))
}
