---
tags: [reference, core]
aliases: [Roadmap, Fork delta]
---
# Upstream Status

How far this fork is from upstream Core Lightning: which core changes could go upstream as they are, which must stay a fork patch, and what to watch for on the next rebase. The upstream merge-base is `41362d38a`; `git diff 41362d38a -- <paths>` shows the delta.

## Size
The core C delta (`bitcoin common channeld closingd onchaind openingd hsmd lightningd wallet wire plugins/bcli.c`) is about +2,450/−300 lines. Ten reviewers classified it hunk by hunk:

| Class | Lines (approx.) | Examples |
|---|---|---|
| Generic upstream bug fixes | ~160 | onchaind replay of a tx spending two watched outputs; hook "in use" after a chain-ending answer (`plugin_hook.c`); use-after-free on a rejected `openchannel2_changed`/`_sign`; use-after-free of an `openchannel`/`openchannel2` hook's `close_to` when another plugin answers after it (parsed onto `tmpctx`; any peer opening a channel crashed such a node); `wally_tx_output_get_amount` reading a confidential value's commitment as an amount when the asset is explicit; `tx_parts.c` assert on issued-asset outputs (a remote crash on any Liquid node) |
| Generic Elements improvements | ~160 | asset-aware tx/PSBT helpers; fees counted from the fee asset only; `liquid-testnet` network; bcli fake fees on liquid-regtest; Elements change/fee outputs on HTLC-tx rebroadcast and on anchor spends |
| Asset-channel protocol (fork patch) | the rest | fee-reserve second input and its signatures, asset tags on channel outputs, open TLVs, funding checks, wallet-funded asset sweeps, fee cap in fee reserves (`asset_fee_cap`), HTLC cap, SCB record |

> [!info] What can't leave core
> While one lightningd holds several Liquid assets, and Elements has no package relay, about 1,100–1,500 lines must stay in core. They build, sign and check the channel's transactions, which both peers must reproduce byte for byte, and hsmd must be able to check what it signs. Fourteen reviewers across two reviews concluded that a generic "channel tx profile" or plugin-owned open TLVs (H1) would only rename this code and grow core. Neither was done. The only zero-core route found is a Simplicity covenant on Liquid emulating a Bitcoin chain per asset (the EVM pattern). That needs one lightningd per asset and is research, not cleanup.

## PRs that could go upstream first (by value)
1. Elements: don't abort on issued-asset outputs and inputs (`tx_parts.c`, fee-asset-only fee sums). This is a security fix.
2. onchaind replay fixes, as three commits, with `test_onchaind_rescan_htlc_success` and `test_onchaind_replay_tx_spending_two_watched`.
3. Plugin hook use count, with `test_htlc_accepted_hook_replaced_after_fail` and `test_peer_connected_hook_replaced_after_disconnect`.
4. Dual-open hook payload use-after-free, with `test_openchannel2_psbt_hook_reject`.
4b. `close_to` use-after-free in a chain of open hooks, with `test_openchannel_hook_close_to_then_another_plugin` (v1 and v2).
4c. Elements: never read a confidential value as an amount (`bitcoin/tx.c`), with `test_confidential_value_amount`.
5. `liquid-testnet` chainparams, plus bcli fake fees on liquid-regtest.
6. onchaind: probing other CSV values for an unidentified output overwrote the expected script of our own outputs (four places upstream, one on the penalty path). Restored stubs lost their `to_remote`. The fork fixes it; `test_emergencyrecoverpenaltytxn` now gets `to_remote` back too.
7. The asset-aware tx builders, together with anchors on Elements: the fork's Elements anchor spend (`anchorspend.c`: P2WPKH change, explicit fee output) is the part ordinary L-BTC channels could use on their own.

Together these take about 15% out of the fork.

## Rebase hazards (upstream moved since the base)
> [!warning] Check these on every rebase
> - **`efdfcc6da`** (our HTLC-tx input and HTLC claims signed `SIGHASH_ALL`, re-signed after adding wallet inputs) is already ported here by hand: on rebase, take upstream's version and keep the fork's asset bits.
> - **DB migrations**: upstream appended a migration (`forwards ADD reason`) at the slot where the fork adds `channels ADD asset` and `fee_reserve`. Order them so existing fork databases don't skip or repeat a migration, and test an old DB on the new binary.
> - **onchaind series** (`35154be8a`, `086bc092f`, `41ec0d5d9`, `dd9ab739c`): `init_reply()` gains an argument at the sites where the fork calls `note_lbtc_outputs`; `steal_htlc_tx` and `handle_onchain_init_reply` change near fork code.
> - **`cf5a46250`** moves `calc_commitsigs`; **`3ac8f3b6a`** changes `sign_last_tx`'s arguments. Re-apply the reserve signature there.
> - **bwatch / watchman**: when channel watches move to bwatch, the funding-asset check in `watch.c` must move to the funding-found callback.

## Open items
- **EVM production identity**: add a core custom-network option (chain hash plus invoice prefix, about 60–80 lines). It would replace the plugin's identity handshake.
- **EVM `--developer` dependence**: promote `dev-bitcoind-poll`, and add a non-dev way to turn a feature bit off.
- **`pricetiers` margins**: 2.01–2.06× the reference rate, possibly a typo. The owner hasn't decided.
- **Asset-channel units**: dust limit and `htlc_minimum` are negotiated in sats but mean asset units; this is plugin policy, per asset.

## See also
- [[Safety Invariants]]
- [[Core - Transactions and Wire]]
- [[Core - lightningd]]
- [[EVM Channels]]
