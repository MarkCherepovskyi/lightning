# EVM channels: research and plan

Lightning channels in ETH (later one ERC-20 per contract) with Core Lightning,
following `contrib/assetchan/ETH.md`: a Solidity contract keeps a Bitcoin-style
UTXO set and validates the Bitcoin transactions CLN makes; a backend plugin
replaces bcli and shows the contract to CLN as a chain of Bitcoin blocks. CLN
runs with `--network=regtest` and opens ordinary channels. **Goal: no change
to CLN.**

This is the plan, not the spec. The spec (`SPEC.md`) comes after the spikes
in section 5 and is what the implementation follows.

## 1. What CLN needs from the chain (research, file:line in this tree)

**The backend interface** is bcli's five methods (`lightningd/bitcoind.c:20`).
Run with `--disable-plugin=bcli --plugin=evmbackend.py`. Any JSON-RPC error
from the plugin is fatal to lightningd, so failures go inside `result`.
- `getchaininfo`: `chain` must be `"regtest"` (`chaintopology.c:1532`);
  `headercount == blockcount`, `ibd: false`; the chain must never get shorter.
- `getrawblockbyheight`: an 80-byte header plus segwit transactions.
  lightningd recomputes the hash (double SHA-256 of the header) and checks only
  the link to the previous block (`chaintopology.c:1129`). It checks no merkle
  root, proof of work, timestamp or coinbase content. Index 0 is always treated
  as the coinbase (`chaintopology.c:50`), so every block needs a dummy one.
  The order of transactions fixes short channel ids, so it must be the same for
  every node.
- `getutxout`: amount and script of an unspent output; gossip uses it on every
  P2WSH output of the block an announced channel names.
- `sendrawtransaction`: `{success, errmsg}`; "already in chain" counts as
  success, as in bcli.
- `estimatefees`: constant rates are fine (floor 253 sat/kw).

Polling is every 30 s (`--dev-bitcoind-poll` to change it). A fresh node starts
30 blocks below the tip.

**The transactions CLN makes** (default channel type: anchors, zero-fee HTLC
txs):
- P2WSH: the 2-of-2 funding output, `to_local`, `to_remote` (`1 OP_CSV`),
  anchors (330 sat), offered and received HTLCs, HTLC-tx outputs. The opcodes
  used are a small fixed set (`bitcoin/script.c`).
- P2WPKH, and **P2TR key-path spends**. There is no option that makes CLN avoid
  taproot on regtest. Wallet change, sweeps, penalties and anchor-CPFP change are
  all P2TR (`wallet/reservation.c:366`, `lightningd/onchain_control.c:1002`,
  `lightningd/anchorspend.c:291`), and the wallet later spends them.
- Sighash types: `ALL`, `SINGLE|ANYONECANPAY` (all anchor-channel HTLC
  signatures), and taproot `DEFAULT`.
- Timelocks: anti-fee-sniping `nLockTime` near the tip; commitment
  `nLockTime 0x20xxxxxx` (a timestamp) with the BIP68 disable bit in
  `nSequence`; CLTV on HTLC timeouts; CSV `to_self_delay` (regtest default 6),
  1, and 16 on anchors. Every tx is version 2.
- Chains of unconfirmed transactions and RBF replacements (anchor CPFP,
  zero-fee HTLC txs).

So the only thing that could force a CLN change is taproot. The fallback is a
`--dev` flag and a predicate next to each of the ~15 `is_elements` P2TR choices.

## 2. What exists on the EVM side (research)

- **Nothing to reuse wholesale.** There is no Solidity BIP143 for general
  transactions, no BIP341 sighash, and no Bitcoin Script interpreter or template
  matcher. BOB's `bitcoin-spv` (active, LGPL-3.0) has parsing helpers. Citrea's
  `WitnessUtils` (LGPL) handles witnesses. tBTC (GPL) parses only P2PKH/P2WPKH.
  We write our own core and borrow test ideas, keeping the contract's license
  clean (no GPL/BUSL/unlicensed code copied).
