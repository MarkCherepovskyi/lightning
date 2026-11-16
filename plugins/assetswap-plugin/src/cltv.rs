//! CLTVs across chains. Leg 1's expiry counts blocks of the chain it was
//! paid on, leg 2's blocks of the chain it is paid on; they are compared by
//! time, using each chain's seconds per block: `assetswap-block-seconds`
//! for the asset if set, else what the node that makes or takes the
//! payment says (`evm-status`'s `interval` on an EVM node, else its
//! network's: 60 s on Liquid, 600 s on Bitcoin).
//!
//! The arithmetic is in the pure functions here (unit-tested below);
//! `block_seconds` does the lookup, once per node.

use crate::rpc::METHOD_NOT_FOUND;
use crate::State;
use anyhow::{anyhow, Error};
use cln_plugin::Plugin;
use cln_rpc::ClnRpc;
use serde_json::{json, Value};
use std::path::Path;

pub(crate) const LIQUID_BLOCK_SECS: u64 = 60;
pub(crate) const BITCOIN_BLOCK_SECS: u64 = 600;

/// The least time leg 2 must end before leg 1's safety margin starts,
/// whatever leg 2's block time: room for a block or two on its chain.
const MIN_LEG2_RESERVE_SECS: u64 = 120;

/// Leg-1 blocks a payer asks for beyond what a provider with the default
/// knobs needs, for blocks found while leg 1 is on its way.
const PAYER_SLACK_BLOCKS: u64 = 8;

/// Seconds per block of a node's network as `getinfo` names it. An EVM
/// node runs as "regtest" too, which is why `evm-status` is asked first.
pub(crate) fn network_block_seconds(network: &str) -> Option<u64> {
    if network.starts_with("liquid") {
        return Some(LIQUID_BLOCK_SECS);
    }
    match network {
        "bitcoin" | "testnet" | "testnet4" | "signet" | "regtest" => Some(BITCOIN_BLOCK_SECS),
        _ => None,
    }
}

/// `evm-status`'s `interval` (seconds per virtual block), rounded up.
fn interval_seconds(v: &Value) -> Option<u64> {
    if let Some(n) = v.as_u64() {
        return (n > 0).then_some(n);
    }
    let f = v.as_f64()?;
    (f.is_finite() && f > 0.0 && f < u32::MAX as f64).then(|| f.ceil() as u64)
}

/// Time a provider keeps between leg 2's latest possible end and leg 1's
/// expiry: `safety_blocks` of leg 1's chain, plus two blocks of leg 2's
/// (at least `MIN_LEG2_RESERVE_SECS`).
pub(crate) fn reserve_secs(safety_blocks: u32, s1: u64, s2: u64) -> Option<u64> {
    let leg2 = s2.checked_mul(2)?.max(MIN_LEG2_RESERVE_SECS);
    u64::from(safety_blocks).checked_mul(s1)?.checked_add(leg2)
}

/// Leg 2's `maxdelay`, in leg-2 blocks, for a leg 1 that expires `rel1`
/// leg-1 blocks from now: whatever time is left after the reserve,
/// rounded down. `None` if nothing is left (or a block time is 0).
pub(crate) fn leg2_maxdelay(rel1: u32, safety_blocks: u32, s1: u64, s2: u64) -> Option<u32> {
    if s1 == 0 || s2 == 0 {
        return None;
    }
    let avail = u64::from(rel1).checked_mul(s1)?.checked_sub(reserve_secs(safety_blocks, s1, s2)?)?;
    u32::try_from(avail / s2).ok()
}

/// The provider's gate on leg 1's CLTV: leg 2's `maxdelay` if it leaves
/// room for the payee's final CLTV plus `min_hop_budget` leg-2 blocks of
/// route, else `None` (fail leg 1).
pub(crate) fn leg2_cltv_gate(
    rel1: u32,
    leg2_final_cltv: u32,
    safety_blocks: u32,
    min_hop_budget: u32,
    s1: u64,
    s2: u64,
) -> Option<u32> {
    let maxdelay = leg2_maxdelay(rel1, safety_blocks, s1, s2)?;
    (u64::from(maxdelay) >= u64::from(leg2_final_cltv) + u64::from(min_hop_budget)).then_some(maxdelay)
}

/// The final CLTV a payer asks of leg 1 (in its own, leg-1, blocks): the
/// least a provider with these knobs accepts for a payee wanting
/// `leg2_final_cltv` leg-2 blocks, rounded up, plus slack.
pub(crate) fn payer_leg1_final_cltv(
    leg2_final_cltv: u32,
    safety_blocks: u32,
    min_hop_budget: u32,
    s1: u64,
    s2: u64,
) -> Option<u32> {
    if s1 == 0 || s2 == 0 {
        return None;
    }
    let leg2_secs = (u64::from(leg2_final_cltv) + u64::from(min_hop_budget)).checked_mul(s2)?;
    let need_secs = leg2_secs.checked_add(reserve_secs(safety_blocks, s1, s2)?)?;
    u32::try_from(need_secs.div_ceil(s1).checked_add(PAYER_SLACK_BLOCKS)?).ok()
}

