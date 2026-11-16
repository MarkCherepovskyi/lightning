//! Plugin options and the parsed configuration they produce.
//!
//! Price-ceiling tiers for a pair aren't configured here at all anymore --
//! that's `plugins/pricetiers-plugin`'s job (a separate, swappable pricing
//! policy plugin; see `protocol::fetch_price_tiers`). This plugin only
//! knows about the swap *mechanics*: who to route leg 1 to, and the CLTV
//! knobs around accepting it.

use anyhow::{anyhow, Error};
use cln_plugin::options::{DefaultIntegerConfigOption, DefaultStringConfigOption, StringArrayConfigOption};
use cln_rpc::primitives::PublicKey;
use std::collections::HashMap;
use std::path::PathBuf;

/// Which half of a cross-asset swap this node plays. Mutually exclusive:
/// [`Mode::Server`] (the shared swap-provider node, e.g. `cln-swap`) can
/// only settle leg-1 HTLCs via [`crate::htlc::on_htlc_accepted`] - its
/// `assetinvoice`/`quoteassetinvoice`/`payassetinvoice`/`assetbalances` RPCs
/// are disabled, since a provider never creates its own asset invoices or
/// pays anyone else's. [`Mode::Client`] (every ordinary per-user node) is
/// the reverse: those RPCs work, but `on_htlc_accepted` always passes a
/// would-be leg-1 HTLC straight through with `"continue"` rather than
/// attempting to settle it - an ordinary node never acts as someone else's
/// swap provider.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Mode {
    Client,
    Server,
}

impl Mode {
    fn parse(raw: &str) -> Result<Self, Error> {
        match raw {
            "client" => Ok(Self::Client),
            "server" => Ok(Self::Server),
            other => Err(anyhow!("assetswap-mode must be \"client\" or \"server\", got {other:?}")),
        }
    }
}

/// Defaults to "client": the vast majority of nodes are ordinary per-user
/// ones, and an ordinary node left in server mode by mistake would be far
/// more dangerous (silently trying to settle other people's leg-1 HTLCs)
/// than the reverse (a provider left in client mode just can't create/pay
/// its own asset invoices, which it has no reason to do anyway). The one
/// node that needs `server` explicitly set is the shared swap-provider
/// (e.g. `cln-swap` - see infra/images/cln/entrypoint.sh's `ASSETSWAP_MODE`).
pub(crate) const OPT_MODE: DefaultStringConfigOption = DefaultStringConfigOption::new_str_with_default(
    "assetswap-mode",
    "client",
    "\"client\" (default - create/pay asset invoices, read balances) or \"server\" \
     (the shared swap-provider node - only settles leg-1 HTLCs via htlc_accepted, \
     nothing else).",
);

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
    pub(crate) mode: Mode,
    pub(crate) providers: Vec<PublicKey>,
    pub(crate) safety_blocks: u32,
    pub(crate) min_hop_budget: u32,
    /// asset -> the RPC socket of the node that pays leg 2 in it, when that
    /// isn't this node (`assetswap-remote`).
    pub(crate) remotes: HashMap<String, PathBuf>,
}

pub(crate) fn parse_mode(raw: &str) -> Result<Mode, Error> {
    Mode::parse(raw)
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
