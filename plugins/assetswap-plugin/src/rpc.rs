//! Small helpers shared by every RPC method and hook handler: connecting
//! back to lightningd, reading call arguments, and converting askrene's
//! route shape into sendpay's.

use crate::State;
use cln_plugin::Plugin;
use cln_rpc::model::responses::GetroutesRoutesPath;
use cln_rpc::model::requests::SendpayRoute;
use cln_rpc::ClnRpc;
use serde_json::Value;
use std::path::{Path, PathBuf};

pub(crate) fn rpc_path(p: &Plugin<State>) -> PathBuf {
    Path::new(&p.configuration().lightning_dir).join(&p.configuration().rpc_file)
}

pub(crate) async fn new_rpc(p: &Plugin<State>) -> Result<ClnRpc, anyhow::Error> {
    ClnRpc::new(rpc_path(p)).await
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
