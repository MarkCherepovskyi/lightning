# Asset channels on CLN — design spec and POC plan

This file is the spec the POC follows. Step 0 commits it verbatim as
`contrib/assetchan/DESIGN.md` on a new branch `asset-channels` cut from `master`
(`v26.06-636-g41362d38a`, i.e. recent upstream CLN).

## 0. Goal and constraints

- Lightning channels whose balance is a non-native asset, first on Liquid (issued
  assets), later on other chains (ETH, Solana, Cosmos).
- CLN stays as close to upstream as possible: a small, generic core delta; **everything
  that can be a plugin is a plugin**.
- The existing `liquid-merged` branch (+4,370 core lines) is a quarry for code and tests,
  not the base.

## 1. Principles

1. **One asset per channel.** The asset is a channel property set at open; HTLCs,
   invoices and routing inherit it. No per-HTLC asset field anywhere. Multi-asset =
   multiple channels (and, on single-coin chains, multiple networks/nodes).
2. **Core = transaction protocol only.** The daemons (`openingd`/`channeld`/`closingd`/
   `onchaind`/`hsmd`) must know the asset to build and sign valid txs; nothing else goes
   in core. Policy, wallet, UX, routing, invoices → plugin.
3. **Reuse upstream mechanisms.** Asset channels use upstream's
   `option_anchors_zero_fee_htlc_tx` channel type, so zero-fee HTLC txs, the
   `SIGHASH_SINGLE|ANYONECANPAY` signing, the "add wallet UTXOs to pay the fee" path
   (`lightningd/onchain_control.c:1125-1195`) and anchor CPFP (`anchorspend.c`) are
   upstream code, not ours.
4. **Chains are plugins.** A chain is whatever satisfies the primitives in §2 behind the
   chain-backend plugin interface (bcli's five methods). CLN's channel code is
   Bitcoin-transaction-shaped and stays so; non-UTXO chains satisfy §2 with a contract
   that emulates a UTXO set.
5. **Safe without the plugin.** A node that has not configured any asset never
   negotiates asset channels (feature bit advertised only when `--channel-asset` is set);
   cross-asset forwarding is refused in core.

## 2. Chain primitives (what a chain must provide)

### 2.1 Level 1 — single-coin UTXO chain (Bitcoin, emulated chains)

Backend plugin interface (upstream, `doc/developers-guide/plugin-development/bitcoin-backend.md`,
`lightningd/bitcoind.c:19-21`): `getchaininfo`, `getrawblockbyheight`, `getutxout`,
`sendrawtransaction`, `estimatefees`. Plus one `chainparams` entry (genesis hash =
`chain_hash` for gossip, `lightning_hrp`, bip32 versions, dust limit, `max_funding`,
`ln_port`).

Transaction primitives the chain's validator must implement (this is what CLN produces
and `hsmd` signs):

| Primitive | Requirement |
|---|---|
| Tx format | Bitcoin segwit-v0 serialization; txid/wtxid as Bitcoin |
| Signatures | secp256k1 ECDSA over BIP143 sighash; `SIGHASH_ALL`, `SIGHASH_SINGLE\|ANYONECANPAY` (HTLC txs, asset sweeps) |
| Scripts | P2WPKH; P2WSH with the BOLT 3 templates: 2-of-2 funding (`OP_CHECKMULTISIG`), `to_local` (`OP_IF` revocation / `OP_CSV` delayed), `to_remote` (P2WPKH, or P2WSH + `1 OP_CSV` with anchors), anchors (`OP_CHECKSIG OP_IFDUP OP_NOTIF 16 OP_CSV`), offered/received HTLCs (`OP_HASH160`, `OP_EQUAL`, `OP_CLTV`, `1 OP_CSV`). No taproot needed if the network is marked "no P2TR" (as Elements is today, `lightningd/opening_control.c` `FIXME: P2TR for elements`) |
| Hashes | SHA256, RIPEMD160 (HTLC scripts), double-SHA256 (txid) |
| Timelocks | relative (`nSequence` in blocks) and absolute (`nLockTime` by height); a monotonic block height |
| Blocks | a chain of headers (`prev_hash` linkage is what `chaintopology.c:1125` checks for reorgs) containing the applied txs, so lightningd's wallet and `onchaind` can scan them |
| Fees | an in-tx fee (inputs − outputs) ≥ the chain's relay floor. On an emulated chain the floor can be 0 and the fee credited to the submitter, who pays the real cost (gas) outside the tx |
| UTXO queries | `getutxout(txid, vout)` → amount + script, for gossip verification |

