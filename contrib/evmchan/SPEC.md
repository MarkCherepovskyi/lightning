# EVM channels: specification

Status: draft 3, 2026-11-05. Draft 2 was revised after an adversarial review
(the changes are in section 11). Draft 3 fixes the readings that M1's contract
and the independent Python reference had to choose (end of 4.3.7, and 4.5). It
is what the implementation follows. The reasoning and the spike results behind it are in `PLAN.md`.

MUST and MUST NOT are normative. Anything this spec doesn't decide is an open
question (section 10), not an implementation choice.

## 1. Scope

- **What.** Lightning channels in ETH on an EVM chain, with **no change to CLN's
  code**. CLN runs with `--network=regtest` and the options in section 5.6. It
  talks to a backend plugin instead of bcli, and the plugin talks to a contract
  that holds the ETH as a Bitcoin-style UTXO set.
- **Version 1** covers ETH on anvil (tests) and Base Sepolia (testnet).
- **Later** (section 9): ERC-20, a CLN network of its own (chainparams from
  config), gas optimisation, an L1 force-inclusion fallback, audit, mainnet.

Components and where they live:

| Path | What |
|---|---|
| `contrib/evmchan/contracts/` | Foundry project: `UtxoChain.sol` and its libraries, tests, deploy script |
| `contrib/evmchan/evmbackend.py` | The backend plugin (stdlib + coincurve) |
| `contrib/evmchan/evm.py` | EVM JSON-RPC, keccak, RLP, EIP-1559 signing, ABI (from spike S6) |
| `contrib/evmchan/btc.py` | Bitcoin tx/block serialization, bech32 (from spike S1) |
| `contrib/evmchan/run-anvil.sh` | Starts anvil and deploys the contract, for tests and local use |
| `tests/test_evmchan.py` | CLN integration tests on anvil |
| `contrib/evmchan/README.md` | How to run it |

New files are MIT-licensed. No GPL, BUSL or unlicensed code is copied in.

## 2. Units and constants

- **1 sat = 10 gwei = 10^10 wei.** So 10^8 sat = 1 ETH: the chain's "BTC" is ETH.
- **`MAX_MONEY` = 21,000,000 × 10^8 sat**, as in Bitcoin. Any amount or sum above
  it is invalid.
- **Deposits** MUST be a whole number of sats: `msg.value % 10^10 == 0`, and
  at least 1 sat.
- **`INTERVAL`**, the seconds per virtual block, is an immutable set at deploy:
  - 2 on anvil;
  - 60 on Base Sepolia.
- **`GENESIS_TIME`** and **`DEPLOY_BLOCK`** are immutables: `block.timestamp`
  and `block.number` at deploy.

## 3. The virtual chain

### 3.1 Heights

- **Height of an EVM block** with timestamp `t`:
  `H(t) = (t − GENESIS_TIME) / INTERVAL`, integer division.
  - Every transaction applied in that EVM block belongs to virtual block `H(t)`.
  - Heights don't depend on `block.number`, which differs between L2s.
- **Virtual block time** of height `h` is `T(h) = GENESIS_TIME + h × INTERVAL`
  (a u32; good until 2106).
- **Block 0** holds only its coinbase. The contract applies nothing at height 0:
  a deposit or submit whose `H(block.timestamp)` is 0 reverts with `TooEarly`.
- **Heights follow the wall clock**, not activity. If the EVM chain stalls or a
  node can't reach it, its timelocks still run down. This is the main
  difference from Bitcoin. Section 5.6 sizes CLN's delays for it, and 5.5
  alarms when it happens.

### 3.2 Block contents (byte-exact; every node MUST build identical blocks)

Virtual block `h` is:
- **An 80-byte header:**
  - version `0x20000000`;
  - `prev_hash` = the hash of block h−1 (all zeros for block 0);
  - the merkle root of the txids, computed as Bitcoin does (pairs, the last one
    duplicated when the count is odd);
  - time `T(h)`;
  - bits `0x207fffff`;
  - nonce 0.
  The block hash is the double SHA-256 of the header, as Bitcoin computes it.
- **Then the tx count** (Bitcoin varint), and the txs.
- **Tx 0 is the coinbase**, serialized without witness:
  - version 2;
  - one input: prevout = 32 zero bytes, index `0xffffffff`;
  - scriptSig = `len(n) || n || 07 "evmchan"`, where `n` is `h` as a
    little-endian CScriptNum with no leading zero bytes, plus `00` if its top
    bit is set; `n` is `00` for h = 0. So it is always a data push, never
    `OP_1`…`OP_16`;
  - sequence `0xffffffff`;
  - one output: value 0, script `6a` (OP_RETURN);
  - locktime 0.
