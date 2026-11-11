<!-- SPDX-License-Identifier: MIT -->
# Python reference for `UtxoChain` validation

An independent implementation of `SPEC.md` (draft 3) for differential testing
of the contract:

- 4.3, when a submitted transaction is valid;
- 3.3, the synthetic deposit transaction, with 4.2's deposit checks;
- 4.4, how a valid transaction changes the UTXO set (`pyref.Chain`).

It was written from the SPEC, BIP143/340/341/65/66/68/112/113, Bitcoin Core's
script semantics and `bitcoin/script.c`, without reading the contract or
spikes S2/S3. Tx parsing started from spike S1's `btc.py`.

| File | What |
|---|---|
| `pyref.py` | `validate(raw_tx, prevouts, height, genesis_time, interval) -> (ok, error, txid, fee)`, plus `deposit_tx`, `check_deposit`, `utxo_key`, `utxo_commitment`, `Chain` |
| `check_vectors.py` | Checks pyref against the CLN vectors, BIP340, BIP341 and known hash answers |
| `fuzz.py` | Writes `cases/*.jsonl`. It is seeded, so every run writes the same bytes |
| `vectors/` | `bip341-wallet-test-vectors.json` and `bip340-test-vectors.csv`, fetched from github.com/bitcoin/bips (master) |
| `cases/` | `cln.jsonl`, `synth.jsonl`, `deposits.jsonl` |

## Running

The scripts need Python 3 with `coincurve` (22 was used). They have no other
dependencies.

```
PY=../../../../lightning/.venv/bin/python     # or any Python with coincurve
$PY check_vectors.py           # CLN + BIP340 + BIP341 + hashes; exit 1 on failure
$PY fuzz.py                    # regenerate cases/; exit 1 if a hand-written expectation fails
$PY check_vectors.py --cases   # also re-validate every case line
```

Results at the last run:
- All 42 non-deposit CLN txs in `tests/vectors/*.jsonl` are valid
  at their heights, with matching txids.
- Their earliest valid height is the same by search and from the tx's fields.
- The 27 that are timelocked fail one block earlier, each with a timelock error.
- Replaying each file on `Chain` reproduces every recorded prevout.
- The 19 BIP340 vectors pass with coincurve, and with a pure-Python verifier
  written from the BIP.
- In BIP341 `keyPathSpending`, all 7 `sigMsg`/`sigHash` values match. Its 7
  taproot inputs verify, and so does its P2WPKH input (an official BIP143
  check). A flipped bit is rejected.

`fuzz.py` writes 7,648 tx cases (2,162 valid, 5,486 invalid) and 112 deposit
cases (52 valid). The CLN part depends on the vector files: a new S1 run changes it.

## Case format

One JSON object per line:

```
{tx, prevouts: [{amount, script, height}], height, genesis_time, interval,
 valid, error, origin, txid, fee}
```

