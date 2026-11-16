---
tags: [liquid, core, transactions, reference]
aliases: [Asset channel transactions, Asset tx layouts]
---

# Asset Transactions

This note gives the exact shape of every transaction an asset channel produces on Liquid: funding, commitment, HTLC-success/timeout, mutual close and the post-close sweeps. For each one it lists the inputs, outputs, assets, amounts, sequences and locktimes, which signature (and sighash) goes on which input and why, and the weight estimates core uses. It is the reference behind [[Liquid Asset Channels]] and [[Asset Channel Lifecycle]].

## 1. Elements background (what every table below assumes)

### Serialization

Elements' format differs from Bitcoin's. As written by libwally `tx_to_bytes` (`external/libwally-core/src/transaction.c:2029-2125`):

| Part | Bytes | Notes |
|---|---|---|
| version | 4 LE | Always 2 here (`wally_tx_init_alloc(WALLY_TX_VERSION_2, ...)`, `bitcoin/tx.c:571`) |
| flag | 1 | `1` if any witness, else `0`. No BIP144 `00 01` marker. |
| vin | varint + inputs | Each input: prevout txid 32, index 4 LE (high bits flag issuance/peg-in, never set here), scriptSig varbuff (empty), sequence 4 LE |
| vout | varint + outputs | Each output: **asset** (33 bytes explicit: `0x01` + 32-byte asset id), **value** (9 bytes explicit: `0x01` + 8-byte **big-endian** amount, `wally_tx_confidential_value_from_satoshi` `:2761-2769`), **nonce** (1 byte `0x00` = none), scriptPubKey varbuff |
| locktime | 4 LE | |
| witness (if flag) | per input: issuance-amount rangeproof varbuff, inflation-keys rangeproof varbuff, script witness stack, peg-in witness stack. Per output: surjection proof varbuff, rangeproof varbuff | All proofs are empty (1 byte `0x00` each), because everything is explicit |

What a channel output costs: an explicit P2WSH output is 33 + 9 + 1 + 1 + 34 = **78 bytes**. A P2WPKH output is 66 bytes. The fee output (empty script) is 33 + 9 + 1 + 1 = **44 bytes**.

### Explicit outputs, fee output, fee computation

| Rule | Code |
|---|---|
| "Explicit" = `asset_len == 33 && asset[0] == 0x01 && value_len == 9 && value[0] == 0x01` | `elements_output_is_explicit`, `common/channel_asset.c:74-80` |
| Every output gets an asset tag: `bitcoin_tx_add_asset_output(tx, script, wscript, amount, asset)`, where `NULL` means L-BTC | `bitcoin/tx.c:78-112` (`bitcoin_tx_add_asset_output` at `:84`), `wally_tx_output_asset` `:37-71` |
| Every input records the spent output's asset in the PSBT `witness_utxo`: `bitcoin_tx_add_asset_input(..., asset)` | `bitcoin/tx.c:256-300`; `psbt_input_set_wit_utxo_asset` `bitcoin/psbt.c:421-450` |
| **Fee = L-BTC in − L-BTC out.** Inputs in another asset are skipped (`psbt_input_in_fee_asset`), and so are outputs in another asset | `bitcoin_tx_compute_fee` `bitcoin/tx.c:166-183`, `bitcoin_tx_compute_fee_w_inputs` `:142-161`; `bitcoin/psbt.c:626-634` |
| The fee is an explicit L-BTC output with an **empty scriptPubKey**, added (or updated) by `bitcoin_tx_finalize`, always after the outputs are sorted, so it's last | `elements_tx_add_fee_output` `bitcoin/tx.c:194-213`, `bitcoin_tx_finalize` `:582-586` |
| PSBT variant (HTLC txs) | `psbt_elements_normalize_fees`, `bitcoin/psbt.c:530-581` |

### Sighash

