//! Pay an invoice that requires a different Liquid asset than you hold.
//!
//! Design: contrib/liquid-poc/CROSS_ASSET_SWAP.md.
//!
//! No pre-negotiated quote: the `assetinvoice` wrapper (`assetinvoice.rs`)
//! carries the invoice and the asset it requires; `payassetinvoice`
//! (`pay.rs`) tries a direct, same-asset payment first, and falling back
//! to a swap, prices it from this node's own local `pricetiers-plugin`
//! and sends leg 1 (a `sendpay` using the final recipient's own
//! payment_hash) with that invoice packed into its `payment_metadata`.
//! The swap-provider's `htlc_accepted` hook (`htlc.rs`) decodes that
//! metadata, prices it live, and either atomically claims it and runs leg
//! 2 via `xpay`, resolving leg 1 with the resulting preimage, or fails
//! leg 1 outright if it isn't profitable at the provider's current price.
//! No direct peer connection between payer and provider is required --
//! leg 1 is routed like any other payment.
//!
//! Pricing (which pairs are quotable, at what rate, with what tier
//! margins) isn't this plugin's job at all -- that's
//! `plugins/pricetiers-plugin`'s, a separate, swappable policy plugin
//! this one calls via a plain local RPC call (`protocol::fetch_price_tiers`).

mod assetinvoice;
mod cltv;
mod config;
mod htlc;
mod pay;
mod protocol;
mod rpc;

use anyhow::{anyhow, Error};
use cln_plugin::{Builder, HookBuilder};
use cln_rpc::primitives::PublicKey;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::str::FromStr;
use std::sync::{Arc, Mutex};

#[derive(Clone)]
struct State {
    config: config::Config,
    /// Seconds per block of each node asked (by RPC socket), found once.
    block_secs: Arc<Mutex<HashMap<PathBuf, u64>>>,
    /// payment_hashes a live hook call of this process has claimed.
    inflight: Arc<Mutex<HashSet<String>>>,
    /// Do we take new swaps (as a provider)?  Only as an important
    /// plugin; settling swaps already under way doesn't depend on it.
    provider: bool,
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    log_panics::init();

    // Not dynamic: a provider must be loaded (from its config file) before
    // lightningd replays a held leg 1 after a restart, and must not be
    // stoppable mid-swap -- `plugin stop` would make lightningd continue
    // the hook and fail leg 1 while leg 2 may still be in flight.
    let Some(plugin) = Builder::new(tokio::io::stdin(), tokio::io::stdout())
        .option(config::OPT_PROVIDER)
        .option(config::OPT_REMOTE)
        .option(config::OPT_SAFETY_BLOCKS)
        .option(config::OPT_MIN_HOP_BUDGET)
        .option(config::OPT_BLOCK_SECONDS)
        .option(config::OPT_LEG2_MAXFEE_PPM)
        .option(config::OPT_LEG2_MAXFEE_BASE)
        // No ordering against contrib/assetchan's htlc_accepted hook:
        // assetchan only enforces its final-hop asset check on HTLCs for an
        // invoice of its own, and a leg 1 has none here, so whichever hook
        // runs first, a leg 1 reaches this one.
        .hook_from_builder(HookBuilder::new("htlc_accepted", htlc::on_htlc_accepted))
        .rpcmethod(
            "assetinvoice",
            "Create an invoice and package it with the asset it requires \
             into one self-contained string",
            assetinvoice::assetinvoice,
        )
        .rpcmethod(
            "quoteassetinvoice",
            "Estimate what paying an assetinvoice string would cost, from \
             this node's own local pricetiers-plugin config, without \
             paying it",
            pay::quoteassetinvoice,
        )
        .rpcmethod(
            "payassetinvoice",
            "Pay an assetinvoice string, even if it requires an asset you \
             don't directly hold (via a swap-provider node)",
            pay::payassetinvoice,
        )
        .configure()
        .await?
    else {
        return Ok(());
    };

    let providers: Vec<PublicKey> = plugin
        .option_str("assetswap-provider")?
        .and_then(|v| v.as_str_arr().cloned())
        .unwrap_or_default()
        .iter()
        .map(|s| PublicKey::from_str(s).map_err(|e| anyhow!("bad assetswap-provider {s}: {e}")))
        .collect::<Result<_, _>>()?;

    let remotes = config::parse_remotes(
        &plugin
            .option_str("assetswap-remote")?
            .and_then(|v| v.as_str_arr().cloned())
            .unwrap_or_default(),
    )?;

    let block_seconds = config::parse_block_seconds(
        &plugin
            .option_str("assetswap-block-seconds")?
            .and_then(|v| v.as_str_arr().cloned())
            .unwrap_or_default(),
    )?;

    let int = |name: &str, default: i64| -> Result<i64, Error> {
        Ok(plugin.option_str(name)?.and_then(|v| v.as_i64()).unwrap_or(default))
    };
    let config = config::Config {
        providers,
        remotes,
        block_seconds,
        safety_blocks: config::non_negative("assetswap-cltv-safety-blocks", int("assetswap-cltv-safety-blocks", 18)?)?,
        min_hop_budget: config::non_negative("assetswap-min-hop-budget", int("assetswap-min-hop-budget", 34)?)?,
        leg2_maxfee_ppm: config::non_negative("assetswap-leg2-maxfee-ppm", int("assetswap-leg2-maxfee-ppm", 5000)?)?,
        leg2_maxfee_base_msat: config::non_negative(
            "assetswap-leg2-maxfee-base-msat",
            int("assetswap-leg2-maxfee-base-msat", 5000)?,
        )?,
    };

    // Only an important plugin takes new swaps (config::is_important).
    let rpc_file = std::path::Path::new(&plugin.configuration().lightning_dir)
        .join(&plugin.configuration().rpc_file);
    let provider = async {
        let mut rpc = cln_rpc::ClnRpc::new(&rpc_file).await?;
        let cfg: serde_json::Value = rpc
            .call_raw("listconfigs", &serde_json::json!({"config": "important-plugin"}))
            .await
            .map_err(|e| anyhow!("listconfigs: {e}"))?;
        Ok::<_, Error>(config::is_important(&cfg, &std::env::current_exe()?))
    }
    .await
    .unwrap_or_else(|e| {
        log::warn!("assetswap: can't tell whether we're an important plugin ({e}): taking no new swaps");
        false
    });
    if !provider {
        log::warn!(
            "assetswap: not started with important-plugin=: paying and settling earlier swaps \
             only, no new swaps as a provider (if this plugin died while holding a leg 1, \
             lightningd would fail it while leg 2 may still be paid)"
        );
    }

    let state = State {
        config,
        block_secs: Arc::new(Mutex::new(HashMap::new())),
        inflight: Arc::new(Mutex::new(HashSet::new())),
        provider,
    };

    let plugin = plugin.start(state).await?;
    plugin.join().await?;
    Ok(())
}
