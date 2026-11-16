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
5. **Safe without the plugin.** Core only negotiates asset channels when a plugin
   offers their feature bit, and the plugin decides which assets; a node without it
   never takes one. Cross-asset forwarding is refused by the plugin's `htlc_accepted`
   hook; core won't start an asset channel unless the plugin's feature bit is set, so an
   asset channel never runs without it.

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
  by the plugin (plugins can set feature bits at startup), which also sets the
  `option_anchors_zero_fee_htlc_tx` bit (23) that lightningd clears on Elements; on
  Bitcoin, where that bit is already set, lightningd refuses to load the plugin.
  `open_channel` TLVs
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
  a small reserve (plugin default 2,000 sat). The commitment's fee is the same whatever it
  carries, so core caps the HTLCs to what it pays for at Elements' 0.1 sat/vB relay floor
  (`asset_commit_max_htlcs`; an explicit HTLC output is ~78 vB): each side accepts at most
  half (2,000 sat: 164 in all, 82 each; 1,000 sat: 36), and a peer asking for more is
  refused. Both channel reserves must be at least the dust limit, so a commitment always
  has a direct output to take whatever the HTLC outputs don't pay out.
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
  `to_remote`, HTLC outputs and HTLC-tx outputs, penalties), and each of our HTLC txs,
  is spent by a transaction whose input 0 is the asset output, going in full to output
  0 (the channel's close script, or the HTLC-tx output for HTLC txs); L-BTC from CLN's
  own wallet pays the fee (more inputs, P2WPKH change back to the wallet, the explicit
  fee output), capped per tx (`asset_fee_cap`, below). Core builds, signs (SIGHASH_ALL;
  HTLC txs keep BOLT 3's SIGHASH_SINGLE|ANYONECANPAY from both sides on their HTLC
  input), broadcasts and rebuilds it every block at the feerate its deadline calls for,
  the upstream sweep and HTLC-tx paths (`consider_onchain_rebroadcast`,
  `consider_onchain_htlc_tx_rebroadcast`, `wallet_utxo_boost`), until it is mined;
  without L-BTC in the wallet it logs that and retries every block. `to_remote` is
  swept the same way, from the `onchaind_add_utxo` notification upstream hands the
  wallet (which can't hold assets); onchaind doesn't wait for that sweep, so
  lightningd keeps the channel (and the sweep's rebroadcasting) until it is mined, even
  once onchaind is done with everything else. No plugin is on this path. (An earlier version had
  the plugin fund sweeps from elementsd, through SIGHASH_SINGLE|ANYONECANPAY signatures
  and an `asset_sweep` notification; reviewers showed that exposes penalties to
  replacement cycling and drops sweeps when the plugin is down, so it went.)
- **Anchor CPFP**: upstream's `anchorspend.c`, which builds Elements transactions too
  (the anchor and wallet coins are explicit L-BTC; P2WPKH change, explicit fee
  output; hsmd's `sign_anchorspend` signs the PSET unchanged). A commitment whose
  fixed fee falls short of the feerate an HTLC deadline calls for gets its anchor
  spent with wallet L-BTC, and the spend is RBF'd every block as the deadline nears.
  Upstream only bumps when the HTLCs are worth more than the fee; an asset channel's
  HTLCs are in the asset, so it pays what the feerate asks up to `asset_fee_cap`
  instead (aiming at a lower feerate if the cap is short). The plugin's anchors bit is
  node-wide, so ordinary L-BTC channels between plugin nodes are anchor channels too:
  they get upstream's rule unchanged (their HTLCs are L-BTC).
- **Fee cap** (`asset_fee_cap`, `lightningd/onchain_control.c`): we can't tell what the
  asset is worth in L-BTC, so every tx of ours (sweep, HTLC tx fee inputs, anchor
  spend) pays at most one fee reserve, which is what the opener set aside for fees.
  For a claim the peer could otherwise win (a penalty, an HTLC: success, timeout, or
  taken off the peer's commitment) that is within 12 blocks of its deadline, the cap
  is 10 fee reserves (20,000 sat at the default reserve): losing the claim costs the
  whole output. Outputs only we can spend (our delayed outputs, `to_remote`) and the
  low-priority anchor spend that just gets a commitment mined stay at one reserve.
  The fee still follows the deadline's feerate, so the cap only bites in a fee spike;
  change too small to be worth an output (at the capped feerate) goes to the fee, so a
  fee can exceed the cap by about the cost of one output plus dust.
- **Invariants**: one asset per channel (core). A forward leaves in the asset it arrived
  in: the plugin's `htlc_accepted` hook keeps core's outgoing channel (`forward_to`) if
  it is in that asset, else picks the peer's channel in that asset with the same fees
  that can send the most, else fails it `unknown_next_peer`; core enforces the rule
  itself too (`forward_htlc` fails a forward into another asset whatever `forward_to` a
  plugin returned, and `best_channel` never swaps to a channel in another asset), so it
  holds even if another plugin's hook has the last word or the plugin is gone. At the
  final hop the plugin checks only HTLCs that pay an invoice of ours (no invoice: core,
  or a swap plugin, decides). Core starts no asset channel without the plugin's feature
  bit (`peer_start_channeld`), and the plugin runs only as an `important-plugin` (it
  disables itself otherwise, which drops the bit), so lightningd stops if it dies.
- **Operational**: every node holding asset channels needs some L-BTC in CLN's own
  wallet (`newaddr`, then fund it: core pays sweep and HTLC-tx fees with it) and some
  unblinded L-BTC in its elementsd wallet (to fund opens), and sets a close address
  (its elementsd wallet) so swept assets are spendable.

## 4. CLN core delta ("small improvements")

| # | Change | Files | ≈ lines | Why not plugin |
|---|---|---|---|---|
| C1 | Channel asset identity: feature bit (set by the plugin); TLVs; `channel->asset`, `fee_reserve`; DB columns; `channeld/closingd/onchaind` init fields; `fundchannel_start asset= fee_reserve=`; `listpeerchannels.asset`, `fee_reserve_msat`; `openchannel` hook payload fields; `common/channel_asset.[ch]` (from `common/liquid_asset.c`) | `wire/peer_wire.csv`, `common/features.[ch]`, `openingd/*`, `lightningd/{options,channel,opening_control,peer_control,channel_control,closing_control}.c`, `wallet/{wallet,migrations}.c`, `*_wire.csv`, `doc/schemas` | 450 | daemons must know the asset |
| C2 | Asset tags on channel tx outputs; fee computation counts only the fee asset; tx parser reads asset amounts instead of asserting L-BTC | `bitcoin/{tx,psbt,tx_parts}.c`, `common/{initial_commit_tx,htlc_tx,close_tx}.c`, `channeld/commit_tx.c` | 150 | tx builders live in daemons |
| C3 | Reserve input + second signature + `hsmd` signing both inputs; `sign_last_tx`. Input 0 is signed with upstream's messages, which accept a tx that also spends the funding output's next sibling; input 1 with one generic message, `hsmd_sign_funding_sibling_input` (SIGHASH_ALL, input 1 must spend the output after input 0's; hsmd knows nothing of assets), so ordinary channels and external signers see upstream's messages unchanged | `openingd`, `channeld`, `closingd`, `hsmd/{hsmd,libhsmd}.c`, `hsmd_wire.csv`, `lightningd/peer_control.c` | 400 | protocol + signer |
| C4 | Anchors on Elements come from the plugin's feature bits (no core change); skip fee subtraction; fee output = reserve − anchors; ignore `update_fee` | `channeld/{commit_tx,channeld}.c` | 30 | |
| C5 | Asset sweeps: `onchaind_tx_unsigned` builds the asset-in → asset-out spend plus L-BTC wallet inputs, change and fee (capped by `asset_fee_cap`: the fee reserve, ten near a contested claim's deadline), all signed SIGHASH_ALL (hsmd allows wallet inputs next to an asset input); HTLC txs get wallet L-BTC on the upstream path; core broadcasts and bumps both; `to_remote` swept from the upstream `add_utxo` notification; `anchorspend` builds Elements anchor spends, capped the same way for asset channels | `lightningd/onchain_control.c`, `hsmd/libhsmd.c`, `onchaind/onchaind.c`, `lightningd/anchorspend.c` | 450 | `onchaind` + channel keys + fee safety |
| C6 | `fundchannel_complete` verifies output n (asset, amount, explicit) and n+1 (L-BTC, reserve); funding watch by asset | `lightningd/opening_control.c`, `lightningd/watch.c` | 60 | safety |
| C7 | `liquid-testnet` chainparams | `bitcoin/chainparams.c` | 35 | |
| ~~C8~~ | (Reverted on `feat/clean-up`.) Gossip verification of asset channels: recording every explicit P2WSH output, any asset, as "sats" let anyone back fake-capacity announcements with a self-issued asset, on every node of the fork. Asset channels are private again; the plugin refuses `announce=true` and offers of announced ones | — | 0 | |

Target was ≈ 1,500 lines (vs 4,370 on `liquid-merged`). **Measured** (`git diff --stat
master`, 2026-11-05, without `contrib/assetchan`, the pytest and the generated
`msggen/schema.json`): 81 files, +1,961/−341, of which unit/fuzz tests +298; by area:
common +563, lightningd +562 (C5's sweeps ≈ 230 of it), openingd +172, bitcoin +146,
hsmd +104, channeld +100, onchaind +99, closingd +65, doc +37, wire +34, wallet +27.

**What stays in core, and what the plugin owns.** Core keeps only what builds, signs,
or watches transactions: the daemons need the asset to produce valid Elements
transactions (C2, C3, C5) and hsmd must check what it signs (C3); plus one guard, that
an asset channel only runs while the plugin's feature bit is set. Everything else is the
plugin's: it turns asset channels on (it offers the feature bit, and the anchors bit on
Elements; core negotiates them only then), keeps forwards within an asset, picks the
assets (`assetchan-asset`), polices fee reserves, funds channels
from elementsd, ties invoices to assets, checks the asset of incoming payments, routes
payments over channels of the right asset, and pays for, broadcasts and bumps the
sweeps of asset outputs after a unilateral close (core signs them, the plugin can't:
it has no channel keys). That makes the plugin part of a node's safety: a sweep with a
deadline (a penalty, an HTLC) that the plugin doesn't get mined in time can be lost,
which is why it runs as an important plugin. Without the plugin a node is an
ordinary CLN node: upstream's `test_closing.py`, `test_connection.py` and
`test_pay.py` pass on Bitcoin regtest with this branch (`run-tests.sh` with
`ASSETCHAN_TEST_NETWORK=regtest`), except three connection tests
(`test_connect_transient[_pending]`, `test_networkevents`) that fail the same way on
master in that Docker setup. The biggest remaining piece, the fee reserve (C3),
goes away only when Elements relays packages, so that commitments can pay no fee and
be bumped through their anchors; a broadcaster-added fee input doesn't work, because
it changes the commitment's txid, which the pre-signed HTLC transactions commit to.
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
  `assetchan-asset` (multi): the assets we open and accept channels in.
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
  [min,max], else `continue` with `close_to`; `htlc_accepted` — on a forward, the
  outgoing channel must be in the incoming one's asset (§3 Invariants); at the final hop
  the incoming channel's asset (cache from `listpeerchannels` by scid/alias) must equal the invoice's
  asset, else `fail` with `temporary_channel_failure` (no `channel_update`): the payer
  then disables the channel it used for the last hop and tries other paths, where
  `incorrect_or_unknown_payment_details` would make it give up on the invoice.
- **Notifications**: `invoice_payment` (drop datastore entry). The plugin has no part
  in sweeps (core funds them from its own wallet, §3). Runs as `important-plugin`; loaded before `htlcs_resubmit`
  (`lightningd/lightningd.c:1422-1437`).

## 6. POC

Branch: `git checkout -b asset-channels master`. Commits:

0. `contrib/assetchan/DESIGN.md` = this spec.
1. **C1 + C2** ported from `liquid-merged`: `common/liquid_asset.c` → `common/channel_asset.c`
   (generic names), `bitcoin/tx.c`/`psbt.c`/`tx_parts.c` helpers, openingd TLVs, channel
   struct/DB/init messages, `listpeerchannels` fields, hook payload, feature bit.
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
  anchors; sweeps pay fees from elementsd's L-BTC; assets at close addresses.
- Real Liquid testnet: `contrib/assetchan/nodes.sh` runs nodes from this tree against
  a testnet elementsd (`ASSETCHAN_NETWORK=liquid-regtest` for a regtest one, where it
  also mines); `contrib/assetchan/TESTNET.md` records the run of all the cases above
  (open, payments, cooperative close, force closes with every sweep kind, an HTLC
  claimed on chain with its preimage, a penalty) with the testnet txids.
- By hand: `contrib/nodeui/ui.py` serves a web page (http://127.0.0.1:8765) for those
  nodes (`--network liquid-regtest --elements-rpc ...` as before), or any
  `--node name=lightning-dir`, also Bitcoin and EVM nodes: the nodes, peers, wallets,
  channels and asset sweeps, with forms to open (asset or L-BTC), pay, close, force
  close, send on-chain and fund a node, all through the nodes' RPCs (`assetchan-*`
  for the asset side); it mines on liquid-regtest. See `contrib/nodeui/README.md`.

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
  output to the opener (as on the branch) is a later refinement. That the commitment
  relays with every HTLC it can carry is core's (see §3, HTLC cap).
- The anchors (660 sat) come out of the fee reserve, never the asset balances: the
  affordability checks in `full_channel.c`, `channeld`, `openingd/common.c` and
  `lightningd/peer_control.c` skip them for asset channels.
- Dust limit / `htlc_minimum` are negotiated in "satoshis" but mean asset units:
  plugin policy per asset later.
- **Gossip** (`test_gossip`). Asset channels are never announced: nothing in
  `channel_announcement`/`channel_update` says which asset a channel is in, and counting
  asset units as capacity (the reverted C8) let any self-issued asset fake capacity. So
  gossip is all L-BTC; an asset payment goes over the payer's own asset channels and the
  invoice's route hints (`assetchan-invoice` adds them), which the plugin's per-asset
  askrene layer (`assetchan-layer`) leaves enabled while disabling everything else. A
  payer without the plugin can't stray onto asset channels it doesn't know. Proper
  per-asset gossip (an asset in `channel_update`, or a `channel_announcement` TLV, and the
  asset kept in the utxoset) stays a later core PR.
- Upstream review will ask about TLV numbers, the reserve, and anchors on Elements;
  the plugin is unaffected by whichever protocol detail changes.

## 8. Clean-up (`feat/clean-up`, 2026-11-12)

Reviewed by three models independently (and the hook question by four); what changed:

- Core, generic bug fixes (upstreamable on their own): the `htlc_accepted`/`peer_connected`
  hook staying "in use" after a chain-ending answer (`plugin_hook.c`); onchaind replay
  dropping a tx that spends two watched outputs when onchaind wanted only one
  (`onchain_control.c`, `test_onchaind_replay_tx_spending_two_watched`); a use-after-free
  when a plugin rejects `openchannel2_changed`/`_sign` (`dual_open_control.c`).
- Core, asset delta made fail-closed and smaller: forwards can't cross assets (above);
  C8 reverted; `anchorspend` off on all of Elements; hsmd signs to-us txs only with one
  input again (a `SIGHASH_SINGLE` signature on an input without an output commits to no
  outputs) for ordinary channels; a channel's asset must be an explicit tag and not
  L-BTC (`channel_asset_tag_valid`, checked by `fundchannel_start` and the fundee).
- Sweeps (decided after a second, four-model review of plugin hooks, which rejected a
  plugin-declared funding check outright and found plugin-funded sweeps unsafe as built):
  core funds asset sweeps and asset HTLC txs from CLN's own L-BTC wallet, signed
  SIGHASH_ALL, and bumps them itself; the `asset_sweep` notification, hsmd's
  SINGLE|ANYONECANPAY override for asset inputs, and the plugin's sweep code (and
  `elements.py`'s tx codec) are gone.
- Plugin: must run as `important-plugin`; final-hop check only for our invoices (after
  `keysend`); unknown channel = refuse; `assetchan-layer` (used by assetswap too);
  `assetchan-open`/`-pay` run in threads so the HTLC hook isn't held up; no gossip
  scanning of elementsd; no sweep code.
- Second pass (ten-model review of what more could leave core; `simplify/CONSOLIDATED`):
  about 1,100-1,500 core lines can't, while one node holds several Liquid assets and
  Elements has no package relay (the reserve input and its signatures, asset tags, the
  fundee's funding check, the start guard); a generic "channel tx profile" or moving the
  negotiation behind plugin-owned TLVs (H1) only renames that and grows core, so neither
  was done. Done: the HTLC cap above; reserves ≥ dust; the 660 deductions; one generic
  hsmd message for input 1 instead of four asset ones; assetswap takes new swaps only as
  an important plugin. Kept on purpose: `fundchannel_complete`'s asset/reserve checks
  (without them a raw-RPC opener could lock funds in a funding tx no commitment can
  spend) and onchaind's per-channel asset (moving its accounting out isn't simpler).
- Static channel backups (`staticbackup`, `emergency.recover`, peer storage) carry an
  asset channel's asset, fee reserve and close script (`scb_tlvs.asset_channel`, even
  type 4242: a node that can't restore an asset channel refuses rather than restore it
  as an ordinary one). `recoverchannel` rebuilds the stub as an asset channel, so when
  the peer closes, core sweeps our asset to the old close address (fee from the CLN
  wallet; `test_recover_asset_channel`). Restoring one also needed an upstream onchaind
  fix: grinding for a lease-CSV `to_remote`/`to_local` on an output it couldn't place
  overwrote the expected script, so a later output that was ours went unrecognised
  (always possible on a restored stub, which can't place the peer's `to_local`).
- Third pass (found while documenting, `contrib/assets-vault/`): every signature of ours is
  `SIGHASH_ALL`, HTLC txs and HTLC claims off the peer's commitment included (upstream
  `efdfcc6da`, ported: wallet inputs are added before signing, and our HTLC-tx input is
  signed again on every fee bump); asset channels hand watchtowers no penalty tx (upstream's
  can't spend an asset output); hsmd advertises `hsmd_sign_funding_sibling_input` and
  lightningd won't start offering asset channels with a signer that lacks it; closingd has no
  "trimmed" close variant for asset channels; `payassetinvoice` waits until leg 1 settles.
