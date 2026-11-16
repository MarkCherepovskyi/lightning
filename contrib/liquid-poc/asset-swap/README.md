# Asset-swap quick guide

## On this branch: Liquid -> EVM swaps on master's asset channels

The plugins were written on `liquid-merged`, whose Liquid asset channels
were in core (`invoice`/`xpay`/`getroutes asset=`). Master replaced that
with `contrib/assetchan` and added EVM channels (`contrib/evmchan`), so
here `assetswap-plugin` works with those instead:

- **Asset names.** An issued Liquid asset is its 64-hex id; anything else
  names a node's native coin (`lbtc` on Liquid, `eth` on an EVM node). A
  native coin is invoiced and paid plainly; an issued asset goes through
  assetchan (`assetchan-invoice`, and assetchan's askrene layer for that
  asset, from `assetchan-layer`, which keeps routes on channels in it, as
  `assetchan-pay` does).
- **A provider across two chains** is two nodes of one operator, one per
  chain. The one that receives leg 1 loads both plugins and names the
  other with `assetswap-remote=<asset>:<its lightning-rpc>`; leg 2 is
  decoded and paid there:

  ```
  plugin=/path/to/assetswap-plugin
  plugin=/path/to/pricetiers-plugin
  pricetiers-pair=lbtc:eth:1000000:0
  assetswap-remote=eth:/path/to/evm-node/regtest/lightning-rpc
  ```

  The payer sets `assetswap-provider=<that Liquid node's id>`; the payee
  (the EVM node) makes the invoice with `assetinvoice ... asset=eth`.
- **Hook order doesn't matter.** assetchan's `htlc_accepted` hook only
  checks the asset of an HTLC that pays an invoice of ours, so it leaves a
  leg 1 (no invoice on the provider) to assetswap, whichever runs first.
- **Load them for good.** assetswap is not a dynamic plugin: a provider
  must have it before lightningd replays held leg-1 HTLCs. Run assetchan
  (and, on a provider, assetswap) as `important-plugin=`: assetchan refuses
  to run otherwise, and assetswap then only pays and settles, taking no new
  swaps (lightningd mustn't outlive it while it holds a leg 1).
- **Tests:** `contrib/liquid-poc/asset-swap/run-tests.sh` runs
  `plugins/assetswap-plugin/tests/test_liquid_evm_swap.py` in Docker
  (elementsd + anvil): L-BTC -> ETH, issued asset -> ETH, leg 2 failing,
  and an underpaid leg 1.

How a provider stays safe:

- **CLTVs are compared by time, not blocks.** Each side's seconds per block
  come from `assetswap-block-seconds=<asset>:<secs>`, else the node itself
  (`evm-status`'s `interval` on an EVM node, 60 s on Liquid, 600 s on
  Bitcoin). Leg 2's `maxdelay` is what fits in leg 1's remaining time minus
  the safety margin; a leg 1 too short for leg 2's final CLTV is refused.
- **Leg 2's routing fee is priced in.** The floor is priced on the leg-2
  amount plus `assetswap-leg2-maxfee-ppm` / `-base-msat`, and that maxfee is
  what xpay may spend.
- **Leg 1 is never failed while leg 2 may still succeed.** A quote goes
  `claimed` -> `paying` (recording the node that pays leg 2) -> `complete`
  or `failed`. Pending, unreachable or erroring leg 2 means hold and poll
  again; leg 1 is failed only when every leg-2 part failed, or when leg 1's
  own expiry is close. A `complete` hash is answered from the stored
  preimage, a `failed` one re-claimed only once the paying node shows
  nothing pending.

Tests: `plugins/assetswap-plugin/tests/test_liquid_evm_swap.py` (Docker,
above). The other test files there are from `liquid-merged` and need
porting to assetchan (`tests/test_assetchan.py`'s helpers); see their
module docstrings.

Design background (written for `liquid-merged`, whose asset channels were
in core; the swap protocol itself is unchanged): `CROSS_ASSET_SWAP.md`.
