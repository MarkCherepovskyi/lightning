//! Connecting back to lightningd to persist price overrides in its own
//! datastore, so `setpricetier` survives a plugin/node restart instead of
//! reverting to whatever `pricetiers-pair` options were set at boot.
//!
//! Takes `lightning_dir`/`rpc_file` as plain strings rather than a
//! `Plugin<State>` - both the pre-start `ConfiguredPlugin` (used once, at
//! startup, to load persisted overrides before `State` even exists yet)
//! and the post-start `Plugin<State>` (used by `setpricetier`) expose the
//! same two fields via `.configuration()`, just as different concrete
//! types, so there's no single type to accept here without more generic
//! machinery than this is worth.

use cln_rpc::ClnRpc;
use std::path::Path;

pub(crate) async fn new_rpc(lightning_dir: &str, rpc_file: &str) -> Result<ClnRpc, anyhow::Error> {
    ClnRpc::new(Path::new(lightning_dir).join(rpc_file)).await
}

/// Every persisted price override lives under this prefix, keyed by
/// `to_asset` (the same uniqueness `pricetiers`'s own lookup already
/// assumes - at most one quotable pair per `to_asset`).
pub(crate) fn price_key(to_asset: &str) -> Vec<String> {
    vec!["pricetiers".into(), "pair".into(), to_asset.into()]
}

/// The bare prefix (no leaf), for `listdatastore` to return every
/// persisted override at once.
pub(crate) fn price_prefix() -> Vec<String> {
    vec!["pricetiers".into(), "pair".into()]
}
