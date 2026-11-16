//! The `assetbalances` RPC method: how much of each asset this node
//! actually holds, summed from its own channels. Liquid on-chain UTXOs
//! aren't asset-tagged by `listfunds` on this fork (see `doc/schemas/
//! listfunds.json` — no `asset` field), so this is channel balance only;
//! an ordinary L-BTC channel groups under `"lbtc"`, same default
//! `rpc.rs::our_channel_assets` already uses.

use crate::rpc::new_rpc;
use anyhow::Error;
use cln_plugin::Plugin;
use serde_json::{json, Value};

use crate::State;

pub(crate) async fn assetbalances(p: Plugin<State>, _args: Value) -> Result<Value, Error> {
    let mut rpc = new_rpc(&p).await?;
    let res: Value = rpc.call_raw("listpeerchannels", &json!({})).await?;

    let mut by_asset: std::collections::BTreeMap<String, u64> = std::collections::BTreeMap::new();
    for c in res["channels"].as_array().into_iter().flatten() {
        let asset = c["asset"].as_str().unwrap_or("lbtc").to_string();
        let to_us_msat = c["to_us_msat"].as_u64().unwrap_or(0);
        *by_asset.entry(asset).or_insert(0) += to_us_msat;
    }

    let balances: Vec<Value> = by_asset
        .into_iter()
        .map(|(asset, channel_balance_msat)| {
            json!({
                "asset": asset,
                "channel_balance_msat": channel_balance_msat,
                // Always 0: Liquid on-chain outputs aren't asset-tagged by
                // listfunds on this fork (see module doc comment above).
                "onchain_balance_msat": 0,
            })
        })
        .collect();

    Ok(json!({ "balances": balances }))
}