`bitcoin_tx_hash_for_sig` (`bitcoin/signature.c:120-152`) calls `wally_tx_get_elements_signature_hash` with the spent output's **value** as an explicit 9-byte value. The fork reads that value with `psbt_input_get_value` (`bitcoin/psbt.c:636-646`), which returns the raw value for an input in **any** asset. Upstream asserted L-BTC there. The call takes no asset argument, so as called, a signature commits to the spent value but not to the spent asset tag. [!question] That's what Elements' segwit-v0 sighash does as far as this code shows. Not checked against the Elements spec.

### Output ordering on Elements

`permute_outputs` sorts BIP69-style by `wally_tx_output.satoshi`, then script, then CLTV (`common/permute_tx.c:37-62`). libwally sets `satoshi = MAX_INVALID_SATOSHI` for **every** Elements output (`wally_tx_elements_output_init` → `tx_elements_output_init(MAX_INVALID_SATOSHI, ...)`, `external/libwally-core/src/transaction.c:1026-1045`), and `bitcoin_tx_output_set_amount` only rewrites the value bytes (`bitcoin/tx.c:328-342`). So on Liquid, channel outputs are ordered **by scriptPubKey, then CLTV**, never by amount. Both peers run the same code, so they agree. This is upstream behavior on Elements, and the asset doesn't change it.

## 2. Funding transaction (built by elementsd, checked by core)

The plugin builds it, explicit throughout (`open_channel`, `contrib/assetchan/assetchan.py:309-327`).

| | Index | Asset | Amount | Script | Notes |
|---|---|---|---|---|---|
| in | 0 | the asset | ≥ `amount` | elementsd UTXO | Unblinded (`explicit_utxo`, `assetchan.py:146-166`) |
| in | 1 | L-BTC | ≥ `fee_reserve + fee` | elementsd UTXO | Unblinded |
| out | **n = 0** | the asset | exactly `amount` (channel units) | P2WSH(2-of-2 funding) | `funding_outnum` (`assetchan.py:185-194`) finds n |
| out | **n+1 = 1** | L-BTC | exactly `fee_reserve` | the **same** P2WSH | The fee reserve |
| out | 2? | the asset | change | new unconfidential elementsd address | Only if the input exceeds `amount` |
| out | 3? | L-BTC | change | new unconfidential elementsd address | Only if ≥ 1000 sat, else added to the fee |
| out | last | L-BTC | fee: `max(1000, 2 × relay × (12 + 70·ins + 45·outs) vB)` | empty | `funding_fee` (`assetchan.py:169-176`) |

Core checks the PSBT in `json_fundchannel_complete` (`lightningd/opening_control.c:1108-1170`) and the mined tx in `watch_check_tx_outputs` / `fee_reserve_output_ok` (`lightningd/watch.c:443-525`). Only n and n+1 matter to core: the same script, explicit, n in the asset with exactly `funding_sats`, n+1 in L-BTC with exactly `fee_reserve`. Other outputs to the funding script fail "Two outputs to open channel". `fee_reserve_outpoint(funding)` is just `{txid, n+1}` (`common/channel_asset.c:82-87`).

> [!info] Funding script
> `bitcoin_redeem_2of2(local_funding_pubkey, remote_funding_pubkey)`: `2 <k1> <k2> 2 OP_CHECKMULTISIG`, keys in lexicographic order, the BOLT 3 funding script, unchanged.

## 3. Commitment transaction

Built by `initial_commit_tx` (commitment 0, `common/initial_commit_tx.c:74-363`) and `commit_tx` (all later ones, `channeld/commit_tx.c:114-487`). Both follow the same asset rules.

### Header

| Field | Value | Where |
|---|---|---|
| version | 2 | BOLT 3 |
| nLockTime | `0x20000000 \| (obscured_commitment_number & 0xFFFFFF)` | `common/initial_commit_tx.c:331-332` |
| inputs | **2** (allocation `asset ? 2 : 1`) | `common/initial_commit_tx.c:199`, `channeld/commit_tx.c:233` |

### Inputs

