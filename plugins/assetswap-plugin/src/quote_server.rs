//! Swap-provider side of the quote protocol: the `custommsg` hook that
//! receives a quote request, decodes the final invoice, prices three tiers,
//! records the quote, and replies — plus dispatching a received quote
//! *reply* back to whichever `payassetinvoice` call is waiting on it.

use crate::protocol::{
    decode_custommsg, encode_custommsg, make_tiers, now_unix, quote_key, QuoteReply, QuoteRecord,
    QuoteRequest,
};
use crate::rpc::new_rpc;
use crate::State;
use anyhow::{anyhow, Error};
use cln_plugin::Plugin;
use cln_rpc::model::requests::{DatastoreMode, DatastoreRequest, DecodeRequest, SendcustommsgRequest};
use cln_rpc::primitives::PublicKey;
use serde_json::{json, Value};
use std::str::FromStr;

async fn handle_quote_request(p: &Plugin<State>, peer_id: PublicKey, req: QuoteRequest) {
    if let Err(e) = handle_quote_request_inner(p, peer_id, &req).await {
        log::warn!("assetswap: quote request from {peer_id} failed: {e}");
    }
}

async fn handle_quote_request_inner(
    p: &Plugin<State>,
    peer_id: PublicKey,
    req: &QuoteRequest,
) -> Result<(), Error> {
    let mut rpc = new_rpc(p).await?;
    let reply_type = p.state().config.msgtype_reply;

    let decoded = rpc
        .call_typed(&DecodeRequest {
            string: req.final_bolt11.clone(),
        })
        .await?;

    let Some(pair) = p.state().config.pair_for(&req.to_asset) else {
        let reply = QuoteReply {
            req_id: req.req_id.clone(),
            ok: false,
            reason: Some("unsupported asset pair".into()),
            from_asset: None,
            to_asset: None,
            payment_hash: None,
            tiers: None,
            quote_expiry_unix: None,
            min_leg1_cltv: None,
        };
        let msg = encode_custommsg(reply_type, &reply)?;
        rpc.call_typed(&SendcustommsgRequest { node_id: peer_id, msg }).await?;
        return Ok(());
    };

    let to_amount_msat = decoded
        .amount_msat
        .ok_or_else(|| anyhow!("invoice has no amount"))?
        .msat();
    let payment_hash = decoded
        .payment_hash
        .ok_or_else(|| anyhow!("invoice has no payment_hash"))?
        .to_string();
    let leg2_final_cltv = decoded
        .min_final_cltv_expiry
        .ok_or_else(|| anyhow!("invoice has no min_final_cltv_expiry"))?;

    let tiers = make_tiers(pair, to_amount_msat);
    let expiry = now_unix() + p.state().config.quote_expiry_secs;
    // +1 block of slack over the hook's own exact gate, so routing-time
    // rounding doesn't put leg 1 exactly on the boundary.
    let min_leg1_cltv =
        leg2_final_cltv + p.state().config.safety_blocks + p.state().config.min_hop_budget + 1;

    let record = QuoteRecord {
        state: "quoted".into(),
        from_asset: pair.from_asset.clone(),
        to_asset: pair.to_asset.clone(),
        final_bolt11: req.final_bolt11.clone(),
        quote_expiry_unix: expiry,
        leg2_final_cltv,
        tiers: tiers.clone(),
    };
    rpc.call_typed(&DatastoreRequest {
        generation: None,
        hex: None,
        mode: Some(DatastoreMode::CREATE_OR_REPLACE),
        string: Some(serde_json::to_string(&record)?),
        key: quote_key(&payment_hash),
    })
    .await?;

    let reply = QuoteReply {
        req_id: req.req_id.clone(),
        ok: true,
        reason: None,
        from_asset: Some(pair.from_asset.clone()),
        to_asset: Some(pair.to_asset.clone()),
        payment_hash: Some(payment_hash),
        tiers: Some(tiers),
        quote_expiry_unix: Some(expiry),
        min_leg1_cltv: Some(min_leg1_cltv),
    };
    let msg = encode_custommsg(reply_type, &reply)?;
    rpc.call_typed(&SendcustommsgRequest { node_id: peer_id, msg }).await?;
    Ok(())
}

pub(crate) async fn on_custommsg(p: Plugin<State>, v: Value) -> Result<Value, Error> {
    let peer_id = v.get("peer_id").and_then(Value::as_str).and_then(|s| PublicKey::from_str(s).ok());
    let payload_hex = v.get("payload").and_then(Value::as_str).unwrap_or_default();

    if let (Some(peer_id), Ok((msgtype, body))) = (peer_id, decode_custommsg(payload_hex)) {
        if msgtype == p.state().config.msgtype_request {
            if let Ok(req) = serde_json::from_slice::<QuoteRequest>(&body) {
                let p2 = p.clone();
                tokio::spawn(async move { handle_quote_request(&p2, peer_id, req).await });
            }
        } else if msgtype == p.state().config.msgtype_reply {
            if let Ok(reply) = serde_json::from_slice::<QuoteReply>(&body) {
                if let Some(tx) = p.state().pending_quotes.lock().await.remove(&reply.req_id) {
                    let _ = tx.send(reply);
                }
            }
        }
    }
    Ok(json!({"result": "continue"}))
}
