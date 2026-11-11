//! Plugin options and the parsed configuration they produce.
//!
//! Price-ceiling tiers for a pair aren't configured here at all anymore --
//! that's `plugins/pricetiers-plugin`'s job (a separate, swappable pricing
//! policy plugin; see `protocol::fetch_price_tiers`). This plugin only
//! knows about the swap *mechanics*: who to route leg 1 to, and the CLTV
//! knobs around accepting it.

use anyhow::{anyhow, Error};
use cln_plugin::options::{DefaultIntegerConfigOption, StringArrayConfigOption};
use cln_rpc::primitives::PublicKey;
use std::collections::HashMap;
use std::path::PathBuf;

pub(crate) const OPT_PROVIDER: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "assetswap-provider",
    "A swap-provider node_id to route leg-1 asset payments to when paying a cross-asset \
     invoice this node can't pay directly. Repeatable; only the first is used in this \
     increment.",
);
pub(crate) const OPT_REMOTE: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "assetswap-remote",
    "asset:rpc-file. A swap provider pays leg 2 in this asset from another node of \
     its own, on another chain (e.g. eth:/path/to/evm-node/regtest/lightning-rpc), \
     instead of from this one. Repeatable, one per asset.",
);
pub(crate) const OPT_SAFETY_BLOCKS: DefaultIntegerConfigOption = DefaultIntegerConfigOption::new_i64_with_default(
    "assetswap-cltv-safety-blocks",
    18,
    "CLTV blocks a swap provider keeps as margin on leg 1 over leg 2's final CLTV \
     (default: mainnet's cltv_final)",
);
pub(crate) const OPT_MIN_HOP_BUDGET: DefaultIntegerConfigOption = DefaultIntegerConfigOption::new_i64_with_default(
    "assetswap-min-hop-budget",
    34,
    "Up-front CLTV sanity-gate budget before even attempting leg 2 \
     (default: mainnet's cltv_expiry_delta)",
);

#[derive(Clone, Debug)]
pub(crate) struct Config {
    pub(crate) providers: Vec<PublicKey>,
    pub(crate) safety_blocks: u32,
    pub(crate) min_hop_budget: u32,
    /// asset -> the RPC socket of the node that pays leg 2 in it, when that
    /// isn't this node (`assetswap-remote`).
    pub(crate) remotes: HashMap<String, PathBuf>,
}

pub(crate) fn parse_remotes(specs: &[String]) -> Result<HashMap<String, PathBuf>, Error> {
    let mut remotes = HashMap::new();
    for spec in specs {
        let (asset, path) = spec
            .split_once(':')
            .filter(|(a, p)| !a.is_empty() && !p.is_empty())
            .ok_or_else(|| anyhow!("assetswap-remote must be asset:rpc-file, got {spec:?}"))?;
        if remotes.insert(asset.to_string(), PathBuf::from(path)).is_some() {
            return Err(anyhow!("assetswap-remote: {asset} given twice"));
        }
    }
    Ok(remotes)
}