/// Blocks before leg 1's expiry from which a provider still waiting on leg
/// 2 logs it as an error (it keeps holding leg 1: see `htlc::settle`):
/// half its safety margin, at least 2.
pub(crate) fn giveup_margin(safety_blocks: u32) -> u32 {
    (safety_blocks / 2).max(2)
}

/// Ask a node its seconds per block.
async fn discover(rpc: &mut ClnRpc) -> Result<u64, Error> {
    match rpc.call_raw::<Value, _>("evm-status", &json!({"cached": true})).await {
        Ok(v) => interval_seconds(&v["interval"]).ok_or_else(|| anyhow!("evm-status gave no usable interval")),
        Err(e) if e.code == Some(METHOD_NOT_FOUND) => {
            let info: Value = rpc.call_raw("getinfo", &json!({})).await.map_err(|e| anyhow!("getinfo: {e}"))?;
            let network = info["network"].as_str().unwrap_or_default();
            network_block_seconds(network)
                .ok_or_else(|| anyhow!("unknown block time for network {network:?}: set assetswap-block-seconds"))
        }
        Err(e) => Err(anyhow!("evm-status: {e}")),
    }
}

/// Seconds per block of the chain payments in `asset` are made on, by the
/// node at `node` (reached through `rpc`): the configured override, else
/// the node's answer, remembered for the node after the first time.
pub(crate) async fn block_seconds(
    p: &Plugin<State>,
    asset: &str,
    node: &Path,
    rpc: &mut ClnRpc,
) -> Result<u64, Error> {
    if let Some(&s) = p.state().config.block_seconds.get(asset) {
        return Ok(s);
    }
    if let Some(&s) = p.state().block_secs.lock().unwrap_or_else(|e| e.into_inner()).get(node) {
        return Ok(s);
    }
    let s = discover(rpc)
        .await
        .map_err(|e| anyhow!("block time of the node at {}: {e}", node.display()))?;
    p.state()
        .block_secs
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .insert(node.to_path_buf(), s);
    Ok(s)
}

#[cfg(test)]
mod tests {
    use super::*;

    const LIQ: u64 = LIQUID_BLOCK_SECS;
    const BTC: u64 = BITCOIN_BLOCK_SECS;

    #[test]
    fn networks() {
        assert_eq!(network_block_seconds("liquid"), Some(60));
        assert_eq!(network_block_seconds("liquid-regtest"), Some(60));
        assert_eq!(network_block_seconds("liquid-testnet"), Some(60));
        for n in ["bitcoin", "testnet", "testnet4", "signet", "regtest"] {
            assert_eq!(network_block_seconds(n), Some(600), "{n}");
        }
        assert_eq!(network_block_seconds("dogecoin"), None);
    }

    #[test]
    fn intervals() {
        assert_eq!(interval_seconds(&json!(2)), Some(2));
        assert_eq!(interval_seconds(&json!(1.5)), Some(2));
        assert_eq!(interval_seconds(&json!(0)), None);
        assert_eq!(interval_seconds(&json!(-3)), None);
        assert_eq!(interval_seconds(&Value::Null), None);
    }

    #[test]
    fn reserve() {
        // 18 Liquid blocks + the 120 s floor (two 2 s or 60 s blocks are less).
        assert_eq!(reserve_secs(18, LIQ, 2), Some(18 * 60 + 120));
        assert_eq!(reserve_secs(18, LIQ, 60), Some(18 * 60 + 120));
        // Two 120 s blocks = 240 s.
        assert_eq!(reserve_secs(18, LIQ, 120), Some(18 * 60 + 240));
        assert_eq!(reserve_secs(18, LIQ, BTC), Some(18 * 60 + 1200));
        assert_eq!(reserve_secs(18, BTC, LIQ), Some(18 * 600 + 120));
        assert_eq!(reserve_secs(u32::MAX, u64::MAX, 1), None);
    }

    #[test]
    fn maxdelay_same_chain() {
        // Liquid -> Liquid: 100 blocks, minus 18 + 2 (120 s), leaves 80.
        assert_eq!(leg2_maxdelay(100, 18, LIQ, LIQ), Some(80));
        assert_eq!(leg2_maxdelay(600, 18, BTC, BTC), Some(580));
    }

    #[test]
    fn maxdelay_liquid_to_evm() {
        // 100 Liquid blocks = 6000 s, minus 1200 s = 4800 s.
        assert_eq!(leg2_maxdelay(100, 18, LIQ, 2), Some(2400));
        assert_eq!(leg2_maxdelay(100, 18, LIQ, 60), Some(80));
        assert_eq!(leg2_maxdelay(100, 18, LIQ, 120), Some((6000 - 1320) / 120));
    }

