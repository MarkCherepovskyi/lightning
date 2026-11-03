//! Pay an invoice that requires a different Liquid asset than you hold.
//!
//! Design: contrib/liquid-poc/CROSS_ASSET_SWAP.md.
//!
//! This increment implements the full happy path (steps 1-6 of that doc's
//! implementation sequence): the `assetinvoice` wrapper (`assetinvoice.rs`),
//! `payassetinvoice`'s same-asset fallback AND its swap path (`pay.rs`: the
//! `custommsg` quote protocol in `quote_server.rs`, with three
//! price-ceiling tiers, then a `sendpay` leg 1 using the final recipient's
//! own payment_hash), and the swap-provider's `htlc_accepted` hold/resolve
//! hook (`htlc.rs`: validate, atomically claim the quote, run leg 2 via
//! `xpay`, resolve leg 1 with the resulting preimage or fail it).
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
mod quote_server;
mod rpc;

use anyhow::{anyhow, Error};
use cln_plugin::Builder;
use cln_rpc::primitives::PublicKey;
use protocol::QuoteReply;
use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;
use tokio::sync::{oneshot, Mutex as AsyncMutex};

type PendingQuotes = Arc<AsyncMutex<HashMap<String, oneshot::Sender<QuoteReply>>>>;

#[derive(Clone)]
struct State {
    config: config::Config,
    pending_quotes: PendingQuotes,
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    log_panics::init();

    let Some(plugin) = Builder::new(tokio::io::stdin(), tokio::io::stdout())
        .dynamic()
        .option(config::OPT_PROVIDER)
        .option(config::OPT_MSGTYPE_REQUEST)
        .option(config::OPT_MSGTYPE_REPLY)
        .option(config::OPT_SAFETY_BLOCKS)
        .option(config::OPT_MIN_HOP_BUDGET)
        .option(config::OPT_QUOTE_EXPIRY)
        .hook("htlc_accepted", htlc::on_htlc_accepted)
        .hook("custommsg", quote_server::on_custommsg)
        .rpcmethod(
            "assetinvoice",
            "Create an invoice and package it with the asset it requires \
             into one self-contained string",
            assetinvoice::assetinvoice,
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
    let msgtype_request = plugin.option_str("assetswap-msgtype-request")?.and_then(|v| v.as_i64()).unwrap_or(43213) as u16;
    let msgtype_reply = plugin.option_str("assetswap-msgtype-reply")?.and_then(|v| v.as_i64()).unwrap_or(43215) as u16;
    if msgtype_request % 2 == 0 || msgtype_reply % 2 == 0 {
        return plugin
            .disable("assetswap-msgtype-request/-reply must both be odd (BOLT1 'it's ok to be odd')")
            .await;
    }

    let config = config::Config {
        providers,
        msgtype_request,
        msgtype_reply,
        safety_blocks: plugin.option_str("assetswap-cltv-safety-blocks")?.and_then(|v| v.as_i64()).unwrap_or(18) as u32,
        min_hop_budget: plugin.option_str("assetswap-min-hop-budget")?.and_then(|v| v.as_i64()).unwrap_or(34) as u32,
        quote_expiry_secs: plugin.option_str("assetswap-quote-expiry-secs")?.and_then(|v| v.as_i64()).unwrap_or(120) as u64,
    };

    let state = State {
        config,
        pending_quotes: Arc::new(AsyncMutex::new(HashMap::new())),
    };

    let plugin = plugin.start(state).await?;
    plugin.join().await?;
    Ok(())
}
