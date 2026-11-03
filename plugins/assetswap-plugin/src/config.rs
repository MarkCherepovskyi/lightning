//! Plugin options and the parsed configuration they produce.
//!
//! Price-ceiling tiers for a pair aren't configured here at all anymore --
//! that's `plugins/pricetiers-plugin`'s job (a separate, swappable pricing
//! policy plugin; see `protocol::fetch_price_tiers`). This plugin only
//! knows about the swap *mechanics*: who to ask for quotes, and the
//! custommsg/CLTV/expiry knobs around them.

use cln_plugin::options::{DefaultIntegerConfigOption, StringArrayConfigOption};
use cln_rpc::primitives::PublicKey;

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
pub(crate) struct Config {
    pub(crate) providers: Vec<PublicKey>,
    pub(crate) msgtype_request: u16,
    pub(crate) msgtype_reply: u16,
    pub(crate) safety_blocks: u32,
    pub(crate) min_hop_budget: u32,
    pub(crate) quote_expiry_secs: u64,
}