- **ECDSA:** `ecrecover` plus hints from the submitter (recovery id `v` and the
  key's Y coordinate). The contract checks parity, the recovered address and the
  compressed key against the script. About 3.5k gas per signature.
- **BIP340 Schnorr:** the known `ecrecover` trick (it computes `s·G − e·P`),
  with an `R.y` hint required even. About 6–8k gas. Existing BIP340 repos are
  unlicensed or unaudited, so we write it and test it on the official BIP340
  vectors. A taproot key-path spend verifies against the output key directly,
  so no tweak is needed in the contract.
- **Gas** (estimates, to be measured in spike S3): ~70k for a 1-in-1-out
  P2WPKH spend, ~150k for a commitment tx (four new outputs at 22.1k each
  dominate), ~85–120k for an HTLC tx. On Base or OP that is a fraction of a cent
  per tx; on Ethereum L1, about $0.10–0.20.
- **Infura** serves Base Sepolia, OP Sepolia, Arbitrum Sepolia, Ethereum
  Sepolia/Hoodi and others over HTTPS and WSS. Free tier: 3M credits/day.
  `eth_getLogs` costs 255 credits, with at most 10k results per call. Log
  *subscriptions* are charged per block, which on Base Sepolia's 200 ms blocks
  is ~130M credits/day, so **poll `eth_getLogs` every 30–60 s instead**.
  Base and Arbitrum support the `safe`/`finalized` tags.
- **Prior art:** nothing runs unmodified CLN on an EVM UTXO emulation. tBTC,
  Citrea and Rootstock parse Bitcoin txs in Solidity; Raiden-style EVM channels
  are not Bitcoin-compatible.

## 3. Design (proposed)

### 3.1 Contract `UtxoChain` (Solidity, Foundry, `contrib/evmchan/contracts`)

- **State:** `utxo[keccak(txid, vout)] = keccak(amount, scriptPubKey, height)`,
  one slot per output. The spender passes amount and script in calldata, which is
  cheaper than storing them. Plus `genesis` (deploy timestamp) and `interval`.
- **Virtual height** = `(block.timestamp − genesis) / interval`. It doesn't use
  `block.number`, which means different things on different L2s. Timelocks are
  enforced against this height, so enforcement is deterministic.
- **`submit(tx, prevouts, hints)`:**
  - Parse the segwit tx strictly: canonical varints, no trailing bytes.
  - Check every input's UTXO against the claimed amount and script.
  - Check the script by exact template:
    - P2WPKH.
    - P2TR key path (BIP341 sighash, BIP340).
    - P2WSH with the BOLT 3 scripts: 2-of-2, `to_local`, `to_remote`, anchor,
      offered HTLC, received HTLC.
  - Check the sighash: BIP143 for `ALL` and `SINGLE|ANYONECANPAY`, BIP341 for
    `DEFAULT`/`ALL`.
  - Check timelocks: nLockTime finality (height or timestamp), BIP68 CSV, CLTV.
  - Check that inputs ≥ outputs.
  - Then spend the inputs, create the outputs at the current height, and emit
    `Tx(height, rawtx)` with witnesses.
- **Any output script is accepted.** Only the templates above can be spent.
- **Deposit:** `deposit(scriptPubKey)` payable creates an output. A user funds a
  CLN wallet by depositing to a `newaddr` script.
- **Exit:** an output to a **segwit v16 program of 20 bytes** pays out ETH to that
  20-byte address instead of creating a UTXO. Its address is a normal `bcrt1s…`
  bech32m address, so a CLN user withdraws to ETH with plain
  `withdraw <exit-address>`. Spike S4 checks CLN accepts it.
- **Fee** (inputs − outputs) is credited to the submitter, who paid the gas.
- **Units:** 1 sat = 10 gwei (1 "BTC" of the chain = 1 ETH). Dust 546 sat ≈
  5.5 µETH. Regtest's `max_payment` (2^32 msat ≈ 0.043 ETH) caps a single payment
  unless large channels are negotiated; to be checked in spike S1.

### 3.2 Backend plugin `evmbackend.py` (Python, `contrib/evmchan`)

- **The five methods**, from the contract's `Tx` events, polled with
  `eth_getLogs`:
  - Block H = a header whose `prev_hash` is block H−1's hash, a fixed timestamp
    `genesis + H·interval`, and a merkle root over its txs (computed for realism,
    unchecked).
  - Then a dummy coinbase that commits to H, so every coinbase txid is unique.
  - Then H's transactions in event order.
- **Tip:** the plugin reports a height only once the EVM block containing its
  end is `safe` (testnet) or `finalized`. Empty heights are synthesized.
- **`sendrawtransaction`:** builds `prevouts` and `hints` (from its own index of
  outputs and from the signatures), sends `submit` as an EIP-1559 tx signed with
  its own key, and waits for the receipt. **There is no mempool:** a submitted tx
  is applied at once. Children of "unconfirmed" parents therefore work, and an
  RBF replacement fails because its inputs are spent, which CLN tolerates
  (it sees the original confirm).
- **`getutxout`:** from the plugin's output index, checked against the contract.
- **`estimatefees`:** constants at the floor.
- **Its own RPCs:** `evm-deposit amount` (to a new CLN address) and
  `evm-exitaddr` (the `bcrt1s…` address of an ETH account).
- **No new dependencies:** stdlib `urllib` JSON-RPC as in `elements.py`;
  `coincurve` (already a pyln dependency) for signing; a small vendored,
  vector-tested keccak-256 for EVM tx hashing.

### 3.3 Networks

- **Development:** local `anvil`, with time controlled through
  `evm_setNextBlockTimestamp`, so a test "mines" by advancing time.
- **Testnet:** Base Sepolia via Infura, OP Sepolia as a fallback. Both are OP
  Stack, have `safe` and `finalized`, and are the cheapest. Arbitrum's
  `block.number` quirk and Linea's slow finality rule those out first.
- **Intervals:** 1–2 s on anvil, 60 s on testnet. That keeps the `safe` lag a
  few heights, well under CLN's smallest CLTV delta (34).

## 4. Tests and docs

- **Contract (Foundry):**
  - Vectors are real transactions captured from CLN on Bitcoin regtest (funding,
    commitment, HTLC-success/timeout, sweeps, penalty, anchor CPFP, wallet P2TR
    spends), each also in tampered form that must be rejected.
  - The official BIP340/BIP341 vectors.
  - Fuzzing of the parser.
  - Gas reports per tx kind.
- **Plugin:** unit tests (block synthesis, keccak, EVM tx signing), then
  `tests/test_evmchan.py` with an anvil + contract fixture. The scenarios mirror
  `tests/test_assetchan.py`: open, pay, mutual close, unilateral close, HTLCs on
  chain, penalty, gossip, deposit and exit.
- **Docs:** `contrib/evmchan/SPEC.md` (the spec) and a README. Separate agents
  draft the contract tests, the plugin tests and the docs in parallel once the
  spec is agreed.

## 5. Spikes before the spec (small, thrown away or folded in)

| # | Question | How | Needs |
|---|---|---|---|
| S1 | Does unmodified CLN run on a synthetic chain from a plugin (sync, wallet deposits, open, pay, close between two nodes)? | A toy backend plugin with an in-memory "chain" that accepts every tx; two regtest nodes | nothing new |
| S2 | BIP340 + BIP341 in Solidity: correct on the official vectors, and gas | Foundry test | Foundry |
| S3 | BIP143 + ECDSA with hints on real CLN txs from S1 (commitment, SINGLE\|ACP HTLC tx); gas | Foundry test | Foundry |
| S4 | Does CLN `withdraw` to a segwit v16 address, and fund P2WPKH/P2TR from a deposit? | In S1 | nothing new |
| S5 | `eth_getLogs`, `safe`/`finalized` and credit use on Base Sepolia | A short script | Infura key |
| S6 | Pure-Python keccak + EIP-1559 signing accepted by anvil | Python test | Foundry (anvil) |

S1 and S4 can start now. They de-risk the CLN side without any EVM code.

### Results so far (2026-11-05)

- **S1 + S4 pass, 14/14, with CLN unchanged** (`spikes/s1-toychain`).
  - Setup: `chaind.py` is the toy chain. It applies a tx as soon as it is sent
    and checks inputs, amounts, nLockTime and BIP68, but not scripts.
    `toychain.py` is the backend plugin. Two regtest lightningd nodes run on it.
  - Passed:
    - Sync from block 0, and deposits to P2TR and P2WPKH wallet addresses.
    - Open and pay, then gossip verification through `getutxout`.
    - Mutual close.
    - **`withdraw` to a segwit v16 exit address** (`bcrt1s…`; CLN accepts it).
    - Force close with taproot sweeps after the CSV delay, ending with onchaind
      forgetting the channel.
    - Force close with an HTLC held by the peer: the HTLC-timeout tx goes out
      (`SINGLE|ANYONECANPAY` plus wallet inputs), and its output is swept.
  - Findings:
    - CLN sends timelocked txs early, takes the refusal, and retries every
      30–60 s. That is fine with no mempool, but tests have to keep the chain
      moving.
    - The 14 txs applied are kept as vectors
      (`spikes/s1-toychain/vectors/cln-regtest-txs.jsonl`).
- **S2 passes, 18/18** (`spikes/s2-taproot`).
  - BIP340 is checked with the `ecrecover` trick. All 19 official vectors pass,
    and 32 random ones.
  - The BIP341 sighash covers all 7 hash types and every official key-path
    input. A strict tx parser is included.
  - Gas: a Schnorr verify is about 5.1k. A whole 1-input key-path spend
    (parse, sighash, verify) is about 32k, and a 2-input one about 47k. The
    parser and the sighash precompute are unoptimized, so 2–3× savings are
    likely.
  - Rejections BIP340 would accept: keys with x in [n, p), and e or s equal to 0.
    None can be produced in practice.
  - **Taproot in the contract is feasible: no CLN change needed.**
- **S6 passes, 10/10** (`spikes/s6-pysigner/evm.py`, stdlib + coincurve).
  - Keccak-256, RLP, EIP-1559 signing (byte-identical to `cast mktx`), ABI,
    and logs.
  - A full `send()` to anvil takes about 6 ms. Pure-Python keccak runs at
    0.67 MB/s.
  - anvil's `safe`/`finalized` trail `latest` by 32/64 blocks by default; run
    it with `--slots-in-an-epoch 1` in tests.
- **S3 passes, 16 tests** (`spikes/s3-segwit`, building on S2's code).
  - Coverage: BIP143 for all hash types, strict DER with low-S, and ECDSA
    through `ecrecover` with on-chain-checked hints.
  - The BOLT 3 templates are matched byte-exactly, each spend path checked as
    Script would. Timelocks: nLockTime, BIP68 and CLTV.
  - **All 11 real CLN txs from S1 validate** at their height. Each timelocked
    one fails one block before its earliest valid height. There are also 136
    tampered cases and fuzzing of the hints.
  - Exec gas, before storage and events (each new output adds ~22.1k):
    - P2TR wallet tx: 46k
    - commitment: 74k
    - mutual close: 64k
    - sweep: 48k
    - HTLC-timeout (2 inputs): 95k
  - Strict parsing is a third of each total, and can be optimized.
  - Paths so far tested only on synthetic, byte-exact spends: revocations,
    HTLC-success, to_remote, anchors, penalties. A wider S1 run would give
    real vectors for them.
  - Not supported: non-anchor channel types, leases, P2SH/legacy inputs,
    taproot script paths. Outputs CLN might pay to those would be unspendable,
    so the plugin must keep CLN on the anchors channel type.
  - A fresh-agent adversarial review found four acceptances Bitcoin would
    refuse: no sigop limit, no size limit, null prevouts, input-sum range. All
    are fixed.
  - The deployed code is 20.8 KB, near the 24 KB limit, so the real contract
    is split into libraries.
- **S5**: waits for an Infura key.

## 6. Decisions for you

1. **Taproot in the contract** (no CLN change, ~300 more lines of Solidity), or
   the ~15-line CLN fallback. The plan assumes the contract, decided after S2.
2. **Network:** anvil, then Base Sepolia via Infura.
3. **Units:** 1 sat = 10 gwei.
4. **Exit by segwit v16 address**, and fees to the submitter.
5. **ETH first**, ERC-20 after.
6. **Licence of the new code:** MIT, like CLN's contrib.

Needed from you: OK to install Foundry (`foundryup`) for S2, S3, S6; an Infura
key in an environment variable for S5; later a funded Base Sepolia account (key
in an environment variable) to deploy.

## 7. Milestone results

### M1: the contract (2026-11-05)

`contracts/` implements SPEC draft 3.

- **Size and toolchain:** 23,150 bytes deployed, internal libraries only.
  Compiled with solc 0.8.30, measured on Osaka.
- **Tests:** 44, all passing.
  - **Real transactions:** all 42 real CLN txs, replayed in order through the
    real contract with state, events and the accounting invariant checked.
    - They cover 21 kinds: penalties, HTLC-success/timeout, remote claims,
      to_remote, anchor CPFP, mutual closes, exits and wallet txs.
    - Each timelocked tx also fails one block early.
  - **Rejections:** 610, each with its exact error.
  - **Synthetic spends:** 23, plus the worst-case commitment with 30 + 30 HTLCs,
    at 1.94M gas against the 12M target.
  - **Official vectors:** BIP340 and BIP341.
  - **Accounting:** deposits, exits, `withdraw`, and reentrancy.
- **Differential:** an independent Python reference (`reference/`) was written
  from the spec without reading the Solidity. The contract agrees with it on all
  7,648 tx cases and 112 deposit cases.
- **Gas** (execution, Osaka pricing):
  - commitment 79–89k;
  - HTLC-timeout 106–113k;
  - wallet P2TR spend 48–51k.

  With storage and calldata a commitment costs 224–337k.
- **Reviews:** two, by the builder's own reviewer and by an independent agent.
  They found no critical or high issues. The low ones are fixed or written into
  the spec:
  - a quadratic memory cost;
  - check order;
  - script numbers above 32 bits;
  - the "keys" reading;
  - the input-count limit, handled in plugin 5.3.
- **Vectors:** `spikes/s1-toychain/run_vectors.py` produced the real CLN txs of
  every kind on the toy chain.
- **A CLN crash, upstream code:** restarting a node while onchaind replays
  HTLC spends segfaulted lightningd. The crash is in `replay_block` →
  `replay_tx_hash_lock` (`lightningd/onchain_control.c`, replay code this
  branch doesn't touch). It looks like two replays for one channel at once.
  Check it on master and report it upstream.

### M2: btc.py, evm.py, the index and blocks (2026-11-06)

- **What was built:**
  - `btc.py`: byte-exact txs, coinbase, blocks and deposit txs, plus bech32 and
    the exit address.
  - `evm.py`: JSON-RPC, signing, ABI, chunked `eth_getLogs`, rate-limit
    retries, and errors that carry no secrets.
  - `evmindex.py`: the SQLite index of SPEC 5.2, draft 4.
  - `run-anvil.sh`.
- **Tests:** 125 Python tests, against a live anvil running the contract, plus
  a fake adversarial node.
  - The coinbase is checked at 37 heights against Bitcoin Core's encoding.
  - Bitcoin's genesis block and block 100000 match.
  - All 55 real CLN txs round-trip.
  - Deposit txids match the contract's.
- **The first independent review** found that the index trusted `eth_getLogs`.
  A clamped, lagging or load-balanced node could make it drop a confirmed tx
  for good, or fake a deep reorg. It also found these:
  - lost progress on a failed sync;
  - shallow reorg detection with `latest`;
  - API-key leaks;
  - a fatal fresh start.
- **The fix (SPEC draft 4).**
  - The contract keeps `txCount` and a running `logHash` of every applied tx:
    23,478 bytes, and about 10.6k more gas per tx.
  - The index advances the tip only on a checkpoint proven against them with
    `eth_call` pinned to the tag block's hash (EIP-1898).
  - Rows are deleted only on a confirmed fork, and repairs are staged and
    proven.
  - Stale backends are skipped, progress is committed per range, and
    rate-limited calls are retried.
- **The second independent review** found six more problems (one high: a
  missing block was treated as a fork). All are fixed, each with a regression
  test.

### M3: the backend plugin (2026-11-06)

- **What was built:** `evmbackend.py`.
  - The five backend methods: reads come from the index and never wait on a
    send.
  - The send path of SPEC 5.3: success once the node accepts a tx, per-job
    budgets, same-nonce replacement of re-signed txs, a tracker outside the
    send path, and filler sends for nonce gaps.
  - The hooks of 5.4, and the RPCs, alarms and wall-clock option check of 5.5
    and 5.6.
- **The scenario:** `scenario_anvil.py` runs spike S1's scenario with two
  unmodified lightningd on anvil and the real contract. **18/18 in about 3
  min.** The steps:
  - deposits, open, pay and gossip;
  - mutual close;
  - an exit, then the contract's `withdraw` paying the ETH address;
  - a force close with taproot sweeps;
  - an HTLC timed out on chain and swept;
  - refusals: a non-anchor channel, a node with regtest defaults, a peer
    without anchors.
- **Tests:** 172 Python, 46 Foundry.
- **Reviews:** four fresh-agent reviews.
  - Two had high findings, all fixed:
    - one stuck send starved the queue;
    - receipt polling grew without bound on the index's key;
    - "pending" answered as failure, so CLN unreserved inputs (double
      withdraws);
    - untracked nonce gaps;
    - HTLC double counting;
    - dust deposits blocking withdraws;
    - option minimums in blocks instead of time;
    - secrets in tracebacks;
    - a high-water mark that could wedge the key.
  - SPEC is draft 5.
- **Open:** our own outbound HTLCs rely on peers following 5.6 (SPEC open
  question 5).
- **For M4:**
  - allow, or avoid by mining, the plugin's tag/lag BROKEN alarms;
  - allow CLN's upstream CGOSSIP_CHANNEL_DEAD BROKEN line;
  - fund node keys before start;
  - wait on chain rather than on RPC results.

### M4: CLN's integration suite on anvil (2026-11-06)

- **What was built:**
  - `tests/test_evmchan.py` and `tests/evmchan_utils.py`: pyln-testing with
    no bitcoind. `EvmChain` runs one anvil and contract per test and stands
    in for the `bitcoind` fixture. `EvmNode` runs unmodified lightningd with
    `--disable-plugin=bcli`, the plugin and the 5.6 options.
  - `contrib/evmchan/run-tests.sh` and `Dockerfile`: image `evmchan/pytest`,
    with Foundry v1.9.0 pinned by sha256, pinned pip packages and a base image
    pinned by digest. It uses its own volume, and the tree is copied in without
    git.
- **Results:** every SPEC 7 scenario passes, with 182 unit tests, on macOS and
  in Docker (Linux arm64).
  - The scenarios:
    - deposits, open, pay and gossip;
    - mutual and unilateral close;
    - HTLCs on chain, both directions, from either commitment;
    - penalty;
    - refusals from both the opener and the accepter;
    - exits and `withdraw`;
    - restart mid-close;
    - reorgs above and through `safe`;
    - an outage;
    - regtest defaults;
    - `max_payment`.
  - Integration runs in about 3.5–4 min at `-n 4`.
- **Answered:** SPEC open question 1. `max_payment` does not cap large
  channels.
- **Plugin bugs found and fixed:**
  - **A reorg that takes back one of our mined sends wedged the key.** The
    plugin now keeps the last 64 mined sends, persisted. If the chain's nonce
    stays below them for an INTERVAL, it re-sends the mined version at the
    same nonce, all at once. A rebroadcast in that window reuses the nonce.
  - **A flaky M3 unit test.**
- **Upstream CLN bugs found:** both are worked around or xfailed, and neither
  is fixed in CLN. Report them upstream.
  - **A crash:** a `reject` from `openchannel2_changed` or
    `openchannel2_sign` is a use-after-free.
    - `hook_extract_psbt` returns false, and `plugin_hook.c` frees the
      payload, but `openchannel2_psbt_remove_dualopend` stays on dualopend.
    - Any peer offering a P2WSH input could crash the accepter.
    - The plugin aborts the open with `openchannel_abort` and then answers
      `continue`. The fail message is queued to dualopend first, so it exits
      before the PSBT is used.
  - **A replay bug:** when a tx spends two outputs onchaind watches, and
    onchaind ignores one of them, `replay_unwatch_txid` drops the whole tx
    after a restart. Its output's later spend is never seen, and the channel
    is never forgotten.
    - A strict xfail checks this exact symptom from after the restart. It
      also checks that the node is synced and serves the reference block.
    - `test_restart_mid_close[htlc]` goes to xfail only when CLN's coin choice
      triggers the bug.
  - **The M1 replay segfault** didn't happen in any M4 run. It is still to be
    checked on master.
- **Reviews:** two fresh-agent reviews of the tests. All findings are fixed:
  - vacuous xfails and log checks;
  - races in the reorg test;
  - a tautological fee assert;
  - accepter-side refusals that weren't exercised; they now use the dev-only
    `--evm-dev-no-outbound-filter`;
  - an unpinned runner.
- **Open:**
  - `openchannel2_sign` refusal is covered by unit tests only, because
    `_changed` always refuses first.
  - No test covers the 30-HTLC cap, pending sends resuming after a restart,
    or a send in flight during an outage.
