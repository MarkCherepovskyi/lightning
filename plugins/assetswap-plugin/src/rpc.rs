//! Small helpers shared by every RPC method and hook handler: connecting
//! back to lightningd (ours, or the node that pays leg 2 for us), reading
//! call arguments, converting askrene's route shape into sendpay's, and
//! keeping a payment in one asset on channels in that asset.

use crate::protocol::{check_asset, is_asset_id};
use crate::State;
use anyhow::anyhow;
use cln_plugin::Plugin;
use cln_rpc::model::requests::SendpayRoute;
use cln_rpc::model::responses::GetroutesRoutesPath;
use cln_rpc::ClnRpc;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

/// JSON-RPC "method not found": the node has no contrib/assetchan.
pub(crate) const METHOD_NOT_FOUND: i32 = -32601;

pub(crate) fn rpc_path(p: &Plugin<State>) -> PathBuf {
    Path::new(&p.configuration().lightning_dir).join(&p.configuration().rpc_file)
}

pub(crate) async fn new_rpc(p: &Plugin<State>) -> Result<ClnRpc, anyhow::Error> {
    ClnRpc::new(rpc_path(p)).await
}

/// The RPC socket of the node that pays (and has paid) leg 2 in `asset`:
/// the one `assetswap-remote` names for it, else this one's.
pub(crate) fn leg2_path(p: &Plugin<State>, asset: &str) -> PathBuf {
    match p.state().config.remotes.get(asset) {
        Some(path) => path.clone(),
        None => rpc_path(p),
    }
}

pub(crate) async fn leg2_rpc(p: &Plugin<State>, asset: &str) -> Result<ClnRpc, anyhow::Error> {
    let path = leg2_path(p, asset);
    ClnRpc::new(&path)
        .await
        .map_err(|e| anyhow!("can't reach the node paying in {asset} at {}: {e}", path.display()))
}

/// A node's `getinfo`, untyped (only `id`, `network` and `blockheight`
/// are read from it).
pub(crate) async fn getinfo(rpc: &mut ClnRpc) -> Result<Value, anyhow::Error> {
    rpc.call_raw("getinfo", &json!({})).await.map_err(|e| anyhow!("getinfo: {e}"))
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
/// asset: the one contrib/assetchan keeps for it (`assetchan-layer`, in
/// which every channel not in `asset` is disabled, ours by scid and alias
/// and gossiped ones alike). A node without assetchan (an EVM node, or a
/// Liquid one that never opens asset channels) has only native-coin
/// channels: no layer needed, and an issued asset can't be paid there.
/// An asset string that is neither an asset id nor a known native coin
/// label is refused rather than guessed at.
pub(crate) async fn asset_layers(rpc: &mut ClnRpc, asset: &str) -> Result<Vec<String>, anyhow::Error> {
    check_asset(asset)?;
    match rpc.call_raw::<Value, _>("assetchan-layer", &json!({"asset": asset})).await {
        Ok(v) => {
            let layer = v["layer"]
                .as_str()
                .filter(|l| !l.is_empty())
                .ok_or_else(|| anyhow!("assetchan-layer {asset} returned no layer"))?;
            Ok(vec![layer.to_string()])
        }
        Err(e) if e.code == Some(METHOD_NOT_FOUND) => {
            if is_asset_id(asset) {
                return Err(anyhow!("paying in asset {asset} needs contrib/assetchan on this node"));
            }
            Ok(vec![])
        }
        Err(e) => Err(anyhow!("assetchan-layer {asset}: {e}")),
    }
}
