//! A standalone plugin that quotes price-ceiling tiers for asset pairs:
//! given `to_asset`, `to_amount_msat` and optionally `from_asset`, returns
//! low/medium/high price ceilings and the exact `from_amount_msat` each
//! implies, for the configured pair from `from_asset` to `to_asset` (or,
//! without `from_asset`, the first pair configured with that `to_asset`).
//! Several pairs may share a `to_asset` - one per asset that can pay for
//! it.
//!
//! Deliberately decoupled from `plugins/assetswap-plugin`
//! (contrib/liquid-poc/CROSS_ASSET_SWAP.md): this plugin knows nothing
//! about Liquid assets, htlc_accepted, or swap mechanics at all -- asset
//! ids are opaque strings to it, just matched, never interpreted. It's a
//! pure, swappable pricing policy; assetswap-plugin calls its `pricetiers`
//! RPC method as a plain local RPC call (`cln_rpc::ClnRpc::call_raw`,
//! since this isn't a core lightningd method). A future oracle-backed
//! version (e.g. calling `plugins/currencyrate-plugin`) only needs to
//! implement the same RPC contract: {to_asset, to_amount_msat, from_asset?}
//! -> {from_asset, rate_ppm, fee_base_msat, tiers}.
//!
//! Two sources for the pairs, never mixed: `pricetiers-ember-url` (an
//! ember deployment's internal endpoint, polled every
//! `pricetiers-ember-refresh-secs` and replacing the whole set each time -
//! ember is the source of truth, managed with `ember-cli set-price-pair`),
//! or, when that's unset, the static `pricetiers-pair` options. Every node
//! polling the same ember means payer and provider price with the very same
//! table, so a payer's own local estimate (assetswap-plugin's
//! `quoteassetinvoice`) matches what the provider will accept.

use anyhow::{anyhow, Error};
use cln_plugin::options::{DefaultIntegerConfigOption, StringArrayConfigOption, StringConfigOption};
use cln_plugin::{Builder, Plugin};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::RwLock;

const OPT_PAIR: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "pricetiers-pair",
    "A quotable pair, as from_asset:to_asset:rate_ppm:fee_base_msat. rate_ppm is how \
     many msat of from_asset are required per 1_000_000 msat worth of to_asset at \
     today's reference price. Repeatable. Asset ids are opaque strings to this plugin \
     (e.g. assetswap-plugin's convention of the literal \"lbtc\" for L-BTC) -- never \
     interpreted, just matched against what a `pricetiers` caller asks for. Ignored \
     when pricetiers-ember-url is set.",
);
const OPT_EMBER_URL: StringConfigOption = StringConfigOption::new_str_no_default(
    "pricetiers-ember-url",
    "Base URL of an ember deployment's internal endpoint (e.g. \
     http://ember-server-internal.ember-nodes.svc.cluster.local:8080). When set, the \
     quotable pairs come only from ember's GET /v1/price-pairs, polled periodically.",
);
const OPT_EMBER_REFRESH: DefaultIntegerConfigOption = DefaultIntegerConfigOption::new_i64_with_default(
    "pricetiers-ember-refresh-secs",
    30,
    "How often to re-poll pricetiers-ember-url (default 30s)",
);

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
struct Pair {
    from_asset: String,
    to_asset: String,
    rate_ppm: u64,
    fee_base_msat: u64,
}

fn parse_pair(s: &str) -> Result<Pair, Error> {
    let parts: Vec<&str> = s.split(':').collect();
    let [from_asset, to_asset, rate_ppm, fee_base_msat] = parts.as_slice() else {
        return Err(anyhow!(
            "pricetiers-pair must be from_asset:to_asset:rate_ppm:fee_base_msat, got {s:?}"
        ));
    };
    Ok(Pair {
        from_asset: from_asset.to_string(),
        to_asset: to_asset.to_string(),
        rate_ppm: rate_ppm.parse()?,
        fee_base_msat: fee_base_msat.parse()?,
    })
}

#[derive(Clone)]
struct State {
    /// Replaced wholesale by the ember poller; read by every `pricetiers`
    /// call, so a price change in ember takes effect within one refresh.
    pairs: Arc<RwLock<Vec<Pair>>>,
}

#[derive(Serialize, Deserialize)]
struct Tier {
    name: String,
    rate_ppm: u64,
    from_amount_msat: u64,
}

/// Each tier's rate as parts-per-thousand of today's reference rate:
/// +1%, +3%, +6%. A looser (higher) cap leaves more room before a
/// live-price recheck can ever fail, at the cost of a worse worst-case
/// rate for the payer.
const MARGINS: [(&str, u64); 3] = [("low", 1010), ("medium", 1030), ("high", 1060)];

/// In u128: a pair priced in a much smaller unit than what it buys (e.g.
/// a stablecoin paying for L-BTC, rate_ppm ~6e10) overflows u64 at
/// `to_amount_msat * rate_ppm` for amounts as small as ~0.0015 BTC - and
/// a wrapped product would let a provider accept a tiny leg 1. A result
/// that doesn't fit u64 is an error, never truncated.
fn make_tiers(pair: &Pair, to_amount_msat: u64) -> Result<Vec<Tier>, Error> {
    MARGINS
        .iter()
        .map(|(name, permille)| {
            let rate_ppm = pair.rate_ppm as u128 * *permille as u128 / 1000;
            let from_amount_msat =
                to_amount_msat as u128 * rate_ppm / 1_000_000 + pair.fee_base_msat as u128;
            let too_big = || anyhow!("{name} tier overflows u64 for {to_amount_msat} msat");
            Ok(Tier {
                name: name.to_string(),
                rate_ppm: u64::try_from(rate_ppm).map_err(|_| too_big())?,
                from_amount_msat: u64::try_from(from_amount_msat).map_err(|_| too_big())?,
            })
        })
        .collect()
}

