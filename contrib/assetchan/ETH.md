# Lightning channels in ETH (or one ERC-20) with CLN

How CLN could run Lightning channels whose coin is ETH, or one ERC-20 token,
without changing CLN's channel code. This is a plan, not code: nothing here
is implemented yet. It is the EVM case of `DESIGN.md` §2.1 (a "Level 1",
single-coin chain).

## The idea in one paragraph

CLN builds, signs and watches Bitcoin transactions, and talks to its chain
through a backend plugin (bcli by default). So we give it a Bitcoin-shaped
chain that lives inside an EVM contract: the contract keeps a UTXO set of one
coin and accepts Bitcoin-format transactions that spend it, and a backend
plugin shows the contract to CLN as a chain of Bitcoin blocks. CLN then opens
ordinary channels. None of the Liquid asset-channel code is used: there is one
coin, and fees are not a problem (see Fees).

```
  lightningd ── backend plugin ── EVM node (an L2) ── UTXO contract
  (unchanged)   (5 RPC methods)     (JSON-RPC)        (holds the ETH/ERC-20)
```

## Pieces

### 1. The UTXO contract (Solidity)

Holds the coin and a UTXO set: `outpoint → (amount, scriptPubKey)`.

- **`deposit(scriptPubKey)`**, payable (or ERC-20 `transferFrom`): creates a
  UTXO. Users fund a CLN wallet by depositing to a `newaddr` address of it.
- **`submit(rawTx)`**: parses a segwit-v0 Bitcoin transaction, checks it, and
  applies it: inputs removed, outputs added, an event emitted with the raw tx.
  Checks:
  - every input exists and is unspent; sum of inputs ≥ sum of outputs;
  - witnesses satisfy their scripts. The contract does not need a general
    Script interpreter: it only has to recognize the scripts CLN produces and
    check each by template: P2WPKH, and P2WSH for the BOLT 3 funding 2-of-2,
    `to_local`, `to_remote` (`1 OP_CSV`), anchors, offered and received HTLCs;
  - signatures: secp256k1 ECDSA over the BIP143 sighash (SHA256 precompile,
    double-hashed), checked with `ecrecover` against the public key in the
    witness. Sighash types `ALL` and `SINGLE|ANYONECANPAY` (CLN's zero-fee
    HTLC transactions use the latter);
  - hashes: SHA256 (precompile), RIPEMD160 (precompile) for HASH160;
  - timelocks: `nLockTime` against the contract's height, `nSequence`
    (relative, in blocks) against the height the spent UTXO was created at.
- **`exit(outpoint, recipient, signature)`**: pays out a P2WPKH UTXO to an ETH
  address. The signature proves ownership with the same secp256k1 key: an ETH
  account's key is also a valid P2WPKH key (`hash160` of its compressed
  public key), so the plugin can hold one key for both sides.
- **Height**: `block.number / N`, so one Bitcoin-style block spans N EVM
  blocks. CLN's timeouts are in blocks (CLTV deltas of 34 to 144, CSV delays
  of 144 and more), so N is chosen to give them sensible wall-clock times on
  a 2-second L2 block.

### 2. The backend plugin

Replaces bcli (`--disable-plugin=bcli --plugin=ethbackend`). It implements the
five methods of `doc/developers-guide/plugin-development/bitcoin-backend.md`:

| Method | What it does |
|---|---|
| `getchaininfo` | `chain: "regtest"` (see Network), heights from the contract |
| `getrawblockbyheight` | builds the Bitcoin block for a height from the contract's events: a coinbase placeholder plus the applied txs, in order. Each header's `prev_hash` must be the previous block's hash: lightningd checks that linkage to detect reorgs. Only finalized EVM blocks are served, so there are no reorgs to handle |
| `getutxout` | amount and script of a UTXO, from contract state. Gossip uses it to verify channel announcements |
| `sendrawtransaction` | calls `submit(rawTx)`, paying the gas from the plugin's ETH key |
| `estimatefees` | the floor (1,000 sat/kvB) for every target |

Plus two RPCs of its own: `eth-deposit amount` (deposit to a CLN wallet
address) and `eth-exit outpoint recipient`.

### 3. CLN

Nothing changes for a proof of concept:

- **Network**: CLN runs with `--network=regtest` and the plugin reports
  `regtest`. lightningd checks only that name against the backend
  (`lightningd/chaintopology.c`); the genesis hash is only the network's
  label in peer and gossip messages, never compared with the blocks it gets.
- **Units**: CLN's "satoshi" is whatever unit the contract defines, for
  example 1 gwei of ETH, or 10^-8 of an 18-decimal token. Millisatoshi
  precision follows.
- **Channels** are ordinary upstream channels: anchors, zero-fee HTLC
  transactions, penalties, gossip, splicing even, all unchanged.

## Fees

The transaction fee inside the emulated chain (inputs − outputs) is tiny: the
plugin reports the floor feerate, and the contract keeps the fee. The real
cost is gas, which the submitter pays in ETH outside the Bitcoin
transaction, and the gas price decides how fast it confirms. So there is no
fee coin problem as on Liquid, and no fee reserve: CLN's normal fee logic
just runs at the floor.

Gas is the reason for an L2: each submitted transaction is parsed, hashed and
signature-checked in the contract, a few signatures each, which is far more
expensive than a Bitcoin node verifying it.

## Limits of running as "regtest"

| Limit | Effect | Fix |
|---|---|---|
| Address and invoice prefixes are regtest's (`bcrt`, `lnbcrt`) | they look like Bitcoin regtest ones | chainparams from config |
| The network is indistinguishable from real regtest or from another contract | one network per node, peers must be on the same contract | chainparams from config (own genesis hash = chain hash) |
| Channel size capped at 16,777,215 units without large channels | small channels in gwei | chainparams from config, or `option_support_large_channel` |
| Dust limit 546 units | may not suit the token | chainparams from config |

**Chainparams from config** is the one core change a real deployment needs,
about 50 lines in `bitcoin/chainparams.c` and option parsing: a network name,
genesis hash, address and invoice prefixes, BIP32 versions, dust limit and
funding limits, read from a file instead of the compiled table. It helps any
emulated chain, not only ETH.

## Order of work

1. **Contract**, with Foundry tests that replay transactions produced by CLN
   on Bitcoin regtest (funding, commitment, HTLC, sweep, penalty) and check
   that it accepts exactly those and rejects tampered ones.
2. **Backend plugin** against a local L2 devnet (anvil), and CLN's own test
   suite run with it in place of bcli: `tests/test_connection.py`,
   `test_pay.py`, `test_closing.py`.
3. **Security review of the contract.** It holds every channel's funds and
   is the consensus layer, so it needs one before any real value.
4. **Chainparams from config** in CLN, to give the network its own identity.

ERC-20 channels are the same with one contract instance per token. Several
tokens in one contract, with fees in ETH, would bring back Liquid's fee-coin
problem; one token per contract avoids it.