- **Then every `TxApplied` event of height h**, in EVM order (block number,
  then log index). Each is the raw tx exactly as the event carries it, with
  witness for submitted txs.

### 3.3 Deposits as transactions

- **`deposit`** creates one output. It appears in the chain as a synthetic
  deposit tx, so that CLN's wallet sees it like any other tx, serialized
  without witness:
  - version 2;
  - one input: prevout txid = `keccak256(abi.encodePacked("evmchan deposit",
    block.chainid, address(this), depositNonce))` (the 32 bytes as stored,
    which is internal byte order), index 0;
  - empty scriptSig;
  - sequence `0xffffffff`;
  - one output: the amount and the given scriptPubKey;
  - locktime 0.
- **Its txid** is the double SHA-256 of those bytes. The contract computes it,
  so the output's outpoint is `(txid, 0)`.
- **`depositNonce`** starts at 0 and goes up by one per deposit.
- **The fake prevout** never exists in the UTXO set, so it can't be spent. It
  is not the null prevout, so CLN doesn't take the tx for a coinbase.

### 3.4 The tip

- **Confirmation tag.** The plugin has one: `safe` by default, or `latest`.
  `finalized` is not supported: on OP Stack it lags 15–20 minutes, which
  section 5.6's delays don't cover.
- **Tip.** Let `S` be the timestamp of the EVM block at that tag. The candidate
  tip is `H(S) − 1`, or 0 if that is negative.
  - Any tx at a height ≤ the candidate is in an EVM block with timestamp
    < `T(candidate + 1)` ≤ `S`, so it is at or below the tag.
- **The reported tip is the highest candidate seen so far**, persisted.
  - lightningd treats a backend whose chain gets shorter as fatal, so the tip
    never goes down (`chaintopology.c:1541`).
- **Blocks ≤ the reported tip are always built from the current index**
  (section 5.2).
  - If the EVM chain reorganises, a served block can change. CLN handles that
    itself: it follows `prev_hash` and rolls back (`chaintopology.c:1129`).
  - The plugin MUST NOT cache a block in a way that hides such a change.
- **Lag.** `L`, the lag in virtual blocks, is the current height `H(latest)`
  minus the reported tip, plus CLN's own poll time. Section 5.6 bounds it.

## 4. The contract `UtxoChain`

### 4.1 State

- **`mapping(bytes32 => bytes32) utxo`.** The key is
  `keccak256(abi.encodePacked(txid, uint32 vout))`; the value is
  `keccak256(abi.encodePacked(uint64 amount, bytes scriptPubKey, uint32 height))`.
  Zero means no such unspent output. `txid` is in internal byte order.
- **`mapping(address => uint256) withdrawable`** holds exits and fees, in wei.
- **Immutables:** `GENESIS_TIME`, `INTERVAL`, `DEPLOY_BLOCK`.
- **`depositNonce`.**
- **Invariant:** `address(this).balance` ≥ the sum of all UTXO amounts × 10^10
  + the sum of `withdrawable`.

### 4.2 Interface

```solidity
struct Prevout { uint64 amount; bytes scriptPubKey; uint32 height; }

event TxApplied(uint32 indexed height, bytes32 indexed txid, bytes rawTx);

function deposit(bytes calldata scriptPubKey) external payable returns (bytes32 txid);
function submit(bytes calldata rawTx, Prevout[] calldata prevouts) external returns (bytes32 txid);
function withdraw() external;                 // pays msg.sender its withdrawable
function height() external view returns (uint32);
function isUnspent(bytes32 txid, uint32 vout, uint64 amount, bytes calldata scriptPubKey,
                   uint32 height) external view returns (bool);
function utxoKey(bytes32 txid, uint32 vout) external pure returns (bytes32);
```

- **`deposit`**
  - Checks the value rules (section 2).
  - Checks the scriptPubKey:
    - at most 10,000 bytes;
    - not an exit script (4.4);
    - not starting with `6a` (OP_RETURN, which could never be spent).
  - Builds the deposit tx (3.3) and stores its output at the current height.
  - Emits `TxApplied` with the deposit tx's raw bytes.
- **`submit`**
  - Validates `rawTx` against `prevouts` at height `H(block.timestamp)` (4.3).
  - Applies it (4.4) and emits `TxApplied` with `rawTx`.
  - Reverts on any failure, with the custom errors listed in `UtxoChain.sol`.
  - **Anyone may submit any valid tx.** A third party who copies a pending
    `submit` gets the fee, and the tx still gets mined, which is what its owner
    wanted. Fees here are tiny (5.3), so this is accepted.
- **`withdraw`**
  - Zeroes the caller's balance first, then sends it with `call`
    (checks-effects-interactions).
  - Exits and fees are pulled this way rather than pushed, so a recipient that
    reverts can't block a channel transaction.

