---
tags: [reference, liquid, evm, swaps]
aliases: [Invariants, Threat model]
---
# Safety Invariants

What must never break, which component guarantees it (core, hsmd, a plugin, the contract), and which test proves it. The design rule is that anything a node's funds depend on is enforced fail-closed in core or hsmd. Plugins add policy on top, but a funds-safety property never rests on a plugin alone when core can check it.

> [!warning] Read before changing code
> Each row names the code that enforces the invariant. Changing that code means re-running the listed test. Line numbers are approximate; search for the function name.

## Liquid asset channels

| # | Invariant | Enforced by | Where | Proven by |
|---|---|---|---|---|
| L1 | A forward leaves in the asset it arrived in. Value never crosses assets 1:1. | **Core**, whatever any plugin answers. The plugin only reroutes to a same-asset channel. | `lightningd/peer_htlcs.c:887` (`forward_htlc`), `:843` (`best_channel`); `contrib/assetchan/assetchan.py` `on_htlc_accepted` | `test_forward_stays_in_asset` |
| L2 | An HTLC paying one of our invoices arrives in that invoice's asset. | Plugin (`assetchan`), final-hop check, for invoices it knows about | `assetchan.py` `on_htlc_accepted` (after `keysend`) | `test_open_pay_close`, `test_gossip` |
| L3 | No asset channel runs without its plugin. | Core start guard; core accepts the asset feature bit (232/233) only from an `important-plugin` (so no other plugin can offer asset channels, and lightningd exits if the one that does dies); the plugin also refuses to run otherwise | `lightningd/channel_control.c:1827`; `lightningd/plugin.c` (featurebits); `assetchan.py` `is_important`/`init` | `test_plugin_must_be_important` |
| L12 | A payment we make leaves in the asset it is meant to be paid in. A plain payment is an L-BTC one and never leaves over an asset channel. | Plugin: `assetchan`'s `rpc_command` hook (before `cln-xpay`, which would otherwise take `pay` over unseen) adds the `assetchan.lbtc` layer to `xpay`, `xpay-as-pay`, `sendamount` and `xkeysend` unless they name an `assetchan.*` layer; rewrites `pay` to `xpay-as-pay` and `keysend` to `xkeysend` with that layer (pay's `exclude` and keysend's `routehints` are refused); adds our asset channels to `renepay`'s `exclude`. Positional calls with more params than it knows, and any error, are refused (fail closed). Raw `sendpay`/`sendonion` are left alone: the caller picks the first hop. `assetchan-pay` takes bolt11 only. **Core**: a blinded path that starts at us (which picks our first channel itself) never leaves over an asset channel | `assetchan.py` `on_rpc_command`; `lightningd/pay.c` (`injectpaymentonion`) | `test_plain_payments_avoid_asset_channels` |
| L4 | Every commitment can be broadcast: its fixed fee (reserve − 660 with both anchors; − 330 with one) meets the 0.1 sat/vB relay floor even with every allowed HTLC. | **Core**: HTLC cap at open. Each side accepts half; a peer asking for more is refused. | `common/initial_commit_tx.c:371` (`asset_commit_max_htlcs`), `openingd/openingd.c:284` (`limit_asset_htlcs`), `openingd/common.c:51` | `test_open_pay_close` (asserts 82 each at 2,000 sat) |
| L5 | A commitment always pays out the whole asset (asset in = asset out). Trimmed HTLCs are burned (OP_RETURN), as they go to the miners on Bitcoin, so nobody profits from dust HTLCs; the rounding left over goes to a direct output, which always exists. | **Core**: `channel_asset_finish_tx`; both channel reserves are at least both dust limits, also after an `openchannel` hook overrides the reserve | `common/channel_asset.c`; `openingd/common.c:65`; `openingd/openingd.c` (after `set_reserve_absolute`) | unit test `common/test/run-channel_asset.c`; `test_hook_reserve_stays_above_dust`, `test_trimmed_htlc_burned` |
| L6 | The funding we lock in really is `amount` of the asset at `n` and exactly the reserve in L-BTC at `n+1`, to the same 2-of-2, both explicit (asset and value). A confidential value is never read as an amount, here or in the wallet. | **Core** (fundee: funding watch; opener: `fundchannel_complete`, which also refuses outputs n and n+1 if the PSET marks them blinded: `psbt_output_blinded`) | `lightningd/watch.c` (`elements_output_is_explicit`, `fee_reserve_output_ok`), `bitcoin/tx.c` `wally_tx_output_get_amount`, `lightningd/opening_control.c` `json_fundchannel_complete` | `test_open_pay_close`; unit `test_confidential_value_amount` |
| L7 | A channel's asset is an explicit tag and not L-BTC. | **Core** | `openingd/openingd.c:1014`, `lightningd/opening_control.c:1366`, recovery `:1885` | the asset suite |
| L8 | hsmd signs only what it should, and never weaker than `SIGHASH_ALL` for a signature of ours: commitments, closes, sweeps, penalties, our side of HTLC txs and HTLC claims off the peer's commitment (upstream `efdfcc6da`, ported). Wallet inputs are added *before* we sign (and we sign again on each fee bump), so nobody can re-wrap or pin our spends. Only the peer's signature on an HTLC tx is `SINGLE\|ANYONECANPAY` (BOLT 3/5). | **hsmd** | `hsmd/libhsmd.c` `spends_funding_and_sibling`, `handle_sign_funding_sibling_input`, `do_sign_local_htlc_tx`, `do_sign_remote_htlc_to_us`, `handle_sign_to_us_tx`; `lightningd/onchain_control.c` `sign_and_get_htlc_witness` | `test_htlc_onchain[*]`, `test_penalty[*]`, Bitcoin `test_closing.py` |
| L9 | Our asset outputs are swept after a unilateral close, even if the wallet has no L-BTC for a while, and even after onchaind is done. A sweep whose input got spent (the peer won an HTLC race) stops at once and lets its wallet L-BTC go, so it never holds coins other sweeps need. | **Core**: wallet-funded sweeps, rebuilt every block until their input is spent; channel kept until the `to_remote` sweep is mined; `asset_sweep_input_spent` | `lightningd/onchain_control.c` | `test_unilateral_close[*]`, `test_sweep_waits_for_wallet_lbtc`, `test_htlc_onchain[success-payee]` |
| L10 | Asset channels are never announced. Gossip only ever carries L-BTC capacity, so a self-issued asset can't fake capacity. | Core (UTXO set holds only L-BTC outputs, unchanged from upstream) plus the plugin (refuses `announce=true` and announced offers) | `assetchan.py:285` (`open_channel`), `:653` (`on_openchannel`) | `test_gossip` |
| L11 | A channel restored from a static backup keeps its asset, fee reserve and close address. A binary that can't restore an asset channel refuses rather than restore it as an ordinary one. | Core (even TLV 4242 in the SCB) | `common/scb_wire.csv`, `lightningd/opening_control.c` `stub_chan` | `test_recover_asset_channel` |

