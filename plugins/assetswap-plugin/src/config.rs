//! Plugin options and the parsed configuration they produce.

use anyhow::{anyhow, Error};
use cln_plugin::options::{DefaultIntegerConfigOption, StringArrayConfigOption};
use cln_rpc::primitives::PublicKey;

pub(crate) const OPT_PAIR: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "assetswap-pair",
    "A swap pair this node provides, as from_asset:to_asset:rate_ppm:fee_base_msat. \
     rate_ppm is how many msat of from_asset are required per 1_000_000 msat worth of \
     to_asset at today's reference price. Repeatable.",
);
pub(crate) const OPT_PROVIDER: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "assetswap-provider",
    "A swap-provider node_id to query for quotes when paying a cross-asset invoice \
     (must already be connected). Repeatable; only the first is used in this increment.",
);
pub(crate) const OPT_MSGTYPE_REQUEST: DefaultIntegerConfigOption = DefaultIntegerConfigOption::new_i64_with_default(
    "assetswap-msgtype-request",
    43213,
    "custommsg wire type for a swap quote request (must be odd)",
);
pub(crate) const OPT_MSGTYPE_REPLY: DefaultIntegerConfigOption = DefaultIntegerConfigOption::new_i64_with_default(
    "assetswap-msgtype-reply",
    43215,
    "custommsg wire type for a swap quote reply (must be odd)",
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
pub(crate) const OPT_QUOTE_EXPIRY: DefaultIntegerConfigOption = DefaultIntegerConfigOption::new_i64_with_default(
    "assetswap-quote-expiry-secs",
    120,
    "How long a swap-provider quote stays valid",
);

#[derive(Clone, Debug)]
pub(crate) struct SwapPair {
    pub(crate) from_asset: String,
    pub(crate) to_asset: String,
    pub(crate) rate_ppm: u64,
    pub(crate) fee_base_msat: u64,
}

pub(crate) fn parse_pair(s: &str) -> Result<SwapPair, Error> {
    let parts: Vec<&str> = s.split(':').collect();
    let [from_asset, to_asset, rate_ppm, fee_base_msat] = parts.as_slice() else {
        return Err(anyhow!(
            "assetswap-pair must be from_asset:to_asset:rate_ppm:fee_base_msat, got {s:?}"
        ));
    };
    Ok(SwapPair {
        from_asset: from_asset.to_string(),
        to_asset: to_asset.to_string(),
        rate_ppm: rate_ppm.parse()?,
        fee_base_msat: fee_base_msat.parse()?,
    })
}

#[derive(Clone, Debug)]
pub(crate) struct Config {
    pub(crate) pairs: Vec<SwapPair>,
    pub(crate) providers: Vec<PublicKey>,
    pub(crate) msgtype_request: u16,
    pub(crate) msgtype_reply: u16,
    pub(crate) safety_blocks: u32,
    pub(crate) min_hop_budget: u32,
    pub(crate) quote_expiry_secs: u64,
}

impl Config {
    /// The "fixed" price source: today's reference price for a pair is
    /// just whatever rate_ppm the operator configured for it. See the
    /// crate-level doc comment in `main.rs`.
    pub(crate) fn current_rate_ppm(&self, from_asset: &str, to_asset: &str) -> Option<u64> {
        self.pairs
            .iter()
            .find(|p| p.from_asset == from_asset && p.to_asset == to_asset)
            .map(|p| p.rate_ppm)
    }

    pub(crate) fn pair_for(&self, to_asset: &str) -> Option<&SwapPair> {
        self.pairs.iter().find(|p| p.to_asset == to_asset)
    }
}
