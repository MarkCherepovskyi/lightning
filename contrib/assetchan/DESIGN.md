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
  event log. Plan: `ETH.md`.
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

- **Negotiation**: feature `option_channel_asset` (odd bit in the custom range: 233 in
  the POC, `OPT_CHANNEL_ASSET` = 232, provisional until upstream assigns one), advertised
  only when `--channel-asset` is configured. `open_channel` TLVs
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
- **Mutual close**: legacy `closing_signed` only (`option_simple_close` is skipped for
  asset channels even when negotiated); fee = whole reserve, so negotiation is
  immediate; TLV `fee_reserve_sig` carries input 1's signature; lightningd's closing-tx
  check (`close_tx_check`) expects the reserve as input 1. Outputs: asset to each side's
  close script.
- **Unilateral close**: `onchaind` recognizes outputs by script as today and reads
  asset amounts (its L-BTC outputs, anchors and fee, stay out of the channel's
  asset-denominated account). Every asset output we own (`to_local` after CSV,
  `to_remote`, HTLC outputs and HTLC-tx outputs, penalties) is spent through upstream's
  own sweep path (`onchain_signing_info`, `create_onchain_tx`, rebroadcast): the asset
  output is input 0 and goes in full to output 0 (the channel's close script, or the
  HTLC-tx output for HTLC txs), wallet L-BTC inputs pay the fee and the rest comes back
  to the wallet as P2WPKH change; rebuilt before every rebroadcast with whatever L-BTC
  the wallet has. `to_remote` is swept too, from the same `onchaind_add_utxo`
  notification upstream hands the wallet (which can't hold assets). The fee is capped
  at the channel's reserve (we can't price the asset in L-BTC). Anchors:
  `anchorspend.c` is off for asset channels (the commitment pays its own fee; it isn't
  Elements-aware).
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
| C5 | Asset sweeps on upstream's sweep path (`onchain_signing_info` / `create_onchain_tx` / rebroadcast): `onchaind_tx_unsigned` builds an asset sweep (asset in → asset out to the close script, wallet L-BTC for the fee, capped at the fee reserve, p2wpkh change) and `onchaind_tx` signs it; `to_remote` swept from the upstream `add_utxo` notification; HTLC txs via upstream `htlc_tx` with Elements fixes; `anchorspend` off | `lightningd/onchain_control.c`, `onchaind/onchaind.c`, `onchaind_wire.csv`, `bitcoin/psbt.c`, `lightningd/anchorspend.c` | 350 | `onchaind` + channel keys |
| C6 | `fundchannel_complete` verifies output n (asset, amount, explicit) and n+1 (L-BTC, reserve); funding watch by asset | `lightningd/opening_control.c`, `lightningd/watch.c` | 60 | safety |
| C7 | `liquid-testnet` chainparams | `bitcoin/chainparams.c` | 35 | |
| C8 | Gossip can verify an asset channel: the block scan records every explicit P2WSH output (any asset, its units as the amount) in the utxoset and filtered blocks, and `fundchannel_start` no longer insists on `announce=false` | `lightningd/{chaintopology,bitcoind,opening_control}.c` | 10 | gossip verification is in lightningd |

Target was ≈ 1,500 lines (vs 4,370 on `liquid-merged`). **Measured** (`git diff --stat
master`, 2026-11-05, without `contrib/assetchan`, the pytest and the generated
`msggen/schema.json`): 81 files, +2,099/−343, of which unit/fuzz tests +307; by area:
lightningd +676 (C5's sweeps ≈ 340 of it), common +589, openingd +172, bitcoin +146,
channeld +100, hsmd +99, onchaind +98, closingd +64, doc +42, wire +34, wallet +27.
The table's per-change numbers are the estimates. **Not in core** (vs the branch): asset wallet (`withdrawasset`, `listassetfunds`,
`our_asset_outputs`), `invoice asset=`, per-asset gossip (gossipd/connectd/gossmap,
~600 lines; see Gossip in §7), askrene/xpay/topology `asset=`, the custom
non-anchor zero-fee HTLC scheme, the fee-reserve change-output logic, macOS fd fixes.

## 5. Plugin spec — `contrib/assetchan/assetchan.py` (Python, pyln-client)

Location rationale: `plugins/` Python plugins become built-ins
(`plugins/Makefile:256`); contrib keeps it opt-in (`--important-plugin`), in the Docker
build context and in `run-tests.sh`.

- **elementsd client**: stdlib `urllib`; credentials from `listconfigs`
  (`bitcoin-rpcuser/-rpcconnect/-rpcport`; `bitcoin-rpcpassword` is concealed as `...`,
  so the plugin reads it back from the config file line `listconfigs` points at, or
  needs the override); override `assetchan-elements-rpc=http://user:pw@host:port`,
  wallet `assetchan-elements-wallet`.
