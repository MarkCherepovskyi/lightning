//! Small helpers shared by every RPC method and hook handler: connecting
//! back to lightningd (ours, or the node that pays leg 2 for us), reading
//! call arguments, converting askrene's route shape into sendpay's, and
//! keeping a payment in one asset on channels in that asset.

use crate::protocol::is_asset_id;
use crate::State;
use anyhow::anyhow;
use cln_plugin::Plugin;
use cln_rpc::model::requests::SendpayRoute;
use cln_rpc::model::responses::GetroutesRoutesPath;
use cln_rpc::ClnRpc;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// JSON-RPC "method not found": the node has no contrib/assetchan.
const METHOD_NOT_FOUND: i32 = -32601;

pub(crate) fn rpc_path(p: &Plugin<State>) -> PathBuf {
    Path::new(&p.configuration().lightning_dir).join(&p.configuration().rpc_file)
}

pub(crate) async fn new_rpc(p: &Plugin<State>) -> Result<ClnRpc, anyhow::Error> {
    ClnRpc::new(rpc_path(p)).await
}

/// The node that pays (and has paid) leg 2 in `asset`: the one
/// `assetswap-remote` names for it, else this one.
pub(crate) async fn leg2_rpc(p: &Plugin<State>, asset: &str) -> Result<ClnRpc, anyhow::Error> {
    match p.state().config.remotes.get(asset) {
        Some(path) => ClnRpc::new(path)
            .await
            .map_err(|e| anyhow!("can't reach the node paying in {asset} at {}: {e}", path.display())),
        None => new_rpc(p).await,
    }
}

/// Read a parameter by position (array-style call) or name (object-style
/// call) — the same dual convention `plugins/currencyrate-plugin` uses.
pub(crate) fn param<'a>(args: &'a Value, pos: usize, name: &str) -> Option<&'a Value> {
    match args {
        Value::Array(a) => a.get(pos),
        Value::Object(m) => m.get(name),
        _ => None,
    }
}

pub(crate) fn getroutes_path_to_sendpay_route(path: &[GetroutesRoutesPath]) -> Vec<SendpayRoute> {
    path.iter()
        .map(|hop| SendpayRoute {
            amount_msat: hop.amount_out_msat,
            amount_out_msat: hop.amount_out_msat,
            channel: Some(hop.short_channel_id_dir.short_channel_id),
            cltv_out: hop.cltv_out,
            delay: hop.cltv_out,
            id: hop.node_id_out,
            node_id_out: hop.node_id_out,
            short_channel_id_dir: Some(hop.short_channel_id_dir),
        })
        .collect()
}

/// The asset of each of our channels, by every name askrene may know it by
/// (scid and local alias); "lbtc" for an ordinary one, as assetchan says.
async fn our_channel_assets(rpc: &mut ClnRpc) -> Result<Vec<(String, String)>, anyhow::Error> {
    let res: Value = rpc.call_raw("listpeerchannels", &json!({})).await?;
    let mut out = Vec::new();
    for c in res["channels"].as_array().into_iter().flatten() {
        let asset = c["asset"].as_str().unwrap_or("lbtc").to_string();
        for scid in [&c["short_channel_id"], &c["alias"]["local"]] {
            if let Some(s) = scid.as_str() {
                out.push((s.to_string(), asset.clone()));
            }
        }
    }
    Ok(out)
}

/// The asset of our channel `scid` (or local alias): `None` for one in the
/// native coin, else the issued asset's id.
pub(crate) async fn channel_asset(rpc: &mut ClnRpc, scid: &str) -> Result<Option<String>, anyhow::Error> {
    let res: Value = rpc.call_raw("listpeerchannels", &json!({})).await?;
    let chan = res["channels"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|c| c["short_channel_id"].as_str() == Some(scid) || c["alias"]["local"].as_str() == Some(scid))
        .ok_or_else(|| anyhow!("no channel {scid}"))?;
    Ok(chan["asset"].as_str().map(str::to_string))
}

/// askrene layers that keep a payment in `asset` on channels in that
/// asset. A node without contrib/assetchan has only native-coin channels:
/// none needed (and an issued asset can't be paid there). With it, the
/// layer `assetswap.<asset>` disables every channel assetchan knows to be
/// in another asset: ours, and the gossiped ones it classifies by their
/// funding output -- what `assetchan-pay` does for its own layer, done here
/// because leg 1 is a `getroutes` + `sendpay`, not an `xpay`.
pub(crate) async fn asset_layers(rpc: &mut ClnRpc, asset: &str) -> Result<Vec<String>, anyhow::Error> {
    let gossiped: Value = match rpc.call_raw("assetchan-listchannels", &json!({})).await {
        Ok(v) => v,
        Err(e) if e.code == Some(METHOD_NOT_FOUND) => {
            if is_asset_id(asset) {
                return Err(anyhow!("paying in asset {asset} needs contrib/assetchan on this node"));
            }
            return Ok(vec![]);
        }
        Err(e) => return Err(anyhow!("assetchan-listchannels: {e}")),
    };

    let layer = format!("assetswap.{asset}");
    // Fails if it exists already, which is fine: it's ours, and disabling
    // a channel again is a no-op.
    let _ = rpc
        .call_raw::<Value, _>("askrene-create-layer", &json!({"layer": layer, "persistent": false}))
        .await;

    let mut other: Vec<String> = our_channel_assets(rpc)
        .await?
        .into_iter()
        .filter(|(_, a)| a != asset)
        .map(|(scid, _)| scid)
        .collect();
    for c in gossiped["channels"].as_array().into_iter().flatten() {
        // A channel assetchan can't classify (null) is left alone, as it does.
        if let (Some(scid), Some(a)) = (c["short_channel_id"].as_str(), c["asset"].as_str()) {
            if a != asset {
                other.push(scid.to_string());
            }
        }
    }
    other.sort();
    other.dedup();
    for scid in other {
        for dir in 0..2 {
            rpc.call_raw::<Value, _>(
                "askrene-update-channel",
                &json!({"layer": layer, "short_channel_id_dir": format!("{scid}/{dir}"), "enabled": false}),
            )
            .await
            .map_err(|e| anyhow!("askrene-update-channel {scid}/{dir}: {e}"))?;
        }
    }
    Ok(vec![layer])
}
