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
//!
//! `pricetiers-pair` options only set a pair's *starting* rate, at boot.
//! `setpricetier` (`lightning-cli setpricetier from_asset to_asset
//! rate_ppm [fee_base_msat]`) changes it live, with no restart - and
//! persists it to lightningd's own datastore (see `rpc::price_key`), so a
//! later restart keeps the operator-set rate rather than reverting to
//! the boot-time option for the same `to_asset`.

mod rpc;

use anyhow::{anyhow, Error};
use cln_plugin::options::StringArrayConfigOption;
use cln_plugin::{Builder, Plugin};
use cln_rpc::model::requests::{DatastoreMode, DatastoreRequest, ListdatastoreRequest};
use rpc::{new_rpc, price_key, price_prefix};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::sync::RwLock;

const OPT_PAIR: StringArrayConfigOption = StringArrayConfigOption::new_str_arr_no_default(
    "pricetiers-pair",
    "A quotable pair, as from_asset:to_asset:rate_ppm:fee_base_msat. rate_ppm is how \
     many msat of from_asset are required per 1_000_000 msat worth of to_asset at \
     today's reference price. Repeatable. Asset ids are opaque strings to this plugin \
     (e.g. assetswap-plugin's convention of the literal \"lbtc\" for L-BTC) -- never \
     interpreted, just matched against what a `pricetiers` caller asks for.",
);

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Pair {
    from_asset: String,
    to_asset: String,
    rate_ppm: u64,
    fee_base_msat: u64,
}

/// Inserts `pair`, replacing any existing entry with the same `to_asset`
/// (the same uniqueness `pricetiers`'s own lookup assumes).
fn upsert_pair(pairs: &mut Vec<Pair>, pair: Pair) {
    match pairs.iter_mut().find(|pr| pr.to_asset == pair.to_asset) {
        Some(existing) => *existing = pair,
        None => pairs.push(pair),
    }
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
    /// Shared with every RPC call: `setpricetier` writes here so a change
    /// takes effect for the very next `pricetiers`/`setpricetier` call, no
    /// restart needed - same instant-effect convention as ember-server's
    /// `ember-cli set-idle-timeout`/`set-swap-service`.
    pairs: Arc<RwLock<Vec<Pair>>>,
    lightning_dir: String,
    rpc_file: String,
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

    let pairs = p.state().pairs.read().await;
    let pair = pairs
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

/// Sets (or creates) the quotable pair for `to_asset`, live - the very
/// next `pricetiers` call for it reflects the change, no restart needed.
/// Also persists it to lightningd's own datastore (`rpc::price_key`), so
/// it survives one too, taking precedence over whatever `pricetiers-pair`
/// option was set at boot (see `main`'s startup merge).
async fn setpricetier(p: Plugin<State>, args: Value) -> Result<Value, Error> {
    let from_asset = param(&args, 0, "from_asset")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing from_asset"))?
        .to_string();
    let to_asset = param(&args, 1, "to_asset")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("Missing to_asset"))?
        .to_string();
    let rate_ppm = param(&args, 2, "rate_ppm")
        .and_then(Value::as_u64)
        .ok_or_else(|| anyhow!("Missing or invalid rate_ppm"))?;
    let fee_base_msat = param(&args, 3, "fee_base_msat")
        .and_then(Value::as_u64)
        .unwrap_or(0);

    let pair = Pair {
        from_asset,
        to_asset: to_asset.clone(),
        rate_ppm,
        fee_base_msat,
    };

    let mut rpc = new_rpc(&p.state().lightning_dir, &p.state().rpc_file).await?;
    let existing = rpc
        .call_typed(&ListdatastoreRequest {
            key: Some(price_key(&to_asset)),
        })
        .await?
        .datastore
        .into_iter()
        .next();
    let mode = if existing.is_some() { DatastoreMode::MUST_REPLACE } else { DatastoreMode::MUST_CREATE };
    rpc.call_typed(&DatastoreRequest {
        generation: existing.and_then(|d| d.generation),
        hex: None,
        mode: Some(mode),
        string: Some(serde_json::to_string(&pair)?),
        key: price_key(&to_asset),
    })
    .await?;

    upsert_pair(&mut *p.state().pairs.write().await, pair.clone());

    Ok(json!({
        "from_asset": pair.from_asset,
        "to_asset": pair.to_asset,
        "rate_ppm": pair.rate_ppm,
        "fee_base_msat": pair.fee_base_msat,
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
        .rpcmethod(
            "setpricetier",
            "Set (or create) the quotable pair for to_asset: from_asset, to_asset, \
             rate_ppm, fee_base_msat (default 0). Takes effect immediately and \
             persists across restarts, overriding any pricetiers-pair option for \
             the same to_asset.",
            setpricetier,
        )
        .configure()
        .await?
    else {
        return Ok(());
    };

    let mut pairs: Vec<Pair> = plugin
        .option_str("pricetiers-pair")?
        .and_then(|v| v.as_str_arr().cloned())
        .unwrap_or_default()
        .iter()
        .map(|s| parse_pair(s))
        .collect::<Result<_, _>>()?;

    // Persisted setpricetier overrides take precedence over whatever
    // pricetiers-pair options were set at boot, for the same to_asset.
    let configuration = plugin.configuration();
    let lightning_dir = configuration.lightning_dir;
    let rpc_file = configuration.rpc_file;
    let mut rpc = new_rpc(&lightning_dir, &rpc_file).await?;
    let overrides = rpc
        .call_typed(&ListdatastoreRequest {
            key: Some(price_prefix()),
        })
        .await?;
    for d in overrides.datastore {
        let Some(s) = d.string else { continue };
        match serde_json::from_str::<Pair>(&s) {
            Ok(pair) => upsert_pair(&mut pairs, pair),
            Err(e) => log::warn!("pricetiers: ignoring unparseable stored pair at {:?}: {e}", d.key),
        }
    }

    let plugin = plugin
        .start(State {
            pairs: Arc::new(RwLock::new(pairs)),
            lightning_dir,
            rpc_file,
        })
        .await?;
    plugin.join().await?;
    Ok(())
}