- **Options**: `assetchan-min-fee-reserve` (1000), `assetchan-max-fee-reserve` (10000),
  `assetchan-default-fee-reserve` (2000), `assetchan-close-to` (`elements`|`none`).
  Allowed assets come from core's `--channel-asset` via `listconfigs`.
- **RPCs**:
  - `assetchan-open id amount asset [fee_reserve] [announce=false] [close_to]`:
    `fundchannel_start(asset=, fee_reserve=, close_to=<elementsd unconfidential addr>)`
    → an **explicit** funding transaction: one unblinded UTXO of the asset and one of
    L-BTC (`listunspent`; if the wallet has none big enough it pays itself one with
    `sendtoaddress` to an unconfidential address first — a transaction with blinded
    inputs can't have all-explicit outputs), `createpsbt([asset_in, lbtc_in], [{addr:
    units, "asset": id}, {addr: reserve}, asset change, L-BTC change, {"fee": ...}])`
    (the shape Liquid testnet case T1 proved) → `walletprocesspsbt` → `finalizepsbt`
    → `decoderawtransaction` sanity (outputs 0/1 explicit, right asset/amount/script)
    → `fundchannel_complete(withhold=false)` → `sendrawtransaction`; on failure
    `fundchannel_cancel` + `lockunspent`. `walletcreatefundedpsbt` was dropped: it
    places change at a random position (which can land between outputs n and n+1)
    and blinds it.
  - `assetchan-close id [destination]`, `assetchan-list [asset]`, `assetchan-funds`
    (elementsd balances + channel balances), `assetchan-send addr asset amount`,
    `assetchan-newaddr`.
  - `assetchan-invoice amount asset label desc [expiry]`: POC = `invoice` with
    `exposeprivatechannels=[our channels in the asset]` (explicit hints: core would
    otherwise hint channels in any asset, and none through a peer without public
    channels) + datastore `["assetchan","invoice",<payment_hash>] → asset`; v2 = asset
    as an unassigned bolt11 tagged field via `createinvoice` (CLN keeps unknown fields:
    `common/bolt11.c:144,1393`).
  - `assetchan-pay bolt11 asset [maxfee]`: creates/updates askrene layer `assetchan.<asset>`
    disabling every channel known to be in another asset (`askrene-update-channel
    enabled=false`): our own (`listpeerchannels`) and the gossiped ones, whose asset the
    plugin reads from their funding output (`gossiped_channel_asset`: scid → block → tx →
    `gettxout` asset tag, cached), then `xpay layers=[...]`; private hops come from the
    invoice's route hints. `asset` may be `lbtc` for an ordinary payment that must keep
    off asset channels. `assetchan-listchannels [asset]` is `listchannels` with that
    classification.
- **Hooks**: `openchannel` — reject if asset not allowed or reserve outside
  [min,max], else `continue` with `close_to`; `htlc_accepted` — final hop only: incoming
  channel's asset (cache from `listpeerchannels` by scid/alias) must equal the invoice's
  asset, else `fail` with `temporary_channel_failure` (no `channel_update`): the payer
  then disables the channel it used for the last hop and tries other paths, where
  `incorrect_or_unknown_payment_details` would make it give up on the invoice.
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
5. **C5** + `test_unilateral_close[opener|peer]` (no HTLC in flight),
   `test_htlc_onchain[timeout|success-payer|payee]` (an HTLC in flight, resolved by
   the closer's HTLC-timeout or HTLC-success tx and the delayed sweep of its output,
   or by the other side's direct spend after the timeout or with the preimage; the
   payer's payment fails or completes from what it sees on-chain) and
   `test_penalty[opener|peer]` (a revoked commitment with an HTLC in it: the victim
   penalizes `to_local` and the HTLC and sweeps its `to_remote`). All sweep kinds of
   C5 are covered.
6. `git diff --stat master` → real numbers into DESIGN.md §4; port `run-tests.sh` for
   macOS Docker runs.
7. `test_gossip`: five nodes, announced asset channels l1-l2-l3-l4, a private one l2-l4,
   announced L-BTC channels l1-l5-l4; see "Gossip" in §7.

Reuse map (`git show liquid-merged:<path>`): `common/liquid_asset.[ch]`,
`bitcoin/tx.c`, `bitcoin/psbt.c`, `bitcoin/tx_parts.c`, `bitcoin/chainparams.c`
(testnet), `openingd/openingd.c` (TLVs, reserve sig), `channeld/commit_tx.c`
(asset outputs), `hsmd/{hsmd,libhsmd}.c`, `closingd/closingd.c` + `common/close_tx.c`,
`lightningd/opening_control.c` (params, checks), `lightningd/watch.c`,
`onchaind/onchaind.c` (asset output recognition only), tests.

### Verification

- `make && make check-units`; `TEST_NETWORK=liquid-regtest pytest -n 4 tests/test_assetchan.py`
  (Linux with `elementsd` on PATH, or the Docker runner).
- Upstream regression on Elements (ordinary channels there stay non-anchor; C4 enables
  anchors only with a channel asset): `contrib/assetchan/run-tests.sh tests/test_closing.py
  -k "test_closing_simple or test_closing_id or test_penalty_inhtlc or test_onchain_unwatch
  or test_onchaind_replay or test_onchain_their_unilateral_out"` passes (the `anchors`
  variants skip on Elements, as upstream has them); with C8, `tests/test_gossip.py -k
  "test_gossip_jsonrpc or test_announce_and_connect or test_gossip_persistence"` too
  (the `_via_dns` variant needs name resolution the container lacks).
