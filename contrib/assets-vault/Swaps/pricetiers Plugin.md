---
tags: [swaps, rust, plugin, pricing]
aliases: [pricetiers-plugin, pricetiers]
---
# pricetiers Plugin

`plugins/pricetiers-plugin/src/main.rs` is a small standalone Rust plugin that answers one question for the swap provider: "to pay out `to_amount_msat` of `to_asset`, how much of which `from_asset` do I need, at low, medium and high price ceilings?". It is a pure, swappable pricing policy: asset ids are opaque strings to it, and it knows nothing about Liquid, HTLCs or swaps. [[assetswap Plugin]] calls it as a plain local RPC (`protocol::fetch_price_tiers`).

## Option: `pricetiers-pair`

`pricetiers-pair=from_asset:to_asset:rate_ppm:fee_base_msat`, repeatable (`main.rs:23-30`, parse at `main.rs:40-53`). Exactly four colon-separated fields (so asset strings cannot contain `:`); `rate_ppm` and `fee_base_msat` parse as `u64`.

| field | meaning |
|---|---|
| `from_asset` | what the payer pays with, e.g. `lbtc` or a 64-hex asset id |
| `to_asset` | what the provider pays out, e.g. `eth` |
| `rate_ppm` | msat of `from_asset` required per 1,000,000 msat of `to_asset` at the **reference** price (1,000,000 = 1:1) |
| `fee_base_msat` | fixed msat of `from_asset` added to every tier |

Example (from the tests): `lbtc:eth:1000000:0`, or `<asset id>:eth:1000000:0`.

The plugin is `.dynamic()` (`main.rs:131`), unlike assetswap. Pairs are read once at start (`main.rs:145-151`).

## RPC: `pricetiers to_asset to_amount_msat`

Handler `pricetiers` (`main.rs:103-124`), positional or named arguments (`param`, `main.rs:95-101`).
1. Both arguments are required (`to_amount_msat` a `u64`).
2. The **first** configured pair whose `to_asset` equals the argument is used (`main.rs:111-116`); none gives the error `no pricetiers-pair configured with to_asset "..."`. Two pairs with the same `to_asset` and different `from_asset` therefore cannot both be quoted: the first wins.
3. Reply:

```json
{"from_asset": "lbtc", "rate_ppm": 1000000, "fee_base_msat": 0,
 "tiers": [{"name":"low","rate_ppm":...,"from_amount_msat":...},
           {"name":"medium",...}, {"name":"high",...}]}
```

`rate_ppm` and `fee_base_msat` at the top are the pair's reference values; each tier carries its own scaled `rate_ppm`. The assetswap plugin mirrors this shape in `PriceTiers` (`plugins/assetswap-plugin/src/protocol.rs:121-127`).

## Tiers and margins

```rust
const MARGINS: [(&str, u64); 3] = [("low", 2010), ("medium", 2030), ("high", 2060)];
```
`main.rs:70`. Margins are in parts per thousand over the reference rate. For each tier (`make_tiers`, `main.rs:75-93`):

```text
tier_rate_ppm   = pair.rate_ppm * permille / 1000
from_amount_msat = to_amount_msat * tier_rate_ppm / 1_000_000 + fee_base_msat
```

> [!warning] Possible typo, undecided
> The constants are 2010 / 2030 / 2060 permille, i.e. **2.01x, 2.03x and 2.06x** the reference rate (the test at `main.rs:177` asserts `20_100_000 / 20_300_000 / 20_600_000` for 10,000,000 msat at 1:1, and `test_liquid_evm_swap.py:34` mirrors `MARGINS = {'low': 2010, 'medium': 2030, 'high': 2060}`). The doc comment calls them "margins ... over today's reference rate", which would suggest 1.01x / 1.03x / 1.06x (1010 / 1030 / 1060). Whether 2.0x is intended (for instance as a deliberate spread in a proof of concept) is **not decided**: do not change the constants, the tests in two places and the `20,300` example in the test docs without the user's decision.

How assetswap uses the tiers:
- payer: picks a tier by name (`payassetinvoice tier=`, default `medium`) and sends that tier's `from_amount_msat` (`pay.rs:173-178`).
- provider: requires leg 1's amount to be `>=` the **lowest** tier's `from_amount_msat`, computed on `to_amount + leg-2 fee budget` (`htlc.rs:465-480`). The amount above the floor is extra profit.

## Arithmetic

All products go through `u128` (`main.rs:81-85`): `rate_ppm * permille` and `to_amount * rate_ppm`. A result that does not fit back into a `u64`, or an overflowing `+ fee_base_msat` (`checked_add`), is an error `price for N msat of <to_asset> doesn't fit in a u64`, never a wrapped, far too cheap price. The tests (`main.rs:158-200`):
- `tiers_unchanged_for_normal_amounts`: 1:1 rate gives 20,100,000 / 20,300,000 / 20,600,000 for 10,000,000 msat, tier `rate_ppm` 2,030,000 for medium; rate 500,000 and fee 7 on 1,000 msat gives 1,012 / 1,022 / 1,037.
- `no_overflow_where_u64_products_would`: amounts near `u64::MAX/4` work because the product is in `u128`.
- `results_past_u64_are_errors`: `u64::MAX` amount, `u64::MAX` rate, `u64::MAX` fee all error.

> [!note] Rounding
> Integer division rounds down at both steps, in the provider's disfavour by at most 1 msat per step; the fee budget and the tier margin dwarf it.

## Extending

An oracle-backed replacement (e.g. one using `plugins/currencyrate-plugin`) only has to implement the same RPC contract `{to_asset, to_amount_msat} -> {from_asset, rate_ppm, fee_base_msat, tiers}` (`main.rs:11-15`); assetswap needs nothing else. It also needs `from_asset` to be what the provider can be paid in on leg 1: [[assetswap Plugin]] requires leg 1's channel asset to match it exactly (`htlc.rs:487-489`).

## See also
- [[Cross-Asset Swaps]]
- [[assetswap Plugin]]
- [[Operations]]