### 4.3 Validation (consensus)

A tx is valid at height `h` if and only if everything below holds. Anything not
listed is invalid.

1. **Encoding.**
   - The size is at most 100,000 bytes.
   - The parse is strict: canonical varints and no trailing bytes.
   - The segwit marker and flag are `00 01`, and are present only if some
     witness is non-empty.
   - There is at least one input and at least one output.
   - The version is read as a uint32.
2. **Inputs.**
   - `prevouts.length` equals the input count.
   - No prevout is null, and none is duplicated.
   - For each input, `utxo[key]` equals the commitment of the prevout.
   - Every scriptSig is empty: segwit only.
3. **Amounts.**
   - Every output, the sum of the inputs and the sum of the outputs are each at
     most `MAX_MONEY`.
   - The sum of the inputs is at least the sum of the outputs.
   - The difference is the fee.
4. **Sigops.** `4 × (legacy sigops in all output scripts) + 4 × (input count)`
   MUST be at most 16,000.
   - Legacy sigops are counted as Bitcoin's `GetSigOpCount(false)`:
     `OP_CHECKSIG(VERIFY)` counts 1 and `OP_CHECKMULTISIG(VERIFY)` counts 20.
   - Inputs are counted at 4 each, because no template path has more than 4.
5. **Lock time (BIP113 semantics).** The tx is final at `h` if either:
   - every input's sequence is `0xffffffff`; or
   - `nLockTime < 500,000,000` and `nLockTime < h`; or
   - `nLockTime ≥ 500,000,000` and `nLockTime < MTP(h)`, where
     `MTP(h) = T(max(h − 6, 0))`. That is the median of the 11 previous block
     times, since they increase by `INTERVAL`.
6. **Relative lock (BIP68).** For version ≥ 2, for each input whose sequence has
   bit 31 clear:
   - if bit 22 is set (a time lock), the tx is invalid (unsupported);
   - otherwise it needs `h − prevout.height ≥ (sequence & 0xffff)`.
7. **Each input's scriptPubKey** MUST be one of:
   - **P2WPKH** `0014<20>`: the witness is `<sig> <33-byte compressed key>`,
     `hash160(key)` equals the program, and the sig checks under BIP143.
   - **P2TR** `5120<32>`, key path only:
     - the witness is one item of 64 bytes, or 65 bytes with a defined hash type
       other than `0x00`;
     - no annex and no script path;
     - BIP340 against the output key under the BIP341 sighash.
   - **P2WSH** `0020<32>`:
     - the last witness item is the witness script, and its SHA-256 equals the
       program;
     - the script is byte-for-byte one of the templates below, as
       `bitcoin/script.c` builds them for the anchors channel type;
     - the other witness items satisfy one of its paths exactly as Bitcoin
       Script would.

   Definitions used in the table:
   - **`CSV n`** is Bitcoin's `CheckSequence`, exactly:
     - the tx version as a uint32 is ≥ 2;
     - the input's sequence has its disable bit (31) clear;
     - its type bit (22) matches that of `n`, which is always "blocks" here;
     - `sequence & 0xffff ≥ n`.

     BIP68 (rule 6) applies on top. Checking the input's age directly is NOT
     equivalent.
   - **`CLTV x`** is Bitcoin's `CheckLockTime`, exactly:
     - `nLockTime` and `x` are both heights (both < 500,000,000) or both times;
     - `nLockTime ≥ x`;
     - the input's sequence is not `0xffffffff`.

   The P2WSH templates:

   | Template | `script.c` | Paths (witness before the script) |
   |---|---|---|
   | 2-of-2 | `bitcoin_redeem_2of2` | `<> <sig1> <sig2>`, sigs in the script's key order; NULLDUMMY |
   | to_local, HTLC-tx output | `bitcoin_wscript_to_local`, `bitcoin_wscript_htlc_tx` | delayed: `<sig> <>` and `CSV to_self_delay`; revocation: `<sig> <01>` |
   | to_remote | `bitcoin_wscript_to_remote_anchored` | `<sig>` and `CSV 1` |
   | anchor | `bitcoin_wscript_anchor` | `<sig>`; or `<>` and `CSV 16` |
   | offered HTLC | `bitcoin_wscript_htlc_offer` | revocation: `<sig> <revocationpubkey>`; remote success: `<remotesig> <32-byte preimage>` and `CSV 1`; local timeout (HTLC-timeout tx): `<> <remotesig> <localsig> <>` and `CSV 1` |
   | received HTLC | `bitcoin_wscript_htlc_receive` | revocation: `<sig> <revocationpubkey>`; local success (HTLC-success tx): `<> <remotesig> <localsig> <32-byte preimage>` and `CSV 1`; remote timeout: `<sig> <>` and `CLTV cltv_expiry` and `CSV 1` |

   - The CSV and CLTV numbers are read from the script, as CScriptNum of up to
     5 bytes.
   - Any other scriptPubKey or script can be paid to, but its outputs can never
     be spent. Section 5.4 keeps CLN from producing such outputs.

   **Readings fixed in draft 3.** These are conservative. Bitcoin would accept a
   little more in some of these cases, but CLN never needs it.
   - **CSV operand.** `n` is compared whole with `sequence & 0xffff`. So
     `n ≥ 2^16`, or `n` with bit 22 or 31 set, is never satisfied. In Bitcoin,
     bit 31 makes CSV a no-op.
     - A to_local with such a delay is unspendable.
     - CLN's `max-locktime-blocks` (2016 by default) keeps delays far below that.
   - **Timeout dummy.** The HTLC paths' dummy item MUST be exactly `<>`. Bitcoin
     accepts any non-32-byte item, but CLN always uses `<>`.
   - **2-of-2 key order.** The keys MUST be in `script.c`'s order, sorted
     lexicographically. An unsorted script is not the template.
   - **to_remote delay.** Its CSV MUST be exactly 1. Lease delays are rejected.
   - **Keys.** A key MUST be a valid compressed point when a signature on the
     taken path is checked against it, as in Bitcoin. The anchor's key MUST be
     one on its `<>` path too. A key that no check uses on the taken path is
     not validated, for example the delayed key on a revocation spend.
   - **Script numbers.** They are at most `0xffffffff`, as `add_number` writes
     them. A bigger one makes the script not a template.
   - **Stack items.** Sizes are fixed by the templates, so Bitcoin's 520-byte
     limit is never reached.
   - **Prevout height.** It comes from the stored commitment, so it can't exceed
     `h`.
   - **Null prevout.** It is Bitcoin's: txid all zeros *and* index `0xffffffff`.
   - **Size.** The 100,000-byte limit counts the raw serialization, witness
     included, not weight.
   - **Same-block spends.** An output created earlier in the same virtual block
     can be spent, for example an anchor CPFP child of its commitment.
   - **Sigops.** They are counted over every output script, OP_RETURN and exits
     included.
   - **Genesis time.** It MUST be a real timestamp, well above 553,648,127.
     Commitments carry a time lock of `0x20xxxxxx`, which needs
     `MTP(h) > 0x20ffffff` to be final, so tests MUST NOT start the chain at a
     small timestamp. A real deploy always satisfies this.
