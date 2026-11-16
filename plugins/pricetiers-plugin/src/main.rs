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

/// The tiers for `to_amount_msat` of `pair`'s to_asset. Products are taken
/// in u128; a result that doesn't fit back in a u64 is an error, never a
/// wrapped (and so far too cheap) price.
fn make_tiers(pair: &Pair, to_amount_msat: u64) -> Result<Vec<Tier>, Error> {
    let too_big = || anyhow!("price for {to_amount_msat} msat of {} doesn't fit in a u64", pair.to_asset);
    MARGINS
        .iter()
        .map(|(name, permille)| {
            let rate_ppm =
                u64::try_from(u128::from(pair.rate_ppm) * u128::from(*permille) / 1000).map_err(|_| too_big())?;
            let from_amount_msat = u64::try_from(u128::from(to_amount_msat) * u128::from(rate_ppm) / 1_000_000)
                .ok()
                .and_then(|m| m.checked_add(pair.fee_base_msat))
                .ok_or_else(too_big)?;
            Ok(Tier {
                name: name.to_string(),
                rate_ppm,
                from_amount_msat,
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
        "tiers": make_tiers(pair, to_amount_msat)?,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn pair(rate_ppm: u64, fee_base_msat: u64) -> Pair {
        Pair {
            from_asset: "lbtc".into(),
            to_asset: "eth".into(),
            rate_ppm,
            fee_base_msat,
        }
    }

    fn amounts(t: &[Tier]) -> Vec<u64> {
        t.iter().map(|t| t.from_amount_msat).collect()
    }

    #[test]
    fn tiers_unchanged_for_normal_amounts() {
        // 1:1 rate: 2.01x / 2.03x / 2.06x (tests/test_liquid_evm_swap.py's 20,300).
        let t = make_tiers(&pair(1_000_000, 0), 10_000_000).unwrap();
        assert_eq!(amounts(&t), vec![20_100_000, 20_300_000, 20_600_000]);
        assert_eq!(t[1].rate_ppm, 2_030_000);
        let t = make_tiers(&pair(500_000, 7), 1_000).unwrap();
        assert_eq!(amounts(&t), vec![1_005 + 7, 1_015 + 7, 1_030 + 7]);
    }

    #[test]
    fn no_overflow_where_u64_products_would() {
        // to_amount * rate_ppm is past u64 here, the result isn't.
        let t = make_tiers(&pair(1_000_000, 0), u64::MAX / 4).unwrap();
        assert_eq!(t[0].from_amount_msat, (u128::from(u64::MAX / 4) * 2_010_000 / 1_000_000) as u64);
        // rate_ppm * permille is past u64, rate_ppm * permille / 1000 isn't.
        let t = make_tiers(&pair(u64::MAX / 1000, 0), 0).unwrap();
        assert_eq!(t[0].rate_ppm, (u128::from(u64::MAX / 1000) * 2010 / 1000) as u64);
    }

    #[test]
    fn results_past_u64_are_errors() {
        assert!(make_tiers(&pair(1_000_000, 0), u64::MAX).is_err());
        assert!(make_tiers(&pair(u64::MAX, 0), 1).is_err());
        assert!(make_tiers(&pair(1_000_000, u64::MAX), 1_000).is_err());
    }
}