Per-chain notes:
- **EVM**: a contract holding one coin (ETH or one ERC-20 per instance) as a UTXO set;
  `ecrecover` (secp256k1), `sha256`/`ripemd160` precompiles; a script interpreter for
  the templates above; CSV/CLTV on the contract's block counter; deposit/withdraw.
  Gas → use an L2. Backend plugin synthesizes Bitcoin-format blocks from the contract's
  event log.
- **Solana**: program with `secp256k1_recover` and SHA256 syscalls; RIPEMD160 in-program;
  UTXOs as PDAs; height = slot.
- **Cosmos**: CosmWasm contract (k256/sha2/ripemd crates) or SDK module; IBC denom as
  the coin; native block height.
- **Bitcoin**: trivially Level 1 (bcli).

On Level 1 chains **no asset-channel code is used**: ordinary CLN channels, fee in the
coin. Multi-asset means one network (contract) per asset. Core support needed later:
custom `chainparams` from config instead of the compiled table (`bitcoin/chainparams.c`),
~50 lines; not part of this POC.

### 2.2 Level 2 — multi-asset UTXO chain (Liquid)

Level 1 plus: every output carries an asset tag; fees must be paid in the native coin
(L-BTC) **inside** the transaction (Elements enforces a min relay fee and, to be
confirmed with the Elements team, has no package relay, so a zero-fee commitment can't be
CPFP'd). That single property is what forces the §3 protocol and the §4 core delta.
Explicit (unblinded) outputs only.

## 3. Asset channel protocol (Level 2 delta)

- **Negotiation**: feature `option_channel_asset` (odd bit in the custom range, number
  TBD), advertised only when `--channel-asset` is configured. `open_channel` TLVs
  `channel_asset` (4243, 33-byte explicit asset tag) and `fee_reserve` (4245, sats);
  `accept_channel` echoes `channel_asset` or the open fails. Channel type must be
  `anchors_zero_fee_htlc_tx`. v2 (dual-fund) and splicing: refused for asset channels.
- **Funding tx**: output n = `amount` of the asset to the 2-of-2; output n+1 =
  `fee_reserve` L-BTC to the same 2-of-2. Both explicit.
- **Commitment tx**: inputs 0 (asset funding) and 1 (L-BTC reserve), each with both
  parties' signatures; the second signature travels in TLV `fee_reserve_sig` (4241) on
  `funding_created`, `funding_signed`, `commitment_signed`. Outputs: `to_local`,
  `to_remote`, HTLCs in the asset (BOLT 3 anchor-era scripts unchanged); anchors 330 sat
  L-BTC each (BOLT 3 rules); explicit fee output = reserve − anchors. **No fee is taken
  from the asset balances; `update_fee` is a no-op; no change output.** The opener picks
  a small reserve (plugin default 2,000 sat; Liquid's floor is 0.1 sat/vB, so ≥ 1,000 sat
  covers the largest commitment; anchors + CPFP cover urgency).
- **HTLC txs**: upstream anchors behaviour (zero fee, `SIGHASH_SINGLE|ANYONECANPAY`,
  broadcaster adds wallet L-BTC); output 0 carries the asset tag.
- **Mutual close**: legacy `closing_signed` only; fee = whole reserve, so negotiation is
  immediate; TLV `fee_reserve_sig` carries input 1's signature. Outputs: asset to each
  side's close script.
- **Unilateral close**: `onchaind` recognizes outputs by script as today and reads
  asset amounts. Every asset output we own (`to_local` after CSV, `to_remote`, HTLC
  outputs and HTLC-tx outputs, penalties) is spent zero-fee to the channel's close script
  and funded with wallet L-BTC through the same routine as HTLC txs. Anchors: upstream
  `anchorspend.c` (they are L-BTC).
- **Invariants (core)**: one asset per channel; `forward_htlc` refuses a different asset
  in/out and `best_channel` matches the asset (`lightningd/peer_htlcs.c`, ~20 lines).
- **Operational**: every node holding asset channels needs some L-BTC in its CLN wallet
  (HTLC txs, sweeps, anchors) and sets a close address (its elementsd wallet) so swept
  assets are spendable.

## 4. CLN core delta ("small improvements")

| # | Change | Files | ≈ lines | Why not plugin |
|---|---|---|---|---|
| C1 | Channel asset identity: `--channel-asset` (multi) + feature bit; TLVs; `channel->asset`, `fee_reserve`; DB columns; `channeld/closingd/onchaind` init fields; `fundchannel_start asset= fee_reserve=`; `listpeerchannels.asset`, `fee_reserve_msat`; `openchannel` hook payload fields; `common/channel_asset.[ch]` (from `common/liquid_asset.c`) | `wire/peer_wire.csv`, `common/features.[ch]`, `openingd/*`, `lightningd/{options,channel,opening_control,peer_control,channel_control,closing_control}.c`, `wallet/{wallet,migrations}.c`, `*_wire.csv`, `doc/schemas` | 450 | daemons must know the asset |
| C2 | Asset tags on channel tx outputs; fee computation counts only the fee asset; tx parser reads asset amounts instead of asserting L-BTC | `bitcoin/{tx,psbt,tx_parts}.c`, `common/{initial_commit_tx,htlc_tx,close_tx}.c`, `channeld/commit_tx.c` | 150 | tx builders live in daemons |
| C3 | Reserve input + second signature + `hsmd` signing both inputs; `sign_last_tx` | `openingd`, `channeld`, `closingd`, `hsmd/{hsmd,libhsmd}.c`, `hsmd_wire.csv`, `lightningd/peer_control.c` | 400 | protocol + signer |
| C4 | Allow anchors on Elements for asset channels (`lightningd/options.c:1854`); skip fee subtraction; fee output = reserve − anchors; ignore `update_fee` | `lightningd/options.c`, `channeld/{commit_tx,channeld}.c` | 40 | |
| C5 | Asset sweeps: zero-fee spend to close script + reuse the HTLC-tx wallet-funding routine; `to_remote` swept instead of handed to the wallet | `lightningd/onchain_control.c`, `onchaind/onchaind.c`, `onchaind_wire.csv` | 400 | `onchaind` + channel keys |
| C6 | `fundchannel_complete` verifies output n (asset, amount, explicit) and n+1 (L-BTC, reserve); funding watch by asset | `lightningd/opening_control.c`, `lightningd/watch.c` | 60 | safety |
| C7 | `liquid-testnet` chainparams | `bitcoin/chainparams.c` | 35 | |

Target ≈ 1,500 lines (vs 4,370 on `liquid-merged`); measured after the POC. **Not in
core** (vs the branch): asset wallet (`withdrawasset`, `listassetfunds`,
`our_asset_outputs`), `invoice asset=`, per-asset gossip (gossipd/connectd/gossmap,
~600 lines; POC uses private channels), askrene/xpay/topology `asset=`, the custom
non-anchor zero-fee HTLC scheme, the fee-reserve change-output logic, macOS fd fixes.

## 5. Plugin spec — `contrib/assetchan/assetchan.py` (Python, pyln-client)

Location rationale: `plugins/` Python plugins become built-ins
(`plugins/Makefile:256`); contrib keeps it opt-in (`--important-plugin`), in the Docker
build context and in `run-tests.sh`.

- **elementsd client**: stdlib `urllib`; credentials from `listconfigs`
  (`bitcoin-rpcuser/-rpcpassword/-rpcconnect/-rpcport`, plain strings in
  `plugins/bcli.c:895-905`); override `assetchan-elements-rpc`, wallet
  `assetchan-elements-wallet`.
- **Options**: `assetchan-min-fee-reserve` (1000), `assetchan-max-fee-reserve` (10000),
  `assetchan-default-fee-reserve` (2000), `assetchan-close-to` (`elements`|`none`).
  Allowed assets come from core's `--channel-asset` via `listconfigs`.
- **RPCs**:
  - `assetchan-open id amount asset [fee_reserve] [announce=false] [close_to]`:
    `connect` → `fundchannel_start(asset=, fee_reserve=, close_to=<elementsd
    unconfidential addr>)` → `walletcreatefundedpsbt([], [{addr: units, "asset": id},
    {addr: reserve}], 0, {"changePosition": 2, "lockUnspents": true})` → `decodepsbt`
    sanity → `walletprocesspsbt` (sign + blind change **before** CLN sees it) →
    `fundchannel_complete` → `finalizepsbt` → `sendrawtransaction`; on failure
    `fundchannel_cancel` + `lockunspent`. Fallback: `listunspent` + `createpsbt`.
  - `assetchan-close id [destination]`, `assetchan-list [asset]`, `assetchan-funds`
    (elementsd balances + channel balances), `assetchan-send addr asset amount`,
    `assetchan-newaddr`.
  - `assetchan-invoice amount asset label desc [expiry]`: POC = `invoice` + datastore
    `["assetchan","invoice",<payment_hash>] → asset`; v2 = asset as an unassigned bolt11
    tagged field via `createinvoice` (CLN keeps unknown fields: `common/bolt11.c:144,1393`).
  - `assetchan-pay bolt11 asset`: creates/updates askrene layer `assetchan.<asset>`
    disabling every local channel of another asset (`askrene-update-channel
    enabled=false`), then `xpay layers=[...]`; private hops come from the invoice's route
    hints.
- **Hooks**: `openchannel` — reject if asset not allowed or reserve outside
  [min,max], else `continue` with `close_to`; `htlc_accepted` — final hop only: incoming
  channel's asset (cache from `listpeerchannels` by scid/alias) must equal the invoice's
  asset, else `fail` with `incorrect_or_unknown_payment_details`.
- **Notifications**: `channel_state_changed`/`channel_opened` (cache), `invoice_payment`
  (drop datastore entry). Runs as `important-plugin`; loaded before `htlcs_resubmit`
  (`lightningd/lightningd.c:1422-1437`).

## 6. POC

Branch: `git checkout -b asset-channels master`. Commits:

0. `contrib/assetchan/DESIGN.md` = this spec.
1. **C1 + C2** ported from `liquid-merged`: `common/liquid_asset.c` → `common/channel_asset.c`
   (generic names), `bitcoin/tx.c`/`psbt.c`/`tx_parts.c` helpers, openingd TLVs, channel
   struct/DB/init messages, `listpeerchannels` fields, hook payload, `--channel-asset`.
2. **C3 + C4**: reserve input, `fee_reserve_sig`, hsmd two-input signing, anchors on
   Elements, fixed fee output. Build: `make`, `make check-units`.
3. **C6 + C7**.
4. **Plugin v0** (open/list/close/invoice/pay, `openchannel` + `htlc_accepted` hooks) and
   `tests/test_assetchan.py::test_open_pay_close`: A–B–C on `liquid-regtest`, asset
   channels A–B and B–C funded by elementsd, L-BTC channel A–B alongside, asset invoice
   on C paid from A (2 hops, route hint), wrong-asset payment refused, mutual close,
   assets land at the elementsd close addresses. Harness pieces reused from
   `tests/test_liquid_asset_poc.py` (`explicit_tx`, `unconfidential`, `explicit_utxo`).
5. **C5** + `test_unilateral_close[opener|peer]`; stretch: `test_htlc_onchain`,
   `test_penalty` (adapted from the branch).
6. `git diff --stat master` → real numbers into DESIGN.md §4; port `run-tests.sh` for
   macOS Docker runs.

Reuse map (`git show liquid-merged:<path>`): `common/liquid_asset.[ch]`,
`bitcoin/tx.c`, `bitcoin/psbt.c`, `bitcoin/tx_parts.c`, `bitcoin/chainparams.c`
(testnet), `openingd/openingd.c` (TLVs, reserve sig), `channeld/commit_tx.c`
(asset outputs), `hsmd/{hsmd,libhsmd}.c`, `closingd/closingd.c` + `common/close_tx.c`,
`lightningd/opening_control.c` (params, checks), `lightningd/watch.c`,
`onchaind/onchaind.c` (asset output recognition only), tests.

### Verification

- `make && make check-units`; `TEST_NETWORK=liquid-regtest pytest -n 4 tests/test_assetchan.py`
  (Linux with `elementsd` on PATH, or the Docker runner).
- Upstream regression on Elements: `TEST_NETWORK=liquid-regtest pytest tests/test_closing.py -k anchor`
  after C4, since anchors were disabled there.
- On-chain (elements-cli): funding vout 0 asset/units, vout 1 L-BTC/reserve, same script,
  both explicit; commitment has two inputs, anchors in L-BTC, fee output = reserve −
  anchors; sweeps pay fees from wallet L-BTC; assets at close addresses.

## 7. Verify first (day 1 spikes), risks

- Why upstream disables anchors on Elements (`options.c:1854`): enable and run the
  upstream anchors tests on `liquid-regtest`. If anchors are genuinely broken there, fall
  back to the branch's non-anchor scheme (C4/C5 grow by ~400 lines).
- Elements relay policy: min relay fee and absence of package relay (ask the Elements
  team). If package relay exists/lands, the reserve shrinks to the anchors alone and the
  commitment fee can be 0.
- `walletcreatefundedpsbt` with asset outputs + blinded change; `fundchannel_complete`
  accepting the PSET; `createinvoice` preserving an unknown tagged field (v2 invoices).
- Fixed fee wastes reserve − anchors on every close: acceptable at 2,000 sat; a change
  output to the opener (as on the branch) is a later refinement.
- Dust limit / `htlc_minimum` are negotiated in "satoshis" but mean asset units:
  plugin policy per asset later.
- Without gossip, asset channels are private (route hints); per-asset gossip is a later
  core PR or a plugin layer.
- Upstream review will ask about TLV numbers, the reserve, and anchors on Elements;
  the plugin is unaffected by whichever protocol detail changes.