8. **Signatures (segwit v0, ECDSA).**
   - Strict DER (BIP66) with low S.
   - A defined hash type: ALL, NONE or SINGLE, each optionally with
     ANYONECANPAY.
   - The BIP143 sighash. SINGLE with no matching output uses a `hashOutputs`
     of zero, as BIP143 specifies.
   - Keys are 33-byte compressed points on the curve.
   - Policy rules, which only ever reject more: MINIMALIF, NULLFAIL, NULLDUMMY.
9. **Signatures (taproot, BIP340).**
   - The BIP341 sighash with hash types DEFAULT, ALL, NONE, SINGLE, each
     optionally with ANYONECANPAY.
   - **SINGLE with no matching output is invalid**, as BIP341 specifies; unlike
     BIP143.
10. **How the contract verifies signatures.** Callers pass no hints.
    - **ECDSA:** compute the key's Y with the modexp precompile (a square root
      mod p, parity from the prefix). Then call `ecrecover` with recovery id 0,
      and with 1 if that fails. Accept only if the recovered address equals the
      key's.
      - The one valid signature this rejects is one with R.x ≥ n, which has a
        chance of 2^-128.
    - **BIP340:** compute R.y = the even `lift_x(R.x)` the same way, and use the
      `ecrecover` trick from spike S2.
      - It rejects what BIP340 would accept only for an output key with x in
        [n, p), or for e = 0 or s = 0. Nobody can produce any of those.
    - **Soundness** is argued in `spikes/s3-segwit/src/Ecdsa.sol` and
      `spikes/s2-taproot/src/Schnorr.sol`:
      - points are compared by keccak address, so a forgery needs a 160-bit
        second preimage;
      - the chain's `ecrecover` MUST return zero for x off the curve and for the
        point at infinity, which is true of geth and revm (Base, OP).

### 4.4 Applying a valid tx

- **Inputs:** delete `utxo[key]` for each.
- **Outputs**, for each output `i`:
  - **Exit** (`6014<20 bytes>`, segwit v16): add `amount × 10^10` to
    `withdrawable[address(bytes20)]`. No UTXO is created.
  - **OP_RETURN** (first byte `6a`): no UTXO.
  - **Anything else:** store `utxo[key(txid, i)] = commitment(amount, script, h)`.