- On-chain (elements-cli): funding vout 0 asset/units, vout 1 L-BTC/reserve, same script,
  both explicit; commitment has two inputs, anchors in L-BTC, fee output = reserve −
  anchors; sweeps pay fees from wallet L-BTC; assets at close addresses.
- Real Liquid testnet: `contrib/assetchan/nodes.sh` runs nodes from this tree against
  a testnet elementsd (`ASSETCHAN_NETWORK=liquid-regtest` for a regtest one, where it
  also mines); `contrib/assetchan/TESTNET.md` records the run of all the cases above
  (open, payments, cooperative close, force closes with every sweep kind, an HTLC
  claimed on chain with its preimage, a penalty) with the testnet txids.
- By hand: `contrib/assetchan/ui.py` serves a web page (http://127.0.0.1:8765) for those
  nodes, or any `--node name=lightning-dir` running the plugin: the nodes, peers,
  wallets and channels, with forms to open (asset or L-BTC), pay, close, force close,
  send on-chain and fund a node, all through the nodes' RPCs (`assetchan-*` for the
  asset side); it mines on regtest when given `--elements-rpc`.

## 7. Verify first (day 1 spikes), risks

- Why upstream disables anchors on Elements (`options.c:1854`): enable and run the
  upstream anchors tests on `liquid-regtest`. If anchors are genuinely broken there, fall
  back to the branch's non-anchor scheme (C4/C5 grow by ~400 lines).
- Elements relay policy: min relay fee and absence of package relay (ask the Elements
  team). If package relay exists/lands, the reserve shrinks to the anchors alone and the
  commitment fee can be 0.
- ~~`walletcreatefundedpsbt` with asset outputs + blinded change~~ (replaced by the
  explicit `createpsbt` flow, §5); `fundchannel_complete` accepts the PSET (verified on
  liquid-regtest); `createinvoice` preserving an unknown tagged field (v2 invoices).
- Fixed fee wastes reserve − anchors on every close: acceptable at 2,000 sat; a change
  output to the opener (as on the branch) is a later refinement. Core only requires
  reserve > 660 sat (the anchors); whether the rest relays is the plugin's policy
  (`assetchan-min-fee-reserve`), so a node must not accept reserves below Liquid's
  relay floor for the largest commitment it allows.
- The affordability checks (`full_channel.c`, `channeld`, `openingd`) still reserve
  660 "sat" for anchors out of the opener's balance; on an asset channel those are asset
  units the opener can't spend. Harmless, but wrong by 660 units; a later cleanup.
- Dust limit / `htlc_minimum` are negotiated in "satoshis" but mean asset units:
  plugin policy per asset later.
- **Gossip** (`test_gossip`). Announced asset channels propagate like any other once
  lightningd's block scan keeps explicit asset outputs for gossip verification (C8; before
  it, every node rejected the announcement with "no unspent txout"): the 2-of-2 script
  matches and the amount read is the asset's units, so every node lists them with
  capacity = units and nothing in
  `channel_announcement`/`channel_update` says which asset they are in; private ones are
  reachable only through the invoice's hints. The plugin tells gossiped channels apart by
  their funding output's asset tag and keeps its per-asset askrene layers accordingly, so
  `assetchan-pay` (in an asset or in `lbtc`) is routed over the right channels at once.
  A payer without the plugin (or plain `xpay`) may take an asset path for an L-BTC
  payment when gossip makes it look cheapest: its units travel asset-to-asset through
  the intermediates, the recipient's hook fails the HTLC as a temporary failure of the
  last channel, and the payer routes around it (one failed part); a wrong-asset forward
  is refused by core (`unknown_next_peer`), so funds never cross assets. A node with no
  channel in the asset cannot pay an asset invoice at all. Proper per-asset gossip (an
  asset in `channel_update`, or a `channel_announcement` TLV) stays a later core PR; until
  then announcing asset channels is a choice (`assetchan-open announce=true`), default off.
- Upstream review will ask about TLV numbers, the reserve, and anchors on Elements;
  the plugin is unaffected by whichever protocol detail changes.
