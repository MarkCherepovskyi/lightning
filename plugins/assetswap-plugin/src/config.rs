//! Plugin options and the parsed configuration they produce.
//!
//! Price-ceiling tiers for a pair aren't configured here at all anymore --
//! that's `plugins/pricetiers-plugin`'s job (a separate, swappable pricing
//! policy plugin; see `protocol::fetch_price_tiers`). This plugin only
//! knows about the swap *mechanics*: who to route leg 1 to, and the CLTV
//! knobs around accepting it.

use cln_plugin::options::{DefaultIntegerConfigOption, StringArrayConfigOption};
use cln_rpc::primitives::PublicKey;

pub(crate) const OPT_PROVIDER: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "assetswap-provider",
    "A swap-provider node_id to route leg-1 asset payments to when paying a cross-asset \
     invoice this node can't pay directly. Repeatable; only the first is used in this \
     increment.",
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
}
