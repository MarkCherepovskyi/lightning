//! The `payassetinvoice` RPC method (payer side): try a direct, same-asset
//! payment first, and fall back to a swap-provider quote + a hash-linked
//! leg-1 payment if that fails.

use crate::assetinvoice::decode_wrapper;
use crate::protocol::{asset_sha, encode_custommsg, new_req_id, QuoteReply, QuoteRequest};
use crate::rpc::{getroutes_path_to_sendpay_route, new_rpc, param};
use crate::State;
use anyhow::{anyhow, Error};
use cln_plugin::Plugin;
use cln_rpc::model::requests::{
    GetinfoRequest, GetroutesRequest, SendcustommsgRequest, SendpayRequest, WaitsendpayRequest,
    XpayRequest,
};
use cln_rpc::primitives::{Amount, PublicKey, Sha256};
use serde_json::{json, Value};
use std::str::FromStr;
use std::time::Duration;
use tokio::sync::oneshot;

async fn get_quote(
    p: &Plugin<State>,
    provider: PublicKey,
    final_bolt11: &str,
    to_asset: &str,
) -> Result<QuoteReply, Error> {
    let req_id = new_req_id();
    let (tx, rx) = oneshot::channel();
    p.state()
        .pending_quotes
        .lock()
        .await
        .insert(req_id.clone(), tx);

    let req = QuoteRequest {
        req_id: req_id.clone(),
        final_bolt11: final_bolt11.to_string(),
        to_asset: to_asset.to_string(),
    };
    let msg = encode_custommsg(p.state().config.msgtype_request, &req)?;

    let mut rpc = new_rpc(p).await?;
    rpc.call_typed(&SendcustommsgRequest {
        node_id: provider,
        msg,
    })
    .await
    .map_err(|e| anyhow!("couldn't send quote request to {provider}: {e}"))?;

    let reply = tokio::time::timeout(Duration::from_secs(10), rx)
        .await
        .map_err(|_| anyhow!("timed out waiting for a swap quote from {provider}"))?
        .map_err(|_| anyhow!("quote request was dropped before a reply arrived"))?;

    if !reply.ok {
        return Err(anyhow!(
            "swap provider {provider} declined: {}",
            reply.reason.unwrap_or_else(|| "no reason given".into())
        ));
    }
    Ok(reply)
}

pub(crate) async fn payassetinvoice(p: Plugin<State>, args: Value) -> Result<Value, Error> {
    let encoded = param(&args, 0, "assetinvoice")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing assetinvoice"))?;
    let tier_name = param(&args, 1, "tier")
        .and_then(Value::as_str)
        .unwrap_or("medium")
        .to_string();
    let wrapper = decode_wrapper(encoded)?;

    let mut rpc = new_rpc(&p).await?;

    // Try the direct, same-asset path first — a payer may already hold
    // channels in the required asset's network, in which case no swap is
    // needed at all.
    let direct_err = match rpc
        .call_typed(&XpayRequest {
            invstring: wrapper.bolt11.clone(),
            asset: asset_sha(&wrapper.asset)?,
            amount_msat: None,
            dev_use_shadow: None,
            label: None,
            localinvreqid: None,
            maxdelay: None,
            maxfee: None,
            partial_msat: None,
            payer_note: None,
            retry_for: None,
            layers: None,
        })
        .await
    {
        Ok(res) => return Ok(json!(res)),
        Err(e) => e,
    };

    // No direct route: fall back to a swap-provider.
    let Some(&provider) = p.state().config.providers.first() else {
        return Err(anyhow!(
            "direct payment in asset {} failed ({direct_err}), and no \
             assetswap-provider is configured to attempt a swap",
            wrapper.asset
        ));
    };

    let quote = get_quote(&p, provider, &wrapper.bolt11, &wrapper.asset)
        .await
        .map_err(|e| anyhow!("direct payment failed ({direct_err}); swap quote also failed: {e}"))?;
    let tiers = quote
        .tiers
        .ok_or_else(|| anyhow!("provider sent no tiers"))?;
    let tier = tiers
        .iter()
        .find(|t| t.name == tier_name)
        .ok_or_else(|| anyhow!("provider didn't offer a {tier_name:?} tier"))?;
    let from_asset = quote
        .from_asset
        .ok_or_else(|| anyhow!("provider sent no from_asset"))?;
    let final_cltv = quote
        .min_leg1_cltv
        .ok_or_else(|| anyhow!("provider sent no min_leg1_cltv"))?;

    let my_id = rpc.call_typed(&GetinfoRequest {}).await?.id;
    let maxfee_msat = tier.from_amount_msat / 100; // 1% leg-1 routing-fee budget
    let routes = rpc
        .call_typed(&GetroutesRequest {
            source: my_id,
            destination: provider,
            amount_msat: Amount::from_msat(tier.from_amount_msat),
            asset: asset_sha(&from_asset)?,
            final_cltv,
            layers: vec!["auto.localchans".into(), "auto.sourcefree".into()],
            maxfee_msat: Amount::from_msat(maxfee_msat),
            maxdelay: None,
            maxparts: None,
        })
        .await
        .map_err(|e| anyhow!("no leg-1 route to swap provider {provider} in asset {from_asset}: {e}"))?;
    let best = routes
        .routes
        .first()
        .ok_or_else(|| anyhow!("askrene returned no routes to the swap provider"))?;
    let route = getroutes_path_to_sendpay_route(&best.path);

    // Leg 1 carries the FINAL recipient's payment_hash, not a fresh one —
    // that's the hash-link the whole swap's atomicity rests on.
    rpc.call_typed(&SendpayRequest {
        amount_msat: Some(Amount::from_msat(tier.from_amount_msat)),
        bolt11: None,
        description: None,
        groupid: None,
        label: None,
        localinvreqid: None,
        partid: None,
        payment_metadata: None,
        payment_secret: None,
        payment_hash: Sha256::from_str(&wrapper.payment_hash)?,
        route,
    })
    .await?;
    let res = rpc
        .call_typed(&WaitsendpayRequest {
            groupid: None,
            partid: None,
            timeout: Some(60),
            payment_hash: Sha256::from_str(&wrapper.payment_hash)?,
        })
        .await?;
    Ok(json!(res))
}