| # | Spends | Asset / amount (in PSBT) | nSequence | Witness | Signatures |
|---|---|---|---|---|---|
| 0 | funding `n` | the asset, `funding_sats` | `0x80000000 \| (obscured >> 24 & 0xFFFFFF)` | `0 <sig_a> <sig_b> <2-of-2 wscript>` (`bitcoin_witness_2of2`, sigs in key order, `bitcoin/script.c:430-452`) | Both SIGHASH_ALL. The peer's arrives in `commitment_signed.signature` / `funding_created` / `funding_signed`. Ours comes from `hsmd_sign_commitment_tx` at broadcast (`lightningd/peer_control.c:262-270`) |
| 1 | fee reserve `n+1` | L-BTC, `fee_reserve` | **same** sequence as input 0 | same 2-of-2 shape | Both SIGHASH_ALL. The peer's arrives in TLV **`fee_reserve_sig` (4241)** and is stored in the PSBT by `check_fee_reserve_sig` (`common/channel_asset.c:101-117`). Ours comes from `hsmd_sign_funding_sibling_input` (`hsmd/libhsmd.c:1453-1495`). `sign_last_tx` puts the witness together (`lightningd/peer_control.c:273-305`) |

Input 0 is added with `bitcoin_tx_add_asset_input(..., tag)` and input 1 with `add_fee_reserve_input` (`common/channel_asset.c:89-99`): `common/initial_commit_tx.c:343-348`, `channeld/commit_tx.c:459-464`.

> [!note] Why SIGHASH_ALL on input 1 is enough
> Both inputs are signed SIGHASH_ALL, so each signature commits to both prevouts and to every output, fee output included. Neither input can be moved into another transaction. hsmd also checks the shape before signing either input: `spends_funding_and_sibling` (`hsmd/libhsmd.c:1438-1451`) requires either 1 input, or 2 inputs where input 1 spends `{txid of input 0, n+1}`. `hsmd_sign_funding_sibling_input` additionally requires exactly 2 inputs and > 0 outputs (`:1477-1484`), and derives the funding key from the client's own channel unless the caller is the master (`:1471-1476`). hsmd knows nothing about assets.

### Outputs (before sorting)

| Output | Asset | Amount | scriptPubKey (P2WSH of…) | Present when | Code |
|---|---|---|---|---|---|
| offered HTLC (one per HTLC) | the asset | `htlc.amount` rounded down to units | `htlc_offered_wscript` (anchors variant: `1 OP_CSV`) | not trimmed (dust only: zero-fee HTLC txs) | `add_offered_htlc_out` `channeld/commit_tx.c:64-85` |
| received HTLC | the asset | same | `htlc_received_wscript` (anchors variant) | not trimmed | `add_received_htlc_out` `channeld/commit_tx.c:87-112` |
| `to_local` | the asset | `self_pay` rounded down (**no fee subtracted**) | `bitcoin_wscript_to_local` (revocation / `to_self_delay` CSV) | ≥ dust limit | `channeld/commit_tx.c:300-310`; `common/initial_commit_tx.c:222-236` |
| `to_remote` | the asset | `other_pay` rounded down | anchors: `<key> OP_CHECKSIGVERIFY 1 OP_CSV` (P2WSH) | ≥ dust limit | `channeld/commit_tx.c:340-372`; `common/initial_commit_tx.c:255-277` |
| (top-up) | — | leftovers added to a direct output | — | always, see below | `channel_asset_finish_tx` `channeld/commit_tx.c:385-389`; `common/initial_commit_tx.c:281-285` |
| `to_local_anchor` | **L-BTC** | **330** | `bitcoin_wscript_anchor(local funding key)` | `to_local` exists or any untrimmed HTLC | `tx_add_anchor_output` `common/initial_commit_tx.c:61-72` (plain `bitcoin_tx_add_output`, so L-BTC) |
| `to_remote_anchor` | **L-BTC** | **330** | `bitcoin_wscript_anchor(remote funding key)` | `to_remote` exists or any untrimmed HTLC | same |
| **fee** (after sort) | **L-BTC** | `fee_reserve − 330 × #anchors` | empty | always | `bitcoin_tx_finalize` `channeld/commit_tx.c:484`, `common/initial_commit_tx.c:361` |

Sort: `permute_outputs(tx, cltvs, htlcmap)` (`channeld/commit_tx.c:431`), which on Elements means by script, then CLTV (§1).