## EVM channels

| # | Invariant | Enforced by | Where |
|---|---|---|---|
| E1 | Only transactions valid under Bitcoin's rules (BOLT 3 templates, sighashes, timelocks) move coins. | **UtxoChain contract** | [[UtxoChain Contract]] |
| E2 | CLN sees only what the contract proved: the tip advances only on a checkpoint whose `txCount` and `logHash` match the indexed rows, none of them undecodable (a provider altering a log's data can't hide a tx). | evmbackend index | [[EVM Index and Blocks]] |
| E3 | Peers on another chain, contract or coin can't open channels with us. | evmbackend identity handshake (`0xFFE9`). Stopgap: the real fix is a core custom chain hash. | [[evmbackend Plugin]] |
| E4 | Only channel types and HTLC counts the contract can afford to validate (anchors only, no leases, HTLC cap). | evmbackend hooks | [[evmbackend Plugin]] |

## Swaps

| # | Invariant | Enforced by | Where |
|---|---|---|---|
| S1 | The provider never fails leg 1 while leg 2 may still succeed. | assetswap `settle` loop (holds and re-polls); fails only when every leg-2 part failed. Never on a deadline: past leg 2's CLTV its payee can still settle it until our timeout confirms, so leg 1 is held past its own expiry if need be (the payer may time it out on chain; the preimage still claims it there). The test holds leg 1 past the old give-up height (expiry − 9), short of expiry | `htlc.rs` `settle`; `test_leg1_held_past_giveup` |
| S2 | Leg 2 can't outlive leg 1: CLTVs are compared by time, using each chain's seconds per block. | assetswap `cltv.rs` | [[assetswap Plugin]] |
| S3 | The provider doesn't lose money: the floor is priced on leg 2's amount plus its maximum routing fee, and `xpay` is capped at that fee. | assetswap + pricetiers | [[Cross-Asset Swaps]] |
| S4 | A provider that dies while holding leg 1 takes lightningd down with it. | assetswap takes new swaps only as `important-plugin` | `plugins/assetswap-plugin/src/htlc.rs:428` |
| S5 | A hash is never paid twice. | Quote record state machine (`complete` resolves from the stored preimage; `failed` is re-claimed only after checking leg 2) | [[assetswap Plugin]] |
| S6 | Leg 2 never pays an invoice of the provider's own nodes (that would mark it paid with the provider's own funds, for a leg 1 priced in another asset). | assetswap: refuses when the bolt11's payee is the leg-1 node or the node that pays leg 2 in this asset (only those two are compared, not every node the provider runs) | `htlc.rs` (`leg 2's invoice is ours`); `test_leg2_not_own_invoice` |
| S7 | The payer sends leg 1 only on the invoice's own hash, priced from its own amount: the wrapper's fields aren't signed. | assetswap `checked_wrapper`: payment_hash and amount must equal the bolt11's, block time 1..=3600 s | `assetinvoice.rs`; `test_wrapper_tampered` |

## Known limits
> [!question] Not covered (yet)
> - **External watchtowers** get no penalty tx for asset channels (the `commitment_revocation` hook isn't called for them): upstream's penalty can't spend an asset output. lightningd's own penalties cover them.
> - **External signers** must support `hsmd_sign_funding_sibling_input` (hsmd advertises it); lightningd refuses to start with asset channels offered otherwise.
> - Fee-reserve fees ignore deadline urgency: the cap is the reserve. Asset commitments can't be bumped either: `anchorspend.c` doesn't build Elements transactions, so a commitment confirms at its own fixed fee. That's fine at Liquid's fees, but a peer could outbid our capped claims in a fee spike.
> - Sweeps share the wallet's L-BTC with no deadline priority: with few wallet coins, a sweep with a later deadline can hold a coin one with an earlier deadline needs (sweeps can chain off each other's unconfirmed change).
> - Dust limit and `htlc_minimum` are negotiated in "sats" but mean asset units.
> - EVM peer identity is a plugin handshake. Invoices from another contract still decode until there is a core custom network.
> - `pricetiers` margins are 2.01–2.06× the reference rate, possibly a typo. That's the owner's decision.

## See also
- [[Liquid Asset Channels]]
- [[Core - lightningd]]
- [[Core - hsmd]]
- [[Upstream Status]]