    #[test]
    fn maxdelay_rounds_down() {
        // 6000 - (1080 + 240) = 4680 s = 39 blocks of 120 s exactly.
        assert_eq!(leg2_maxdelay(100, 18, LIQ, 120), Some(39));
        // 4800 s of 7 s blocks: 685.7 -> 685; of 7000 s blocks: none.
        assert_eq!(leg2_maxdelay(100, 18, LIQ, 7), Some(685));
        assert_eq!(leg2_maxdelay(300, 18, LIQ, 7000), Some(0));
    }

    #[test]
    fn maxdelay_liquid_to_bitcoin() {
        // 200 Liquid blocks = 12000 s, minus 1080 + 1200: 9720 s = 16 blocks.
        assert_eq!(leg2_maxdelay(200, 18, LIQ, BTC), Some(16));
        // Bitcoin -> Liquid: 40 Bitcoin blocks = 24000 s, minus 10800 + 120.
        assert_eq!(leg2_maxdelay(40, 18, BTC, LIQ), Some((24000 - 10920) / 60));
    }

    #[test]
    fn maxdelay_underflow_rejects() {
        // 20 Liquid blocks = 1200 s: exactly the reserve, nothing left.
        assert_eq!(leg2_maxdelay(20, 18, LIQ, 2), Some(0));
        assert_eq!(leg2_maxdelay(19, 18, LIQ, 2), None);
        assert_eq!(leg2_maxdelay(0, 18, LIQ, LIQ), None);
        assert_eq!(leg2_maxdelay(100, 18, 0, LIQ), None);
        assert_eq!(leg2_maxdelay(100, 18, LIQ, 0), None);
    }

    #[test]
    fn gate() {
        // Liquid -> Liquid as before: final 18 + hop budget 34 + 20 reserve.
        assert_eq!(leg2_cltv_gate(72, 18, 18, 34, LIQ, LIQ), Some(52));
        assert_eq!(leg2_cltv_gate(71, 18, 18, 34, LIQ, LIQ), None);
        // EVM payee wanting 144 blocks of 2 s: 288 + 68 = 356 s of route,
        // plus 1200 s of reserve: 26 Liquid blocks are enough, 25 aren't.
        assert_eq!(leg2_cltv_gate(26, 144, 18, 34, LIQ, 2), Some((1560 - 1200) / 2));
        assert_eq!(leg2_cltv_gate(25, 144, 18, 34, LIQ, 2), None);
        // A Bitcoin payee wanting 144 blocks is a day: Liquid blocks for it.
        assert_eq!(leg2_cltv_gate(1500, 144, 18, 34, LIQ, BTC), None);
        assert!(leg2_cltv_gate(1840, 144, 18, 34, LIQ, BTC).is_some());
        // Underflow is a rejection, not a panic.
        assert_eq!(leg2_cltv_gate(1, 144, 18, 34, LIQ, BTC), None);
    }

    #[test]
    fn payer_cltv_passes_the_gate() {
        for (s1, s2) in [(LIQ, 2), (LIQ, LIQ), (LIQ, 120), (LIQ, BTC), (BTC, LIQ), (BTC, 2), (2, BTC)] {
            for final2 in [9, 18, 144, 2016] {
                let c = payer_leg1_final_cltv(final2, 18, 34, s1, s2).unwrap();
                // Accepted with up to PAYER_SLACK_BLOCKS blocks found on the way...
                let rel1 = c - PAYER_SLACK_BLOCKS as u32;
                assert!(leg2_cltv_gate(rel1, final2, 18, 34, s1, s2).is_some(), "{s1} {s2} {final2} {c}");
                // ...and no more is asked than that (rounded up).
                assert!(leg2_cltv_gate(rel1 - 1, final2, 18, 34, s1, s2).is_none(), "{s1} {s2} {final2} {c}");
            }
        }
    }

    #[test]
    fn payer_cltv_values() {
        // Liquid -> Liquid, 18-block payee: 18 + 34 + 2 + 18 + 8.
        assert_eq!(payer_leg1_final_cltv(18, 18, 34, LIQ, LIQ), Some(80));
        // Liquid -> EVM (2 s), 144-block payee: ceil((178 * 2 + 120) / 60) = 8, + 18 + 8.
        assert_eq!(payer_leg1_final_cltv(144, 18, 34, LIQ, 2), Some(34));
        // Liquid -> Bitcoin: ceil((178 * 600 + 1200) / 60) = 1800, + 26.
        assert_eq!(payer_leg1_final_cltv(144, 18, 34, LIQ, BTC), Some(1826));
        assert_eq!(payer_leg1_final_cltv(144, 18, 34, 0, BTC), None);
        assert_eq!(payer_leg1_final_cltv(u32::MAX, 18, 34, 1, u64::MAX), None);
    }

    #[test]
    fn giveup() {
        assert_eq!(giveup_margin(18), 9);
        assert_eq!(giveup_margin(3), 2);
        assert_eq!(giveup_margin(0), 2);
    }
}
