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

pub(crate) const OPT_BLOCK_SECONDS: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "assetswap-block-seconds",
    "asset:seconds. The block time of the chain payments in this asset are made on, \
     overriding what is discovered from the node that makes or takes them (evm-status's \
     interval on an EVM node, else 60 on Liquid and 600 on Bitcoin). Leg 1's and leg 2's \
     CLTVs are compared by time, not block count. Repeatable, one per asset.",
);
pub(crate) const OPT_LEG2_MAXFEE_PPM: DefaultIntegerConfigOption = DefaultIntegerConfigOption::new_i64_with_default(
    "assetswap-leg2-maxfee-ppm",
    5000,
    "Routing fee a swap provider allows leg 2, in parts per million of its amount, on top \
     of assetswap-leg2-maxfee-base-msat; leg 1 must pay for it at the provider's price",
);
pub(crate) const OPT_LEG2_MAXFEE_BASE: DefaultIntegerConfigOption = DefaultIntegerConfigOption::new_i64_with_default(
    "assetswap-leg2-maxfee-base-msat",
    5000,
    "Fixed part of the routing fee a swap provider allows leg 2, in msat of leg 2's asset",
);

#[derive(Clone, Debug)]
pub(crate) struct Config {
    pub(crate) providers: Vec<PublicKey>,
    pub(crate) safety_blocks: u32,
    pub(crate) min_hop_budget: u32,
    /// asset -> the RPC socket of the node that pays leg 2 in it, when that
    /// isn't this node (`assetswap-remote`).
    pub(crate) remotes: HashMap<String, PathBuf>,
    /// asset -> seconds per block of its chain (`assetswap-block-seconds`).
    pub(crate) block_seconds: HashMap<String, u64>,
    pub(crate) leg2_maxfee_ppm: u64,
    pub(crate) leg2_maxfee_base_msat: u64,
}

/// A non-negative integer option, as the type the config keeps it in.
pub(crate) fn non_negative<T: TryFrom<i64>>(name: &str, v: i64) -> Result<T, Error> {
    T::try_from(v).map_err(|_| anyhow!("{name} must be a non-negative integer that fits, got {v}"))
}

pub(crate) fn parse_block_seconds(specs: &[String]) -> Result<HashMap<String, u64>, Error> {
    let mut out = HashMap::new();
    for spec in specs {
        let (asset, secs) = spec
            .rsplit_once(':')
            .filter(|(a, _)| !a.is_empty())
            .and_then(|(a, s)| s.parse::<u64>().ok().filter(|&s| s > 0).map(|s| (a, s)))
            .ok_or_else(|| anyhow!("assetswap-block-seconds must be asset:seconds (seconds > 0), got {spec:?}"))?;
        if out.insert(asset.to_string(), secs).is_some() {
            return Err(anyhow!("assetswap-block-seconds: {asset} given twice"));
        }
    }
    Ok(out)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn block_seconds_option() {
        let m = parse_block_seconds(&["eth:2".into(), "lbtc:60".into()]).unwrap();
        assert_eq!(m["eth"], 2);
        assert_eq!(m["lbtc"], 60);
        for bad in ["eth", "eth:", ":2", "eth:0", "eth:-1", "eth:x"] {
            assert!(parse_block_seconds(&[bad.into()]).is_err(), "{bad}");
        }
        assert!(parse_block_seconds(&["eth:2".into(), "eth:3".into()]).is_err());
    }

    #[test]
    fn non_negative_options() {
        assert_eq!(non_negative::<u32>("x", 18).unwrap(), 18);
        assert!(non_negative::<u32>("x", -1).is_err());
        assert!(non_negative::<u64>("x", -5).is_err());
    }
}

/// Were we started with `important-plugin=` (so lightningd stops if we
/// die)?  A provider must be: if it died while holding leg 1, lightningd
/// would carry on without its hook and fail leg 1, while leg 2 may still
/// get paid.  Compares `listconfigs`' important-plugin paths with our own.
pub(crate) fn is_important(listconfigs: &serde_json::Value, me: &std::path::Path) -> bool {
    let opt = &listconfigs["configs"]["important-plugin"];
    let mut paths: Vec<&str> = opt["values_str"]
        .as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str()).collect())
        .unwrap_or_default();
    if let Some(p) = opt["value_str"].as_str() {
        paths.push(p);
    }
    let me = me.canonicalize().unwrap_or_else(|_| me.to_path_buf());
    paths.iter().any(|p| {
        let p = std::path::Path::new(p);
        p.canonicalize().unwrap_or_else(|_| p.to_path_buf()) == me
    })
}

#[cfg(test)]
mod important_tests {
    use super::is_important;
    use serde_json::json;
    use std::path::Path;

    #[test]
    fn finds_ourselves_among_important_plugins() {
        let me = Path::new("/opt/plugins/assetswap-plugin");
        let cfg = json!({"configs": {"important-plugin": {
            "values_str": ["/opt/plugins/other", "/opt/plugins/assetswap-plugin"]}}});
        assert!(is_important(&cfg, me));
        let cfg = json!({"configs": {"important-plugin": {"values_str": ["/opt/plugins/other"]}}});
        assert!(!is_important(&cfg, me));
        assert!(!is_important(&json!({"configs": {}}), me));
    }
}