fn param<'a>(args: &'a Value, pos: usize, name: &str) -> Option<&'a Value> {
    match args {
        Value::Array(a) => a.get(pos),
        Value::Object(m) => m.get(name),
        _ => None,
    }
}

async fn pricetiers(p: Plugin<State>, args: Value) -> Result<Value, Error> {
    let to_asset = param(&args, 0, "to_asset")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing to_asset"))?;
    let to_amount_msat = param(&args, 1, "to_amount_msat")
        .and_then(Value::as_u64)
        .ok_or_else(|| anyhow!("Missing or invalid to_amount_msat"))?;
    let from_asset = param(&args, 2, "from_asset").and_then(Value::as_str);

    let pairs = p.state().pairs.read().await;
    let pair = pairs
        .iter()
        .find(|pr| pr.to_asset == to_asset && from_asset.is_none_or(|f| pr.from_asset == f))
        .ok_or_else(|| match from_asset {
            Some(f) => anyhow!("no price pair configured from {f:?} to {to_asset:?}"),
            None => anyhow!("no price pair configured with to_asset {to_asset:?}"),
        })?;

    Ok(json!({
        "from_asset": pair.from_asset,
        "rate_ppm": pair.rate_ppm,
        "fee_base_msat": pair.fee_base_msat,
        "tiers": make_tiers(pair, to_amount_msat)?,
    }))
}

async fn fetch_ember_pairs(client: &reqwest::Client, url: &str) -> Result<Vec<Pair>, Error> {
    let pairs = client
        .get(format!("{}/v1/price-pairs", url.trim_end_matches('/')))
        .send()
        .await?
        .error_for_status()?
        .json::<Vec<Pair>>()
        .await?;
    Ok(pairs)
}

/// Keeps `pairs` in sync with ember. A failed poll leaves the last good set
/// in place rather than emptying it - a brief ember outage shouldn't stop
/// in-flight swaps from being priced.
async fn poll_ember(url: String, refresh: Duration, pairs: Arc<RwLock<Vec<Pair>>>) {
    let client = match reqwest::Client::builder().timeout(Duration::from_secs(10)).build() {
        Ok(c) => c,
        Err(e) => {
            log::warn!("pricetiers: can't build HTTP client: {e}");
            return;
        }
    };
    loop {
        match fetch_ember_pairs(&client, &url).await {
            Ok(fresh) => {
                let mut current = pairs.write().await;
                if *current != fresh {
                    log::info!("pricetiers: {} price pair(s) from ember", fresh.len());
                    *current = fresh;
                }
            }
            Err(e) => log::warn!("pricetiers: polling {url}: {e}"),
        }
        tokio::time::sleep(refresh).await;
    }
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    log_panics::init();

    let Some(plugin) = Builder::new(tokio::io::stdin(), tokio::io::stdout())
        .dynamic()
        .option(OPT_PAIR)
        .option(OPT_EMBER_URL)
        .option(OPT_EMBER_REFRESH)
        .rpcmethod(
            "pricetiers",
            "Quote low/medium/high price-ceiling tiers for to_asset/to_amount_msat, \
             paid in from_asset (optional: else the first pair configured with to_asset)",
            pricetiers,
        )
        .configure()
        .await?
    else {
        return Ok(());
    };

    let static_pairs: Vec<Pair> = plugin
        .option_str("pricetiers-pair")?
        .and_then(|v| v.as_str_arr().cloned())
        .unwrap_or_default()
        .iter()
        .map(|s| parse_pair(s))
        .collect::<Result<_, _>>()?;
    let ember_url = plugin.option(&OPT_EMBER_URL)?;
    let refresh = Duration::from_secs(plugin.option(&OPT_EMBER_REFRESH)?.max(1) as u64);

    let pairs = Arc::new(RwLock::new(match &ember_url {
        Some(_) => {
            if !static_pairs.is_empty() {
                log::warn!("pricetiers: pricetiers-ember-url is set, ignoring pricetiers-pair options");
            }
            Vec::new()
        }
        None => static_pairs,
    }));

    let plugin = plugin.start(State { pairs: pairs.clone() }).await?;
    if let Some(url) = ember_url {
        tokio::spawn(poll_ember(url, refresh, pairs));
    }
    plugin.join().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(rate_ppm: u64, fee_base_msat: u64) -> Pair {
        Pair { from_asset: "usdt".into(), to_asset: "lbtc".into(), rate_ppm, fee_base_msat }
    }

    #[test]
    fn tiers_scale_rate_by_margin() {
        let tiers = make_tiers(&pair(1_000_000, 5), 2_000).unwrap();
        let medium = tiers.iter().find(|t| t.name == "medium").unwrap();
        assert_eq!(medium.rate_ppm, 1_030_000);
        assert_eq!(medium.from_amount_msat, 2_000 * 1_030_000 / 1_000_000 + 5);
    }

    #[test]
    fn large_rate_and_amount_do_not_wrap() {
        // 1 USDT = 1600 sat paying for 1 L-BTC: rate_ppm 62.5e9, 1e11 msat.
        let tiers = make_tiers(&pair(62_500_000_000, 0), 100_000_000_000).unwrap();
        let low = tiers.iter().find(|t| t.name == "low").unwrap();
        assert_eq!(low.from_amount_msat, 6_312_500_000_000_000);
    }

    #[test]
    fn result_beyond_u64_is_an_error() {
        assert!(make_tiers(&pair(u64::MAX / 1000, 0), u64::MAX).is_err());
    }
}