### Asset conservation: `channel_asset_finish_tx`

`common/channel_asset.c:119-151`, called **after** the direct outputs and **before** the anchors and the sort:

1. `paid` = sum of every output whose asset tag equals the channel's (`:129-135`).
2. `leftover = funding_sats − paid` (abort on underflow, `:136-137`). If 0, return.
3. Add `leftover` to output `opener_outnum` if the opener has a direct output, else to `other_outnum` (`:145-150`). It `assert`s that one of them exists.

What the leftover is: msat → unit rounding of both balances and of each HTLC (`amount_msat_to_sat_round_down`). **Trimmed HTLCs** don't get here: on Bitcoin they go to fees, and on a commitment here their units go to an OP_RETURN burn output first (`channel_asset_add_burn`), so nobody, the opener included, gains from the other side's dust HTLCs. Why a direct output always exists: both channel reserves are ≥ dust (`check_config_bounds`, `openingd/common.c:56-72`), and the opener's balance is never cut for anchors (§3 of [[Liquid Asset Channels]]). Unit test: `test_finish_tx` (`common/test/run-channel_asset.c:145-195`).

### Fee

`commit_tx_base_fee(asset, ...)` returns 0 (`common/initial_commit_tx.h:63-79`), so nothing is subtracted from either balance. `try_subtract_fee` is skipped in commitment 0 (`common/initial_commit_tx.c:153-160`), the 660 isn't added in later ones (`channeld/commit_tx.c:192-199`), and the L-BTC that the outputs don't use becomes the fee output. Tests check for `[330, 330, FEE_RESERVE − 660]` L-BTC outputs (`tests/test_assetchan.py:346-347`).

## 4. HTLC-success / HTLC-timeout transactions

Built by `htlc_tx(..., asset_tag)` (`common/htlc_tx.c:8-71`), through `htlc_success_tx` / `htlc_timeout_tx` (channeld, for signatures: `channeld/full_channel.c:229-290`) and `handle_onchaind_spend_htlc_success` / `_timeout` (lightningd, to broadcast: `lightningd/onchain_control.c:1955-2117`).

### As signed in the channel (zero-fee)

| | # | Asset | Amount | Script / witness | nSequence / nLockTime |
|---|---|---|---|---|---|
| in | 0 | the asset | HTLC amount | spends the commitment's HTLC output. Witness (success): `0 <remote_htlcsig> <local_htlcsig> <preimage> <htlc wscript>`. Timeout: same with an empty element instead of the preimage (BOLT 3) | sequence `1` (anchors, `common/htlc_tx.c:47-49`); locktime 0 (success) / `cltv_expiry` (timeout) |
| out | 0 | the asset | HTLC amount − `htlc_fee` (= 0 with `anchors_zero_fee_htlc_tx`) | P2WSH(`bitcoin_wscript_htlc_tx`: revocation / `to_self_delay` CSV) | — |
| out | 1 | L-BTC | 0 | empty (fee output) | Added by `bitcoin_tx_finalize`, since `bitcoin_tx_compute_fee` = 0 (no L-BTC in or out). [!question] Inferred from `elements_tx_add_fee_output` always adding one on Elements; not observed in a test |

**Signatures on input 0:** the peer's, from `commitment_signed.htlc_signature`, is **SIGHASH_SINGLE|SIGHASH_ANYONECANPAY** (BOLT 3 anchors), so we can add inputs and outputs. **Ours is SIGHASH_ALL** (BOLT 5; upstream `efdfcc6da`, ported): `hsmd_sign_any_local_htlc_tx` → `do_sign_local_htlc_tx` allows the extra wallet inputs on anchor channels, and lightningd signs input 0 *after* adding them, again on every fee bump (`sign_and_get_htlc_witness`, called from `consider_onchain_htlc_tx_rebroadcast`). So nobody else can re-wrap our HTLC tx.

### As broadcast (fee added from CLN's wallet)

`consider_onchain_htlc_tx_rebroadcast` (`lightningd/onchain_control.c:1672`), run immediately and then every block:

| | # | Asset | Amount | Notes |
|---|---|---|---|---|
| in | 0 | the asset | HTLC amount | peer's sig SINGLE\|ACP, ours ALL (re-signed after the wallet inputs are added) |
| in | 1..k | L-BTC | wallet UTXOs | `wallet_utxo_boost` → `psbt_using_utxos`, sequence `BITCOIN_TX_RBF_SEQUENCE` (`0xFFFFFFFD`) (`:1655-1665`). hsmd signs them **SIGHASH_ALL** (`hsmd_sign_htlc_tx_mingle` → `sign_our_inputs` → `wally_psbt_sign`, `hsmd/libhsmd.c:1909-1924`, `:562-645`) |
| out | 0 | the asset | HTLC amount | Unchanged |
| out | fee | L-BTC | `L-BTC in − change` | `psbt_elements_normalize_fees` (`:1786`) keeps one fee output and sets it |
| out | change? | L-BTC | `change_amount(excess, ...)`, at the feerate actually paid (lower when capped) | **P2WPKH** on Elements (upstream: P2TR, which Elements lacks) (`:1778-1782`) |

Asset-specific fee rules: the fee is `min(amount_tx_fee(feerate, weight), asset_fee_cap(channel, deadline, true))` (one fee reserve, ten within 12 blocks of the HTLC's deadline), or everything the gathered wallet inputs hold if that is less; whatever was gathered past it becomes change (`:1722-1755`). The upstream "don't spend more in fees than the HTLC is worth" check is skipped, because the HTLC is in another unit (`:1788-1790`). Without wallet L-BTC it logs "We want to bump HTLC fee, but no funds!" and keeps the zero-fee tx, which won't relay, and retries next block (`:1670-1673`).

## 5. Mutual close transaction

Built by `create_close_tx(..., asset, we_opened)` (`common/close_tx.c:11-110`), called from closingd's `close_tx` (`closingd/closingd.c:48-110`) with `opener == LOCAL`.

| | # | Asset | Amount | Script | nSequence | Signatures |
|---|---|---|---|---|---|---|
| in | 0 | the asset | `funding_sats` | funding 2-of-2 | `0xFFFFFFFF` (`BITCOIN_TX_DEFAULT_SEQUENCE`) | SIGHASH_ALL ×2. closingd signs it for the peer with `hsmd_sign_mutual_close_tx` (`hsmd/libhsmd.c:1498-1530`); lightningd signs again at broadcast with `hsmd_sign_commitment_tx` (`sign_last_tx`) |
| in | 1 | L-BTC | `fee_reserve` | same 2-of-2 | `0xFFFFFFFF` | SIGHASH_ALL ×2. Ours: `hsmd_sign_funding_sibling_input` (closingd `closingd/closingd.c:203-213`; lightningd `peer_control.c:273-284`). Theirs: `closing_signed` TLV `fee_reserve_sig`, checked and stored by `receive_offer` (`closingd/closingd.c:397-411`) |
| out | — | the asset | `to_us` (**no fee subtracted**, `closingd/closingd.c:76-85`) | our close script (`shutdown_scriptpubkey[LOCAL]`: an elementsd address) | — | If ≥ dust (`common/close_tx.c:71-85`) |
| out | — | the asset | `to_them` | their close script | — | If ≥ dust (`:87-92`) |
| (top-up) | — | — | leftovers to the opener's output, else the other's | — | — | `channel_asset_finish_tx(tx, asset, funding, we_opened ? our : their, ...)` (`:98-102`). Absorbs dust-trimmed balances and rounding |
| out | last | L-BTC | **= `fee_reserve`** (the whole reserve) | empty (fee) | — | No anchors on a close. `calc_fee_bounds` pins `min = desired = max = fee_reserve` (`closingd/closingd.c:705-706`); `receive_offer` requires `received_fee == fee_reserve` (`:398-404`) |

Sort: `permute_outputs(tx, NULL, NULL)` (`common/close_tx.c:105`), then `bitcoin_tx_finalize` (`:107`). lightningd re-checks: `close_tx_check` requires 2 inputs, with input 1 spending `fee_reserve_outpoint(funding)` (`lightningd/simple_close_control.c:42-60`). `calc_tx_fee` starts from `fee_reserve` instead of `funding_sats` (`lightningd/closing_control.c:172-200`). `closing_fee_is_acceptable` accepts only `fee == fee_reserve` (`:292-301`).

> [!info] Only legacy `closing_signed`
> `option_simple_close`'s `create_simple_close_tx` (`common/close_tx.c:128-205`) has no asset parameter and builds one input. Asset channels never use it (`lightningd/channel_control.c:1454`).

## 6. Sweeps of asset outputs after a unilateral close

All sweeps go through `asset_sweep_unsigned` (`lightningd/onchain_control.c:1180-1308`). They share one shape:

| | # | Asset | Amount | Script | nSequence | Signature |
|---|---|---|---|---|---|---|
| in | 0 | the asset | `out_sats` (full output) | the swept output (`info->wscript`, or the to_remote scriptpubkey) | `info->to_self_delay` (per kind, below) | per kind, below |
| in | 1..k | L-BTC | wallet UTXOs (last build's first, then `wallet_utxo_boost`) | CLN wallet (P2WPKH, ...) | `0xFFFFFFFD` | `hsmd_sign_withdrawal` → `wally_psbt_sign` = **SIGHASH_ALL** (`sign_wallet_inputs`, `:1385-1441`) |
| out | 0 | the asset | `out_sats` (**full**, no fee) | `channel->shutdown_scriptpubkey[LOCAL]` (the close address) | — | — |
| out | 1? | L-BTC | `change_amount(L-BTC in − fee)` | P2WPKH(final_key) with a BIP32/86 keypath | — | — |
| out | last | L-BTC | `min(feerate × weight, asset_fee_cap)` (+ change too small for an output, if any) | empty (fee) | — | — |

nLockTime = `info->locktime` (`bitcoin_tx(ctx, chainparams, 2, 3, info->locktime)`, `:1122`). No `permute_outputs`: the asset is output 0, change output 1, fee last.

### Per kind

| Kind | Swept output (on which commitment) | Input-0 witness | nSequence | nLockTime | hsmd message | Sighash on input 0 |
|---|---|---|---|---|---|---|
| `to_local` / HTLC-tx output after CSV | ours / our HTLC tx | `<delayed_sig> <> <wscript>` (`bitcoin_witness_sig_and_element` with NULL element, `bitcoin/script.c:568-580`) | `to_self_delay` (from onchaind) | 0 | `hsmd_sign_any_delayed_payment_to_us` (`lightningd/onchain_control.c:924-935`) | **ALL** (`hsmd/libhsmd.c:2228`) |
| penalty: revoked `to_local`, HTLC outputs, their HTLC-tx outputs | theirs (revoked) | `<revocation_sig> <elem from onchaind> <wscript>` | 0 | 0 | `hsmd_sign_any_penalty_to_us` (`:937-948`) | **ALL** (`hsmd/libhsmd.c:1818`) |
| fulfill: their offered HTLC, with preimage | theirs | `<sig> <preimage> <wscript>` | 1 | 0 | `hsmd_sign_any_remote_htlc_to_us` (`:982-996`) | **ALL** (`do_sign_remote_htlc_to_us`, ported from upstream `efdfcc6da`) |
| expired: our offered HTLC, timed out | theirs | `<sig> <> <wscript>` | 1 | `cltv_expiry` | `hsmd_sign_any_remote_htlc_to_us` (`:998-1012`) | **ALL** |
| `to_remote` | theirs (or a revoked one) | `<sig> <wscript>` (P2WSH `<key> OP_CHECKSIGVERIFY 1 OP_CSV`), finalized by wally | 1 (`csv_lock`) | 0 | `hsmd_sign_withdrawal` with a `close_info` UTXO (`option_anchors = true`); `sign_our_inputs` keeps the asset tag of the PSBT input (`hsmd/libhsmd.c:590-610`) | **ALL** |

hsmd lets these to-us messages sign a multi-input tx only when the input being signed is **not** L-BTC (`handle_sign_to_us_tx`, `hsmd/libhsmd.c:681-690`). Ordinary channels keep upstream's one-input rule.

> [!note] All our sweep signatures are SIGHASH_ALL
> Direct spends of HTLC outputs on the **peer's** commitment (fulfill with the preimage, expired after timeout) used to be signed SIGHASH_SINGLE|SIGHASH_ANYONECANPAY by upstream's `do_sign_remote_htlc_to_us` on anchor channels, so a third party could re-use the signed input in a tx of its own against the HTLC deadline. Upstream `efdfcc6da` (ported here) signs them SIGHASH_ALL: we are the only signer, and the wallet inputs are in the tx before we sign.

### Why the sweep has this shape

- **Input 0 = the asset, output 0 = all of it.** An asset can't pay a fee (§1), and CLN's wallet can't hold the asset, so the whole amount goes to the close address. That's where a mutual close would have paid it (DESIGN §3).
- **L-BTC from CLN's own wallet**, not from the plugin or elementsd. DESIGN §8: plugin-funded sweeps with SINGLE|ACP signatures exposed penalties to replacement cycling and dropped sweeps whenever the plugin was down.
- **Fee capped in fee reserves** (`asset_fee_cap`, `lightningd/onchain_control.c:1140`). Core can't tell what the asset is worth in L-BTC, and the reserve is what the opener set aside for fees. One reserve per tx; ten for a claim the peer could win (penalty, HTLC success/timeout, fulfill, expired) within 12 blocks of its deadline, since losing it costs the whole output. Our delayed outputs and `to_remote` stay at one reserve. The fee still follows the deadline's feerate, so the cap only bites in a fee spike. Change too small to be worth an output (at the capped feerate) goes to the fee, so the fee can exceed the cap by about the cost of one output plus dust.
- **Rebuilt every block until the swept input is spent** (`consider_onchain_rebroadcast`, `lightningd/onchain_control.c:1507`), with the UTXOs re-reserved for 2 blocks (`ASSET_SWEEP_RESERVE_BLOCKS`, `:1134`). Once that input is spent, by this sweep or another tx, `asset_sweep_input_spent` (`:1574`) stops the rebuilds and unreserves the sweep's wallet L-BTC.

## 7. Signature map (who signs what)

| Tx | Input | Our signer (daemon → hsmd message) | Peer's signature arrives in | Sighash |
|---|---|---|---|---|
| Commitment (theirs, we sign) | 0 | openingd `sign_remote_commitment` / channeld `calc_commitsigs` → `hsmd_sign_remote_commitment_tx` | — | ALL |
| Commitment (theirs, we sign) | 1 | same → `hsmd_sign_funding_sibling_input` (peer_id NULL) | — | ALL |
| Commitment (ours, broadcast) | 0 | lightningd `sign_last_tx` → `hsmd_sign_commitment_tx` | `funding_created`/`funding_signed`/`commitment_signed` `signature` | ALL |
| Commitment (ours, broadcast) | 1 | lightningd `sign_last_tx` → `hsmd_sign_funding_sibling_input` (peer_id + dbid) | TLV 4241 `fee_reserve_sig` on the same messages | ALL |
| HTLC-success/timeout | 0 | channeld (for the peer) / lightningd `sign_htlc_success`/`_timeout` → `hsmd_sign_any_local_htlc_tx` | `commitment_signed.htlc_signature[]` | peer's SINGLE\|ACP, ours ALL |
| HTLC-success/timeout | 1..k | lightningd → `hsmd_sign_htlc_tx_mingle` | — | ALL |
| Mutual close | 0 | closingd → `hsmd_sign_mutual_close_tx`; lightningd → `hsmd_sign_commitment_tx` | `closing_signed.signature` | ALL |
| Mutual close | 1 | closingd / lightningd → `hsmd_sign_funding_sibling_input` | TLV 4241 on `closing_signed` | ALL |
| Sweeps | 0 | lightningd → delayed / penalty / remote-htlc / withdrawal message | — | ALL |
| Sweeps | 1..k | lightningd → `hsmd_sign_withdrawal` | — | ALL |

hsmd capability for `WIRE_HSMD_SIGN_FUNDING_SIBLING_INPUT`: `HSM_PERM_SIGN_REMOTE_TX | HSM_PERM_SIGN_CLOSING_TX | HSM_PERM_MASTER` (`hsmd/libhsmd.c:131-135`). That's the clients allowed to sign input 0 of the same tx. The message is listed in `hsmd/hsmd.c:799`. Wire definition: `hsmd/hsmd_wire.csv` (type 4100: `peer_id ?node_id`, `channel_dbid u64`, `tx`, `remote_funding_key`).

## 8. Weights core uses

| Estimate | Formula (weight units) | Where |
|---|---|---|
| Commitment base (BOLT 3 anchors) | `1124 + 172·n + elements_tx_overhead(1, 4+n)` | `commit_tx_base_weight`, `common/initial_commit_tx.h:30-58` |
| `elements_tx_overhead(in, out)` | `2·4 + (8+1)·4 + (32+1+1+1)·4·(out+1) + 6·in` = `44 + 140·(out+1) + 6·in` | `bitcoin/tx.h:270-292` |
| Asset commitment (for the HTLC cap) | base + reserve input `4·(32+4+1+4)` = 164 + its witness `1+1+73+73+1+71` = 220 + `overhead(2,0) − overhead(1,0)` = 6 → **2264 + 312·n** | `asset_commit_max_htlcs`, `common/initial_commit_tx.c:371-392` |
| Relay floor | `ASSET_RELAY_FLOOR_PERKW` = 25 sat/kW (0.1 sat/vB, Elements' default `minrelaytxfee`) | `common/initial_commit_tx.c:368` |
| Cap | largest n with `25·(2264+312n)/1000 ≤ fee_reserve − 660`; each side accepts `⌊cap/2⌋` | `asset_commit_max_htlcs`, `limit_asset_htlcs` (`openingd/openingd.c:282-294`) |
| Mutual close | `bitcoin_tx_weight(tx) + num_inputs · bitcoin_tx_2of2_input_witness_weight()` (2 inputs for assets) | `closing_tx_weight_estimate`, `closingd/closingd.c:631-659` (only used for fee bounds, which are then pinned to the reserve) |
| Sweep | `bitcoin_tx_weight(1-in/1-out tx) + channel_input_witness_weight + elements_tx_overhead(2, 3) + Σ utxo_spend_weight` | `asset_sweep_unsigned` `lightningd/onchain_control.c:1130-1146`; `channel_input_witness_weight` `:1076-1093` = `1 + (1+71) + [1+|elem|] + varint(|wscript|) + |wscript|` |
| HTLC tx bump | upstream: `bitcoin_tx_weight(raw HTLC tx)` + what `wallet_utxo_boost` adds | `consider_onchain_htlc_tx_rebroadcast` `:1559-1573` |
| Anchor spend (CPFP) | upstream's 1-in-1-out core + anchor input witness + change + `elements_tx_overhead(1, 1)` + the commitment's weight (the package), plus what `wallet_utxo_boost` adds | `spend_anchor` `lightningd/anchorspend.c:412-419` |

Results of the cap formula: reserve 1,000 → 36 HTLCs (18 per side); 2,000 → 164 (82 per side); see the table in [[Liquid Asset Channels]] §4.

> [!note] Why the HTLC count, not the feerate, is capped
> The fee is fixed by the reserve, so the only thing that can push the commitment below the relay floor is its size. `update_fee` can't help: it changes no output (§3), and CPFP through the anchor can't either: the commitment must be relayed on its own before a child can bump it (no package relay). So the cap is the only lever, and it's checked at open time, once, for the worst case of both sides' `max_accepted_htlcs`. Once relayed, a commitment whose fee is short of an HTLC deadline's feerate is CPFP'd through our anchor with wallet L-BTC, capped by `asset_fee_cap` ([[Core - lightningd]] §10).

## See also

- [[Liquid Asset Channels]]
- [[Asset Channel Lifecycle]]
- [[Core - Transactions and Wire]]
- [[Core - hsmd]]
- [[Core - Daemons]]
- [[Safety Invariants]]
- [[Glossary]]
- [[Index]]
