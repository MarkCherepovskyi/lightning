//! A standalone plugin that quotes price-ceiling tiers for asset pairs:
//! given `to_asset` and `to_amount_msat`, returns low/medium/high price
//! ceilings and the exact `from_amount_msat` each implies, for whichever
//! pair is configured with that `to_asset`.
//!
//! Deliberately decoupled from `plugins/assetswap-plugin`
//! (contrib/liquid-poc/CROSS_ASSET_SWAP.md): this plugin knows nothing
//! about Liquid assets, htlc_accepted, or swap mechanics at all -- asset
//! ids are opaque strings to it, just matched, never interpreted. It's a
//! pure, swappable pricing policy; assetswap-plugin calls its `pricetiers`
//! RPC method as a plain local RPC call (`cln_rpc::ClnRpc::call_raw`,
//! since this isn't a core lightningd method). A future oracle-backed
//! version (e.g. calling `plugins/currencyrate-plugin`) only needs to
//! implement the same RPC contract: {to_asset, to_amount_msat} ->
//! {from_asset, rate_ppm, fee_base_msat, tiers}.

use anyhow::{anyhow, Error};
use cln_plugin::options::StringArrayConfigOption;
use cln_plugin::{Builder, Plugin};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const OPT_PAIR: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "pricetiers-pair",
    "A quotable pair, as from_asset:to_asset:rate_ppm:fee_base_msat. rate_ppm is how \
     many msat of from_asset are required per 1_000_000 msat worth of to_asset at \
     today's reference price. Repeatable. Asset ids are opaque strings to this plugin \
     (e.g. assetswap-plugin's convention of the literal \"lbtc\" for L-BTC) -- never \
     interpreted, just matched against what a `pricetiers` caller asks for.",
);

#[derive(Clone, Debug)]
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
    pairs: Vec<Pair>,
}

#[derive(Serialize, Deserialize)]
struct Tier {
    name: String,
    rate_ppm: u64,
    from_amount_msat: u64,
}

/// Margins in parts-per-thousand over today's reference rate: a looser
/// (higher) cap leaves more room before a live-price recheck can ever
/// fail, at the cost of a worse worst-case rate for the payer.
const MARGINS: [(&str, u64); 3] = [("low", 2010), ("medium", 2030), ("high", 2060)];

fn make_tiers(pair: &Pair, to_amount_msat: u64) -> Vec<Tier> {
    MARGINS
        .iter()
        .map(|(name, permille)| {
            let rate_ppm = pair.rate_ppm * permille / 1000;
            let from_amount_msat = to_amount_msat * rate_ppm / 1_000_000 + pair.fee_base_msat;
            Tier {
                name: name.to_string(),
                rate_ppm,
                from_amount_msat,
            }
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

    let pair = p
        .state()
        .pairs
        .iter()
        .find(|pr| pr.to_asset == to_asset)
        .ok_or_else(|| anyhow!("no pricetiers-pair configured with to_asset {to_asset:?}"))?;

    Ok(json!({
        "from_asset": pair.from_asset,
        "rate_ppm": pair.rate_ppm,
        "fee_base_msat": pair.fee_base_msat,
        "tiers": make_tiers(pair, to_amount_msat),
    }))
}

#[tokio::main]
async fn main() -> Result<(), Error> {
    log_panics::init();

    let Some(plugin) = Builder::new(tokio::io::stdin(), tokio::io::stdout())
        .dynamic()
        .option(OPT_PAIR)
        .rpcmethod(
            "pricetiers",
            "Quote low/medium/high price-ceiling tiers for to_asset/to_amount_msat, \
             discovering from_asset from the matching configured pricetiers-pair",
            pricetiers,
        )
        .configure()
        .await?
    else {
        return Ok(());
    };

    let pairs: Vec<Pair> = plugin
        .option_str("pricetiers-pair")?
        .and_then(|v| v.as_str_arr().cloned())
        .unwrap_or_default()
        .iter()
        .map(|s| parse_pair(s))
        .collect::<Result<_, _>>()?;

    let plugin = plugin.start(State { pairs }).await?;
    plugin.join().await?;
    Ok(())
}
