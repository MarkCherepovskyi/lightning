//! The `quoteassetinvoice` and `payassetinvoice` RPC methods (payer side).
//! Neither one negotiates with the provider ahead of time -- there's no
//! request/reply round trip, so no requirement that this node be directly
//! connected to it. `payassetinvoice` tries a direct, same-asset payment
//! first; failing that, it decides how much to send for leg 1 either from
//! an explicit (`from_asset`, `from_amount_msat`) the caller already knows
//! (e.g. a harness with cross-node visibility, like
//! contrib/liquid-poc/lqui.py), or by pricing it from this node's own
//! local `pricetiers-plugin` -- an estimate, not a commitment, since the
//! provider makes the real decision live when leg 1 actually lands, in
//! `htlc.rs`. Either way it sends leg 1 with the invoice packed into its
//! `payment_metadata`, so the provider learns what to pay for leg 2 from
//! the HTLC itself. `quoteassetinvoice` runs just the local-pricing half,
//! for a caller that wants an estimate before deciding to pay -- it only
//! works if this node happens to hold the same `pricetiers-pair=` config
//! as the provider (see its own doc comment).

use crate::assetinvoice::decode_wrapper;
use crate::protocol::{asset_sha, encode_swap_metadata, fetch_price_tiers, SwapMetadata};
use crate::rpc::{getroutes_path_to_sendpay_route, new_rpc, param};
use crate::State;
use anyhow::{anyhow, Error};
use cln_plugin::Plugin;
use cln_rpc::model::requests::{
    DecodeRequest, GetinfoRequest, GetroutesRequest, SendpayRequest, WaitsendpayRequest,
    XpayRequest,
};
use cln_rpc::primitives::{Amount, Sha256};
use serde_json::{json, Value};
use std::str::FromStr;

/// CLTV blocks requested for leg 1 over the invoice's own
/// `min_final_cltv_expiry`, covering the provider's default
/// `assetswap-cltv-safety-blocks` (18) + `assetswap-min-hop-budget` (34) +
/// slack. Not negotiated -- if a provider configured with a bigger budget
/// rejects it, the payer just needs to retry with more (same failure mode
/// as sending too little for the price).
const LEG1_CLTV_MARGIN: u32 = 60;

/// Ask this node's own `pricetiers-plugin` what paying `assetinvoice` would
/// cost, for display purposes. This is NOT a quote from the swap provider
/// -- it only returns real numbers if this node happens to carry the same
/// `pricetiers-pair=` config the provider uses (set deliberately, e.g. by
/// the harness that also set up the provider), since there's no other way
/// for a payer to learn a remote provider's price without asking it, and
/// asking it is exactly the round trip this design avoids. If this node
/// has no matching config, it fails the same way an unsupported pair
/// would.
pub(crate) async fn quoteassetinvoice(p: Plugin<State>, args: Value) -> Result<Value, Error> {
    let encoded = param(&args, 0, "assetinvoice")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing assetinvoice"))?;
    let wrapper = decode_wrapper(encoded)?;

    let mut rpc = new_rpc(&p).await?;
    let priced = fetch_price_tiers(&mut rpc, &wrapper.asset, wrapper.amount_msat).await?;
    Ok(json!(priced))
}

pub(crate) async fn payassetinvoice(p: Plugin<State>, args: Value) -> Result<Value, Error> {
    let encoded = param(&args, 0, "assetinvoice")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing assetinvoice"))?;
    let tier_name = param(&args, 1, "tier")
        .and_then(Value::as_str)
        .unwrap_or("medium")
        .to_string();
    // An explicit (from_asset, from_amount_msat) lets a caller that already
    // knows the provider's rate -- e.g. contrib/liquid-poc/lqui.py, which
    // reads every node's config directly rather than this node's own --
    // skip the local pricetiers-plugin lookup below entirely. Most payers
    // won't have a matching `pricetiers-pair=` of their own (that config
    // means "I'm a provider for this pair", which a plain payer isn't), so
    // without this the swap fallback would only ever work for a node that
    // happens to also hold that config.
    let from_override = match (
        param(&args, 2, "from_asset").and_then(Value::as_str),
        param(&args, 3, "from_amount_msat").and_then(Value::as_u64),
    ) {
        (Some(a), Some(m)) => Some((a.to_string(), m)),
        _ => None,
    };
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

    // No negotiation: decide how much to send for leg 1 either from the
    // caller's own explicit override, or -- failing that -- by pricing it
    // from this node's own local pricetiers-plugin (an estimate; see this
    // function's own doc comment and quoteassetinvoice's). Either way the
    // provider makes the real, binding call live when the HTLC lands
    // (htlc.rs).
    let (from_asset, from_amount_msat) = if let Some((a, m)) = from_override {
        (a, m)
    } else {
        let priced = fetch_price_tiers(&mut rpc, &wrapper.asset, wrapper.amount_msat)
            .await
            .map_err(|e| anyhow!("direct payment failed ({direct_err}); can't price a swap locally: {e}"))?;
        let tier = priced
            .tiers
            .iter()
            .find(|t| t.name == tier_name)
            .ok_or_else(|| anyhow!("no {tier_name:?} tier for this pair"))?;
        (priced.from_asset, tier.from_amount_msat)
    };

    let decoded = rpc
        .call_typed(&DecodeRequest {
            string: wrapper.bolt11.clone(),
        })
        .await?;
    let leg2_final_cltv = decoded
        .min_final_cltv_expiry
        .ok_or_else(|| anyhow!("invoice has no min_final_cltv_expiry"))?;
    let final_cltv = leg2_final_cltv + LEG1_CLTV_MARGIN;

    let my_id = rpc.call_typed(&GetinfoRequest {}).await?.id;
    let maxfee_msat = from_amount_msat / 100; // 1% leg-1 routing-fee budget
    let routes = rpc
        .call_typed(&GetroutesRequest {
            source: my_id,
            destination: provider,
            amount_msat: Amount::from_msat(from_amount_msat),
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

    // What the provider needs to settle leg 2, packed into leg 1 itself --
    // no separate message, so no requirement that we be directly connected
    // to it. The payment_hash below (reused from the FINAL recipient's
    // invoice, not a fresh one) is the hash-link the whole swap's
    // atomicity rests on.
    let metadata = encode_swap_metadata(&SwapMetadata {
        final_bolt11: wrapper.bolt11.clone(),
        to_asset: wrapper.asset.clone(),
    })?;
    rpc.call_typed(&SendpayRequest {
        amount_msat: Some(Amount::from_msat(from_amount_msat)),
        bolt11: None,
        description: None,
        groupid: None,
        label: None,
        localinvreqid: None,
        partid: None,
        payment_metadata: Some(metadata),
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