- **Fee:** add `fee × 10^10` to `withdrawable[msg.sender]`.
- **Event:** emit `TxApplied(h, txid, rawTx)`.

### 4.5 Size, gas, structure

- **Size.** The deployed contract MUST be under 24 KB. Parsing, the sighashes,
  signatures and templates live in internal libraries (`BtcTx`, `Bip143`,
  `TaprootSighash`, `Ecdsa`, `Schnorr`, `Secp256k1`, `Templates`), compiled into
  the one contract. M1 measured 23,150 bytes.
- **EVM.** It needs Cancun opcodes (`mcopy`). Gas is measured with Osaka pricing
  (EIP-7883 modexp).
- **Errors.** They are in `Errors.sol`.
- **Interface details fixed in M1:**
  - `depositNonce` is a `uint256`;
  - deposits above `MAX_MONEY` revert;
  - `withdraw()` with nothing owed reverts `NothingToWithdraw`;
  - `isUnspent`'s last parameter is named `height_`.
- **Per-transaction gas cap.** Any tx CLN can make for a channel within section
  5.6's limits MUST fit in 12M gas, with margin under the 16,777,216 per-tx cap
  of EIP-7825.
  - **The worst case** is a commitment with 30 HTLCs each way and 4 more
    outputs. It is tested.
- **Gas targets** (execution only, before storage; modexp is priced as in
  EIP-7883, which adds about 4–8k per signature for the key recovery):
  - a commitment ≤ 100k;
  - an HTLC-timeout ≤ 125k;
  - a wallet P2TR spend ≤ 60k.

## 5. The backend plugin `evmbackend.py`

### 5.1 Options

```
lightningd --network=regtest --disable-plugin=bcli \
  --plugin=contrib/evmchan/evmbackend.py \
  --evm-rpc=<url> --evm-contract=<address> [section 5.6 options] ...
```

- **`evm-rpc`:** the JSON-RPC URL.
  - If it contains `${INFURA_API_KEY}`, that is read from the environment.
  - The key MUST NOT be written to files or logs.
- **`evm-contract`:** the contract's address.
  - At startup the plugin checks there is code at it and reads `DEPLOY_BLOCK`,
    `GENESIS_TIME` and `INTERVAL`.
  - Logs are scanned from `DEPLOY_BLOCK`. No start-block option exists, so none
    can be wrong.
- **`evm-key-file`:** the node's own ETH private key, which pays for gas.
  - Default: `<lightning-dir>/evm-key`, created with mode 0600 if it doesn't
    exist.
  - Keys are per node: there is no environment override. The deploy script uses
    its own `DEPLOYER_PRIVATE_KEY`.
- **`evm-confirm`:** `safe` (default) or `latest`.
- **`evm-poll`:** seconds between polls.
  - Default 30, which suits Infura's free tier.
  - Use 1 on anvil.
- **`evm-min-balance`:** the ETH below which the plugin refuses new channels and
  logs at BROKEN level. Default 0.01 ETH.

The plugin is `dynamic: false` and MUST NOT call lightningd during `init`.
lightningd waits on the backend plugin at startup, as it does with bcli.

### 5.2 The index

- **Storage:** a SQLite file `<lightning-dir>/evmchan.sqlite3` (stdlib
  `sqlite3`).
- **What it holds:**
  - every `TxApplied` log from `DEPLOY_BLOCK` to `latest`: EVM block number
    and hash, log index, height, txid, raw tx;
  - the outputs of each tx;
  - their spent status.
- **Polling:** each poll is one `eth_getBlockByNumber` for the tag and one for
  `latest`, then `eth_getLogs` over the new range.
  - Ranges start at 2,000 blocks and are split in half whenever the node
    reports too many results (Infura caps at 10,000).
  - It does not use log subscriptions, which Infura charges per block.
  - At a 30 s poll that is about 1.2M Infura credits per node per day, under
    the 3M free tier for two nodes.
- **Reorgs:**
  - each poll re-reads logs from the last tag block forward;
  - it drops rows whose EVM block hash no longer matches;
  - blocks are rebuilt from what remains (section 3.4).
- **Our own submits:** after a successful submit the plugin MUST add the
  receipt's `TxApplied` log to the index at once, before the next poll, marked
  with its EVM block. This is so children of that tx find their prevouts:
  anchor CPFP, and HTLC txs re-signed with new wallet inputs. It also makes
  "already in chain" and `getutxout` correct straight away.

### 5.3 The five backend methods

