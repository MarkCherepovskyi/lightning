# UtxoChain contract

The contract of SPEC.md section 4 (Foundry project; no forge-std, no git
dependencies).

## Running the tests

The test data is generated, deterministically, and not committed:

```
PY=/path/to/python-with-coincurve
(cd ../reference && $PY fuzz.py)        # reference/cases/: differential cases
$PY -I script/gen_vectors.py            # test/generated/: vectors and cases
forge test
```

`gen_vectors.py` reads the real CLN transactions in
`../spikes/s1-toychain/vectors/*.jsonl`, the official BIP340/BIP341 vectors in
`test/data/`, and the reference cases. Rerun it after any of those change.

## Deploying

```
DEPLOYER_PRIVATE_KEY=... ./deploy.sh --rpc-url <url> --interval <seconds>
```
