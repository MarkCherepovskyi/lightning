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
mod config;
mod htlc;
mod pay;
mod protocol;
mod rpc;

use anyhow::{anyhow, Error};
use cln_plugin::{Builder, HookBuilder};
use cln_rpc::primitives::PublicKey;
use std::str::FromStr;

#[derive(Clone)]
struct State {
    config: config::Config,
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    log_panics::init();

    let Some(plugin) = Builder::new(tokio::io::stdin(), tokio::io::stdout())
        .dynamic()
        .option(config::OPT_PROVIDER)
        .option(config::OPT_REMOTE)
        .option(config::OPT_SAFETY_BLOCKS)
        .option(config::OPT_MIN_HOP_BUDGET)
        // Before contrib/assetchan's hook: a leg 1 that lands here in an
        // issued asset has no invoice of ours, so assetchan's final-hop
        // check would fail it as being in the wrong asset.
        .hook_from_builder(
            HookBuilder::new("htlc_accepted", htlc::on_htlc_accepted).before(vec!["assetchan.py".into()]),
        )
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

    let config = config::Config {
        providers,
        remotes,
        safety_blocks: plugin.option_str("assetswap-cltv-safety-blocks")?.and_then(|v| v.as_i64()).unwrap_or(18) as u32,
        min_hop_budget: plugin.option_str("assetswap-min-hop-budget")?.and_then(|v| v.as_i64()).unwrap_or(34) as u32,
    };

    let state = State { config };

    let plugin = plugin.start(state).await?;
    plugin.join().await?;
    Ok(())
}
