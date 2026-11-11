# UtxoChain contract

The contract of `contrib/evmchan/SPEC.md` section 4 (Foundry project; no
forge-std, no git dependencies, nothing outside this directory to build it). The logic is in `src/UtxoChainBase.sol`; two thin
deployables add the asset:

- `UtxoChain` holds ETH (1 sat = 10^10 wei), with M1's ABI;
- `UtxoChainToken` holds one ERC-20, one instance per token (SPEC 4.6).
  1 sat = `UNITS_PER_SAT` token units, which is 10^(decimals - 8), or 1 below
  8 decimals. `deposit(amount, script)` pulls with `transferFrom` (approve
  first), and `withdraw()` pays with `transfer`. No-bool tokens (USDT) work.
  A balance check on every token call refuses fee-on-transfer tokens.
  Rebasing tokens MUST NOT be used. Its tests are `test/Token.t.sol`, with
  the mocks in `test/Tokens.sol`.

## Running the tests

The test data is generated, deterministically, and not committed. The
generator is on the Python side, in `contrib/evmchan` (from the tree's root):

```
PY=/path/to/python-with-coincurve
(cd contrib/evmchan/reference && $PY fuzz.py)   # reference/cases/: differential cases
$PY -I contrib/evmchan/gen_vectors.py           # contracts/evm/test/generated/
(cd contracts/evm && forge test)
```

`gen_vectors.py` reads the real CLN transactions in
`contrib/evmchan/tests/vectors/*.jsonl`, the official BIP340/BIP341 vectors in
`test/data/`, and the reference cases. Rerun it after any of those change.
`--contracts DIR` points it at a checkout of this project elsewhere.

## Deploying

```
DEPLOYER_PRIVATE_KEY=... ./deploy.sh --rpc-url <url> --interval <seconds>
DEPLOYER_PRIVATE_KEY=... ./deploy.sh --rpc-url <url> --interval <seconds> --token <erc20> [--units-per-sat N]
```

Without `--units-per-sat`, the script reads the token's `decimals()`. On
anvil, `contrib/evmchan/run-anvil.sh --token mock:6` deploys a mock 6-decimal token
first. It prints the contract's address, then the token's.