**Read methods never wait on a send.** `getchaininfo`, `getrawblockbyheight`,
`getutxout` and `estimatefees` MUST answer from the index without waiting on any
`sendrawtransaction` in progress. The plugin handles sends in a worker thread,
using pyln's async method replies.

- **`getchaininfo`:** `{chain: "regtest", headercount: tip, blockcount: tip,
  ibd: false}`. Until the first index sync after startup is done, it reports
  `ibd: true`.
- **`getrawblockbyheight h`:** for h ≤ tip, the block from 3.2 with its hash;
  above the tip, `{blockhash: null, block: null}`.
- **`getutxout txid vout`:**
  - from the index, as of `latest`, the amount in sats and the script. That
    matches bitcoind's mempool-inclusive `gettxout`;
  - `{amount: null, script: null}` if the output is unknown, spent, an exit or
    an OP_RETURN.
- **`estimatefees`:** constant: `feerate_floor` 1000 sat/kvB, and
  `[{2,5000},{6,4000},{12,3000},{100,1000}]`.
  - The in-tx fee only goes to whoever submits, and the real cost is gas, so
    constants keep CLN's fee logic quiet.
  - With anchors, CLN's commitment feerate is at least 1250 sat/kw; that is
    fine.
- **`sendrawtransaction tx`** MUST return within `2 × INTERVAL` seconds:
  1. Parse the tx. If its txid is in the index, return success ("already in
     chain").
  2. Build `prevouts` from the index (amount, script, height). If an input is
     unknown or spent, return `{success: false, errmsg: "missing or spent
     inputs"}`.
  3. `eth_call` `submit` at `latest`. If it would revert, return failure with
     the error name, for example `NonFinal`.
     - That is normal for CLN's early broadcasts, which it retries, and it
       costs no gas.
     - Then `eth_estimateGas`. Above 12M, return failure ("too large for one
       EVM tx: N gas").
       - M1 measured a P2TR input at about 35k gas and a P2WPKH input at about
         43k. So a wallet tx with more than roughly 300 inputs can never be
         mined here, though Bitcoin would allow it.
       - The `rpc_command` hook of 5.4 also rejects `withdraw`, `fundpsbt`,
         `utxopsbt` and `fundchannel` calls that would spend more than 200 UTXOs.
  4. Sign `submit` as an EIP-1559 tx from the node key, with the nonce from
     `eth_getTransactionCount(pending)`, and send it.
     - Wait for the receipt up to the time limit. If it arrives, ingest it
       (5.2) and return success or failure.
     - If it doesn't, return failure ("pending") and keep tracking it. If it is
       still pending after another `INTERVAL`, replace it with the same nonce at
       1.25× the fees.
  5. Sends are serialized per node key.

The plugin MUST NOT return a JSON-RPC error from these methods: lightningd
treats that as fatal.

### 5.4 Keeping CLN on what the contract can spend

- **Channel types.** Only `anchors_zero_fee_htlc_tx` channels, with static
  remote key, optionally with the `scid_alias` and `zeroconf` bits.
  - A `peer_connected` hook disconnects peers that don't offer feature 22/23.
    CLN would otherwise fall back to a static-remotekey channel when opening to
    them (`lightningd/channel.c:265`). Its HTLC outputs would be unspendable.
  - An `rpc_command` hook rejects `fundchannel`, `fundchannel_start`,
    `multifundchannel` and `openchannel_init` calls whose `channel_type` isn't
    an allowed one.
  - The `openchannel` and `openchannel2` hooks reject inbound opens of any
    other type.
- **Leases.** Liquidity-ad leases (`option_will_fund`) are rejected in
  `openchannel2`. Their `to_remote` has a lease CSV that the templates don't
  cover.
- **Dual funding.** `openchannel2`, `openchannel2_changed` and
  `openchannel2_sign` reject a PSBT whose peer inputs aren't P2WPKH or P2TR.
  Such a funding tx would never get mined.
- **Splicing** has no hook to check the peer's inputs. Version 1 runs with
  splicing off (section 5.6); open question 4.
- **HTLC count.** The `htlc_accepted` hook fails a forward if its outgoing
  channel already has 30 HTLCs pending, so no commitment can exceed the gas cap
  (4.5). `openchannel`/`openchannel2` reject peers whose `max_accepted_htlcs`
  is above 30.

### 5.5 Its own RPCs and alarms

- **`evm-deposit amount [address]`:** deposits `amount` sats from the node key
  to `address`, or to a fresh `newaddr` (P2TR) of this node.
- **`evm-exitaddr ethaddress`:** the `bcrt1s…` bech32m address (segwit v16, the
  20 bytes) that `withdraw` pays out to that ETH address.
- **`evm-withdraw`:** calls the contract's `withdraw` for the node key (fees,
  and exits to it).
- **`evm-status`:** contract, chain id, the node's ETH address and balance, its
  `withdrawable`, tip, current height, lag `L`, the tag's age, pending sends.
- **Alarms**, logged at BROKEN level:
  - `L` above a third of the smallest margin in 5.6;
  - the tag not advancing for `10 × INTERVAL`;
  - the node's ETH balance below `evm-min-balance`;
  - an index-sync error that lasts longer than `5 × INTERVAL`.

### 5.6 CLN options (normative)

Regtest's defaults (`lightningd/options.c:971`) assume instant blocks:
`watchtime-blocks` 6, `cltv-delta` 6, `cltv-final` 10. CLN goes on chain for an
HTLC only `(cltv-delta + 1)/2` blocks before its expiry (`peer_htlcs.c`). With a
lag of `L` virtual blocks, penalties and HTLC claims need margins well above
`2L`, plus time to survive an outage of the EVM chain (3.1).

**Every node on this network MUST run with:**

| Option | anvil (INTERVAL 2) | Base Sepolia (INTERVAL 60) | Why |
|---|---|---|---|
| `--watchtime-blocks` | 40 | 1440 (1 day) | the to_self_delay we demand: time to penalise a cheater |
| `--cltv-delta` | 40 | 144 (2.4 h) | half of it is our on-chain margin for HTLCs |
| `--cltv-final` | 40 | 144 | the same for payments we receive |
| `--funding-confirms` | 1 | 3 | depth before using a channel |
| `--max-concurrent-htlcs` | 30 | 30 | the gas cap (4.5) |
| `--developer --dev-force-features=-63` | yes | yes | splicing off (5.4) |
| `--developer --dev-bitcoind-poll=1` | yes | no (CLN's 30 s poll is half a block) | CLN polls the backend every 30 s otherwise |

The plugin checks these after `init`, once lightningd answers `listconfigs`,
from a background thread.
- If any is below these minimums, it logs at BROKEN level.
- It then refuses every channel open (all hooks in 5.4) until the node is
  restarted with correct values.
- The anvil values are for tests only. The integration tests set them through
  pyln-testing's options.

## 6. Networks and deployment

- **anvil:**
  - run with `--slots-in-an-epoch 1`, so `safe` trails `latest` by one block;
  - tests control time with `evm_setNextBlockTimestamp` and `evm_mine`; one
    virtual block is `INTERVAL` seconds;
  - `run-anvil.sh` deploys with `forge create`, using anvil's first account as
    `DEPLOYER_PRIVATE_KEY`, and prints the address.
- **Base Sepolia via Infura:**
  - RPC `https://base-sepolia.infura.io/v3/${INFURA_API_KEY}`;
  - deploy with `DEPLOYER_PRIVATE_KEY`, funded from a faucet;
  - nodes have their own key files, funded from the deployer;
  - `INTERVAL` 60, `evm-confirm=safe`, `evm-poll` 30.
- **OP Sepolia** is the fallback; the same settings work.

## 7. Tests

**Contract (Foundry, no forge-std or git dependencies):**
- **Real CLN transactions.** A wider spike S1 run captures every kind:
  revocation, penalty, HTLC-success, to_remote, anchor spends, mutual close.
  Each MUST validate at its height and fail one block before its earliest
  valid height.
- **Tampered cases.** S3's 136, carried over and extended:
  - for each CSV and CLTV path, the input's sequence or the tx's locktime one
    below what the script needs, with the age otherwise sufficient;
  - taproot SINGLE with no matching output;
  - time-based locktimes around `MTP(h)`.
- **Official BIP340 and BIP341 vectors.**
- **Differential fuzzing** against a Python reference: `btc.py` plus coincurve
  for signatures, BIP143 and BIP341.
- **Deposit and exit accounting:** the 4.1 invariant after every operation.
- **Reentrancy** in `withdraw`.
- **Gas:** a report for each tx kind, plus the worst-case commitment (30 + 30
  HTLCs) under 12M.

**Plugin unit tests:**
- byte-exact block, coinbase and deposit-tx building, checked against the
  contract's txids, including coinbase heights 0, 1–16, 127/128 and 32767/32768;
- index ingestion of our own receipts;
- reorg handling and the tip staying monotonic;
- the 10k-result split;
- the send path against anvil, including a send that stays pending past the
  time limit and is replaced.

**Integration (`tests/test_evmchan.py`, anvil):**
- **Funding and paying:** deposits to P2TR and P2WPKH; open, pay, gossip.
- **Mutual close.**
- **Unilateral close by either side**, with sweeps.
- **HTLCs on chain:** timeout and success, from both the local and the remote
  commitment.
- **Penalty.**
- **Rejections:** a non-anchor channel, a lease and a non-segwit dual-fund
  input are refused, and so is a peer without anchors.
- **Exits:** `withdraw` to an exit address, then the contract's `withdraw`.
- **Restart and reorg:** restarts mid-close; an anvil reorg above `safe`; anvil
  stopped for longer than `L` (outage) and restarted.
- **Options:** a node started with regtest's default options refuses to open
  channels.

**Base Sepolia smoke test** (manual, scripted): two nodes, open, pay, close,
with credits per day recorded.

Agents can share the work:
- one writes the contract tests and Python reference;
- one writes the plugin and its unit tests;
- one writes the integration tests and README.

Each change is reviewed by a fresh agent, as spike S3 was.

## 8. Milestones

Each needs its tests green and your review before the next.

| # | Deliverable | Done when |
|---|---|---|
| M1 | Contract: libraries from S2/S3 without hints, `deposit`/`submit`/`withdraw`, size < 24 KB | Contract tests (7) pass; gas within 4.5 |
| M2 | `btc.py`, `evm.py`, block synthesis + index | Plugin unit tests pass, blocks match the contract's txids |
| M3 | `evmbackend.py`: five methods, hooks, RPCs, alarms, option checks | Spike S1's scenario passes on anvil instead of the toy chain |
| M4 | `tests/test_evmchan.py` | All scenarios in 7 pass on anvil, in Docker as well as macOS |
| M5 | Base Sepolia | Smoke test passes; Infura credits and the lag `L` measured |
| M6 | Docs | README, this spec updated to what was built |

## 9. Later (out of scope for version 1)

- **ERC-20:** one contract instance per token. `deposit(amount, script)` uses
  `transferFrom`, exits use `transfer`, and the token's decimals fix the sat.
- **A CLN network of its own:** chainparams from config, about 50 lines in CLN,
  giving its own chain hash, address prefixes and limits.
- **An L1 force-inclusion fallback** for penalties and HTLC claims when the L2
  sequencer censors or stalls: an OptimismPortal deposit tx.
- **Splicing**, with a check of the peer's splice inputs (open question 4).
- **Gas:** hashing slices of the calldata instead of re-serializing, which
  spike S3 shows is a third of the cost; optional hints, to save the modexp and
  second `ecrecover` calls.
- **Before real value:** an audit, and mainnet.

## 10. Open questions

1. **Payment size.** Regtest's `max_payment` (2^32 msat ≈ 0.043 ETH) may cap
   single payments. Check whether it applies with large channels (M3).
2. **Network identity.** Two contracts on the same chain look like one CLN
   network: same chain hash, `bcrt` addresses. Gossip for channels on another
   contract fails verification and is dropped, but nodes must be pointed at the
   right contract.
3. **Wall-clock heights.** An outage longer than the margins in 5.6 can still
   lose funds: if the EVM chain or a node's access to it is down for more than
   about a day on Base Sepolia, penalties and HTLC claims may be late. The
   fallback in 9 addresses the sequencer, not the node's own connectivity.
4. **Splicing.** CLN has no hook to inspect a peer's splice inputs. Options:
   keep splicing off; or check in a `commitment_revocation` or custom-message
   path; or propose a hook upstream.

## 11. Changes from draft 1 (review)

- **Safety (5.6):** normative CLN options sized for the lag and outages.
  Regtest's 6-block delays could lose penalties and HTLCs. Alarms added, and
  the plugin refuses opens when the options are wrong.
- **Read methods never wait on sends (5.3).** Sends are time-limited, with
  pending tracking and fee replacement. CLN's rebroadcast timer waits for every
  send, so one hung send blocked them all.
- **Channel type enforcement (5.4)** covers outbound opens and peers without
  anchors, plus dual-fund inputs, leases, the HTLC cap and splicing.
- **Gas cap (4.5)** with a worst-case commitment test.
- **Consensus wording (4.3)** made exact:
  - MTP for time locks;
  - `CheckSequence` and `CheckLockTime`;
  - `CSV 1` on the HTLC paths;
  - taproot SINGLE;
  - the BIP340 restriction;
  - the sigops rule.
- **Index (5.2):** our own receipts are ingested immediately; reorgs are served
  rather than assumed away; `finalized` dropped; adaptive `eth_getLogs`
  ranges; `DEPLOY_BLOCK` read from the contract.
- **Keys (5.1):** per node, no shared environment key.
- **Smaller fixes:**
  - the coinbase height push is defined exactly;
  - deposits to OP_RETURN are rejected;
  - the version is read as a uint32;
  - front-running of submits is accepted;
  - modexp pricing is in the gas targets;
  - the "send later" exception is dropped.
