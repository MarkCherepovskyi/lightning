# Elements: `newaddr` hands out taproot addresses, and spending such a coin kills the node

**Version:** master at `v26.06-636-g41362d38a`

## What happens

On an Elements chain (Liquid, liquid-regtest) the wallet cannot spend a taproot
output: `hsmd`'s `sign_our_inputs()` gets an error from `wally_psbt_sign()` for
it and calls `hsmd_status_failed()`, so `hsmd` exits and `lightningd` dies with

```
**BROKEN** lightningd: EOF reading from HSM after WIRE_HSMD_SIGN_WITHDRAWAL
**BROKEN** lightningd: FATAL SIGNAL 6
```

Most of the code knows this and avoids taproot on Elements: change outputs
(`wallet/reservation.c`, with a `FIXME: P2TR for elements!`), shutdown scripts
(`anysegwit` is false there), onchaind's sweeps, and `newaddr p2tr`, which is
refused. Two places do not:

1. **`newaddr` with no type** (`wallet/walletrpc.c`). The default became
   `p2tr`, and the default is applied after the check that refuses an explicit
   `p2tr` on Elements. So a plain `newaddr` on Liquid returns a `p2tr` address
   (and `newaddr all` returns one next to the `bech32` one). Whatever is sent
   there cannot be spent.
2. **Coin selection** (`wallet_find_utxo()`, `wallet_utxo_boost()`,
   `wallet_has_funds()`, and `utxopsbt` with explicit coins). A taproot coin
   the wallet holds is picked like any other, and signing it takes the node
   down. If the spend is one that `lightningd` retries by itself (a fee bump or
   sweep for a closed channel), the node dies again on every start.

Also, with `--unified-invoices` the BOLT11 fallback address is always taproot.

## How it showed up

On Liquid testnet. Nodes were funded through `newaddr` (no type), so each held
one taproot coin. When a channel was force closed, the sweep needed a wallet
coin for its fee, selected the taproot one, and the node aborted with the
backtrace above (`sign_wallet_inputs` -> `hsm_sync_req`). It aborted again at
each restart, as soon as onchaind asked for the same sweep. That sweep path is
from our branch, not upstream, but it only does what `withdraw` does: select
wallet coins and send `WIRE_HSMD_SIGN_WITHDRAWAL`.

## Fix

- `newaddr` on Elements always returns `bech32` (default and `all`).
- A new `utxo_is_unsignable()` (taproot on Elements); the three selection
  functions skip such coins, and `utxopsbt` refuses one named explicitly with
  an error instead of trying to sign it. `listfunds` still shows the coin.
- The `--unified-invoices` fallback is P2WPKH on Elements.

This stops new unspendable coins and keeps a node that already holds one
running. It does not make those coins spendable: that needs taproot signing
for Elements in `hsmd`.

Test: `tests/test_wallet.py::test_elements_no_taproot` (liquid-regtest only).
It checks the address types, then turns the node's only coin into a taproot
one in the database and checks that `fundpsbt` and `withdraw` report
insufficient funds, `utxopsbt` refuses the coin, and the node is still up.

Patch: commit `5832815e7` on `generic-fixes` (merged into `feat/clean-up`).