- **`prevouts`** are the UTXO entries that the inputs spend, in input order. A
  harness stores `utxo[key(input i's outpoint)] = commitment(prevouts[i])` and
  then submits at virtual height `height`, meaning a `block.timestamp` in
  `[T(h), T(h+1))`.
  - `genesis_time` is 1,700,000,000 and `interval` is 600 (the toy chain's).
  - A few cases deliberately don't line up, and their names say so:
    `prevouts-one-short`/`-extra` (`PrevoutCountMismatch`) and `duplicate-*`
    (the same outpoint twice).
- **`txid`** is in display hex. It is null if the tx doesn't parse.
- **`fee`** is null if validation stopped before rule 3.
- **`origin`** says what was done. Only 6 origins still contain `ambiguous`,
  for the null-prevout reading below.
- **No case has a prevout height of 0 or above `height`.** No such UTXO can
  exist (draft 3: the height comes from the commitment).

`deposits.jsonl` lines have these fields: `chain_id`, `contract`, `nonce`,
`value_wei`, `script`, `height`, `valid`, `error`, `raw_tx`, `txid`, `origin`.
The txid assumes `depositNonce` is a `uint256`, as 4.5 now fixes it.

### What the cases cover

**`cln.jsonl`:** every real tx, unchanged, at E−1, E, E+1 and E+1000, where E
is its earliest valid height, and at its recorded height +0, +1, +10 and
+100,000.
- **Prevout heights.** The tx is also swept with prevout heights lowered and
  raised. A prevout's height is not signed, so a lower one stays valid.
- **Fee bumps.** An input or an output is added. That stays valid exactly when
  every signature in the tx is ANYONECANPAY or SINGLE/NONE.
- **Full mutation set.** The first tx of each kind also gets:
  - flipped bytes in witness items, all 11 hash-type bytes, high S, padded DER
    and empty sigs;
  - dropped or extra witness items, swapped 2-of-2 sigs, `NULLDUMMY` and
    `MINIMALIF` cases;
  - wrong preimages, and non-empty timeout dummies;
  - changed sequences, locktimes and versions;
  - output amounts, `MAX_MONEY`, sigops;
  - added, dropped, duplicated and null inputs, and non-empty scriptSigs;
  - prevout amounts, 15 kinds of foreign prevout script, prevout counts;
  - encodings: trailing bytes, truncation, 18 non-canonical varints, flags
    00 00 and 00 02, stripped and superfluous witnesses, no outputs, height 0;
  - 25 random byte edits.

**`synth.jsonl`:** txs built and signed here, for each P2WPKH, P2TR and P2WSH
template path. Each variant is re-signed, so it breaks one rule only. Each
variant carries an expected result, worked out by hand from the SPEC and the
BIPs, and `fuzz.py` fails if pyref disagrees. The variants cover:
- **CSV:** n−1, version 0/1/u32max/0x80000002, the disable and type bits,
  `(n−1)|0x10000`, `n|0x10000`.
- **CSV numbers:** every `add_number` width 0…0xffff, plus non-minimal and
  out-of-range encodings.
- **CLTV:** locktime −1, type mismatches, final sequence, every CLTV width, and
  time-based expiries.
- **Signatures:** hash types, DER, high S, wrong keys.
- **Script paths:** preimages, `NULLDUMMY`, `MINIMALIF`, `CLEANSTACK`, timeout
  dummies other than `<>`.
- **Template rules:** unsorted 2-of-2, lease to_remote, template keys with bad
  prefixes or off the curve (anchor `<>` path included), CSV operands ≥ 2^16.
- **SINGLE with no output:** valid for ECDSA, invalid for taproot.
- **Taproot shapes:** annex, script path, 63/65/66-byte sigs, output key off
  the curve or ≥ p.
- **Limits:** sigops exactly at 16,000 and one above; tx size exactly 100,000
  and 100,001; `MAX_MONEY` boundaries.
- **Lock time:** height and MTP lock-time boundaries, including the first ten
  blocks; BIP68 up to 0xffff.
- **Mixed txs:** a 5-input tx and 300 random valid compositions. Each is swept
  over heights and gets 3 random edits.

## Error names

The checks run in the SPEC's rule order. Inside a script they run in Bitcoin's
execution order, so in the received-HTLC timeout path CLTV fails before CSV.

| Name | Rule | Meaning |
|---|---|---|
| `TooEarly` | 3.1 | height 0 (checked first) |
| `TxTooLarge` | 4.3.1 | more than 100,000 bytes, witness included |
| `Truncated` | 4.3.1 | the parse ran out of bytes |
| `NonCanonicalVarint` | 4.3.1 | a varint longer than needed |
| `TrailingBytes` | 4.3.1 | bytes after the locktime |
| `BadWitnessFlag` | 4.3.1 | `00` after the version not followed by `01` |
| `SuperfluousWitness` | 4.3.1 | marker and flag present, every witness empty |
| `NoInputs`, `NoOutputs` | 4.3.1 | an empty input or output list |
| `PrevoutCountMismatch` | 4.3.2 | `prevouts.length` ≠ input count |
| `NullPrevout` | 4.3.2 | outpoint (0³², 0xffffffff) |
| `DuplicatePrevout` | 4.3.2 | the same outpoint twice |
| `BadPrevoutHeight` | 4.3.2 | prevout height 0 or above `h` (no such UTXO can exist; never in cases) |
| `ScriptSigNotEmpty` | 4.3.2 | a non-empty scriptSig |
| `OutputTooLarge` | 4.3.3 | one output above `MAX_MONEY` |
| `InputsTooLarge` | 4.3.3 | the input sum is above `MAX_MONEY` |
| `OutputsTooLarge` | 4.3.3 | the output sum is above `MAX_MONEY` |
| `OutputsExceedInputs` | 4.3.3 | outputs > inputs |
| `TooManySigops` | 4.3.4 | `4·legacy + 4·inputs > 16000` |
| `NonFinal` | 4.3.5 | the lock time is not reached |
| `RelativeTimeLockUnsupported` | 4.3.6 | version ≥ 2, bit 31 clear, bit 22 set |
| `RelativeLockNotMet` | 4.3.6 | `h − prevout.height < sequence & 0xffff` |
| `UnknownScript` | 4.3.7 | the prevout script is not P2WPKH/P2WSH/P2TR, or the witness script is not a template |
| `BadWitness` | 4.3.7 | P2WPKH witness not 2 items; empty P2TR or P2WSH witness |
| `KeyHashMismatch` | 4.3.7 | P2WPKH `hash160(key)` ≠ program |
| `WitnessScriptMismatch` | 4.3.7 | P2WSH `sha256(script)` ≠ program |
| `TaprootAnnex` | 4.3.7 | an annex is present |
| `TaprootScriptPath` | 4.3.7 | 2 or more items without an annex |
| `StackUnderflow` | 4.3.7 | too few stack items, or an unbalanced IF |
| `CleanStack` | 4.3.7 | not exactly one item left |
| `MinimalIf` | 4.3.8 | an IF/NOTIF argument other than `<>` or `<01>` |
| `NullDummy` | 4.3.8 | the CHECKMULTISIG dummy is not empty |
| `BadPreimage` | 4.3.7 | `HASH160 <h> EQUALVERIFY` failed |
| `CsvNotMet` | 4.3.7 | `CheckSequence` failed |
| `CltvNotMet` | 4.3.7 | `CheckLockTime` failed |
| `BadSigEncoding` | 4.3.8/9 | not strict DER; a taproot sig not 64 or 65 bytes |
| `HighS` | 4.3.8 | S > n/2 |
| `BadHashType` | 4.3.8/9 | an undefined hash type, or a 65-byte taproot sig ending in 00 |
| `BadPubKey` | 4.3.7/8 | a template key that is not a valid compressed point (draft 3); a key used by CHECKSIG that is not 33 bytes with prefix 02/03 |
| `BadTimeoutDummy` | 4.3.7 | the HTLC timeout path's dummy item is not exactly `<>` (draft 3) |
| `SighashSingleNoOutput` | 4.3.9 | taproot SINGLE with no output at that index |
| `BadSignature` | 4.3.8/9 | a signature that doesn't verify (NULLFAIL), or a required one that is empty |

Internal names that templates can't reach: `NegativeLocktime`,
`ScriptNumOverflow`, `NonMinimalNumber`, `PubKeyCount`, `SigCount`. Only
`Chain` raises `UnknownPrevout`, for a missing UTXO or a commitment mismatch.
For deposits: `TooEarly`, `ValueNotWholeSats`, `ZeroDeposit`,
`AmountTooLarge`, `ScriptTooLong`, `ExitScript`, `OpReturnScript`.

## Readings: settled by draft 3, and still open

pyref follows SPEC draft 3. That includes the conservative "Readings fixed in
draft 3" at the end of 4.3.7, and the interface details in 4.5. Where Bitcoin
would accept more, the Bitcoin-exact variant is left as a comment in
`pyref.py`.

### Settled by the SPEC (draft 3)

Draft 2 left these open; draft 3 decides them. No case is tagged `ambiguous`
for them any more.

1. **CSV operand.**
   - `n` is compared whole with `sequence & 0xffff`. So `n ≥ 2^16`, or `n` with
     bit 22 or 31 set, is never satisfied (`CsvNotMet`).
   - This differs from Bitcoin, which masks `n` and treats bit 31 as a NOP.
   - Such scripts still match the template, so their revocation path works.
     Cases: `synth:to_local:csv-0x10000-*`, `csv-0x80000000-*` and others.
2. **HTLC timeout dummy.** The dropped item in the offered local-timeout and
   received remote-timeout paths must be exactly `<>`. Anything else is
   `BadTimeoutDummy`, whereas Bitcoin accepts any item that isn't 32 bytes.
3. **2-of-2 key order.** The keys must be sorted, as `bitcoin_redeem_2of2`
   sorts them. An unsorted script is not the template (`UnknownScript`).
4. **to_remote.** Its CSV must be exactly 1. Lease delays are `UnknownScript`.
5. **Template keys.**
   - Every 33-byte key in a template must be a valid compressed point. That
     means prefix 02/03 and on the curve, checked after the template matches
     (`BadPubKey`).
   - This applies even to the anchor's key on its `<>` path, where Bitcoin
     checks only the prefix and never the curve.
6. **The 520-byte stack-item limit** can't be reached with fixed template
   sizes. pyref no longer checks it, so `PushSize` is gone. A 521-byte sig is
   `BadSigEncoding`.
7. **Prevout height ≤ h.** It comes from the stored commitment.
   - pyref still rejects a height of 0 or above `h` (`BadPrevoutHeight`), and
     the cases never contain one.
   - Same-block spends (prevout height = h) are valid.
8. **Sigops** are counted over every output script, OP_RETURN and exits
   included, as pyref already did. `GetSigOpCount` stops at a truncated push.
9. **Genesis time** must be a real timestamp, so that commitments' `0x20xxxxxx`
   time locks are final. The cases use 1,700,000,000.
10. **Deposits.** Deposits above `MAX_MONEY` revert (`AmountTooLarge`), and
    `depositNonce` is a `uint256`. The deposit cases no longer carry a uint64
    txid.

These were never ambiguous, and pyref follows the SPEC text:
- **MTP for heights 2 to 10.** `MTP(h) = T(max(h−6, 0))` differs from Bitcoin's
  median for 2 ≤ h ≤ 10 (`synth:mtp-early:*`).
- **The P2TR rules.**
- **SINGLE with no matching output.** It uses a zero `hashOutputs` for ECDSA
  and is invalid for taproot.

### Still open (pyref's reading)

1. **What "null prevout" means.**
   - pyref uses Bitcoin's `COutPoint::IsNull`: txid 0 *and* index 0xffffffff.
   - Txid 0 with index 0, and txid ff…ff with index 0xffffffff, are not null
     and are valid when signed. They are the only cases still tagged
     `ambiguous` (6 cases: `ambiguous:p2wpkh:outpoint-*`).
2. **What "size" measures.** pyref counts the raw bytes of `rawTx`, witness
   included (not weight). The boundary cases are exactly 100,000 and 100,001
   bytes, so a weight-based contract would disagree on them.
3. **Error names and their order.** pyref checks in the SPEC's rule order. Inside
   a script it follows Bitcoin's execution order, so CLTV fails before CSV in
   the received-HTLC timeout path. Some names are pyref's choice:
   - `00` after the version followed by something other than `01` is
     `BadWitnessFlag` (Bitcoin: zero inputs);
   - an empty sig where one is required, or a non-empty sig that fails
     (NULLFAIL), is `BadSignature`.

   A harness should compare validity first.
4. **A key supplied in the witness.**
   - In the revocation paths, the revocation key comes from the witness, so no
     template rule covers it. It is checked as Bitcoin checks it: encoding at
     CHECKSIG, and the curve only when a signature is verified.
   - The verdict can't differ, since its hash160 must match the script.
5. **Deposit scripts.**
   - An empty scriptPubKey is accepted, and so is a `60` script that isn't
     exactly `6014<20>`. 4.2 lists only three script checks.
   - The output is stored but can never be spent.

## Findings from the real CLN vectors

- **Commitment transactions are time-locked.** They have `nLockTime`
  0x20xxxxxx (≥ 500,000,000) and sequence 0x80xxxxxx, which is not final.
  - So they are final only while `MTP(h) > nLockTime`, which needs
    `GENESIS_TIME` above about 553,648,127 (July 1987).
  - Under a harness or anvil with a small genesis timestamp, every commitment
    is `NonFinal`.
- **Many claims are signed SINGLE|ANYONECANPAY (0x83).**
  - That holds for both signatures of HTLC-success and HTLC-timeout txs, and
    for the single signature of `remote-success` and `remote-timeout` claims.
  - Anyone can therefore add inputs, or outputs after index 0, and the tx stays
    valid. The `bump-*` cases test this.
- **Anchor CPFP spends the anchor in the same virtual block as its
  commitment**, so prevout height = h. The contract must allow spending an
  output in the block that creates it.
- **HTLC-timeout txs carry `nLockTime = cltv_expiry`, but the offered-HTLC
  script has no CLTV.** Only the signatures over the locktime enforce the
  timeout.
- **Wallet txs use anti-fee-sniping `nLockTime = h−1` with sequence
  0xfffffffd**, so they are `NonFinal` one block before the height they were
  mined at. Penalty txs use sequence 0 with locktime 0, which leaves BIP68
  active but trivially satisfied.
- **The vector files changed while this was written.**
  `cln-regtest-txs-full.jsonl` appeared and was regenerated. The original file
  has no `kind` labels.
