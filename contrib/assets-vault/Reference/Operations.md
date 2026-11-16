---
tags: [reference, operations]
aliases: [Running nodes, Config]
---
# Operations

How to configure and run each kind of node (Liquid asset node, EVM node, swap provider and payer), what each one's wallets must hold, and how to run the tests.

## Liquid asset node

```ini
# lightningd config (Liquid)
network=liquid             # or liquid-testnet / liquid-regtest
important-plugin=/path/to/contrib/assetchan/assetchan.py   # MUST be important (see below)
assetchan-asset=<64-hex asset id>                          # repeat for each asset you take
assetchan-elements-rpc=http://user:pass@127.0.0.1:7041     # elementsd that funds opens
assetchan-elements-wallet=<wallet name>                    # optional
# assetchan-default-fee-reserve=2000   (sat, put up when you open)
# assetchan-min-fee-reserve=1000 / assetchan-max-fee-reserve=10000   (accepted from peers)
# assetchan-close-to=elements          (our side closes to an elementsd address)
```

> [!warning] Two wallets, two jobs
> - **elementsd's wallet** funds opens. It needs *unblinded* UTXOs of the asset and of L-BTC; the plugin pays itself one if needed. It also receives your assets at close, because `close_to` is an elementsd address.
> - **CLN's own wallet** (`newaddr`, then send L-BTC to it) pays the L-BTC fees of asset sweeps and asset HTLC transactions after a unilateral close. With no L-BTC there, core logs "only … L-BTC in our wallet … will retry every block" and keeps the channel until it can sweep. The asset is not lost, but deadline-bound claims (penalties, HTLCs) can be missed. Monitor it.

- **`important-plugin` is required.** The plugin disables itself otherwise. lightningd then offers no asset channels, and refuses to start existing ones (`Asset channel, but no asset channel plugin`).
- Asset channels are always private. Payers reach you through the route hints in `assetchan-invoice`.
- Fee reserve vs HTLCs: the reserve caps how many HTLCs a channel carries. At 2,000 sat each side accepts 82; at 1,000 sat, 18. A larger reserve allows more HTLCs.
- Static channel backups (`staticbackup`, `emergency.recover`) include each asset channel's asset, fee reserve and close script, so `recoverchannel` restores them. Recovery needs this fork's binary.

Main RPCs: `assetchan-open id amount asset [fee_reserve]`, `assetchan-invoice amount asset label description`, `assetchan-pay bolt11 asset`, `assetchan-close id`, `assetchan-list`, `assetchan-funds`, `assetchan-layer asset`. See [[assetchan Plugin]].

Scripts: `contrib/assetchan/nodes.sh start A` runs nodes A–F against a Liquid testnet or regtest elementsd (`nodes.sh` header lists the environment variables). `contrib/nodeui/testnet.sh start` brings up Liquid, EVM and Bitcoin test nodes plus the web page.

## EVM node (ETH or one ERC-20)

```ini
network=regtest                 # every EVM network uses it (see the warning below)
disable-plugin=bcli
plugin=/path/to/contrib/evmchan/evmbackend.py
evm-rpc=https://base-sepolia.infura.io/v3/${INFURA_API_KEY}
evm-contract=0x…                # the UtxoChain (or UtxoChainToken) instance
# evm-key-file, evm-confirm=safe, evm-poll=60, evm-min-balance=0.01, evm-snapshot=…
# evm-require-peer-identity=true
developer
dev-force-features=-63          # splicing off
watchtime-blocks / cltv-delta / cltv-final / funding-confirms / max-concurrent-htlcs: see below
```

The plugin checks CLN's timing options against wall-clock minimums (`contrib/evmchan/SPEC.md` §5.6) and refuses opens until they are met:

| Option | Public chains | anvil |
|---|---|---|
| `watchtime-blocks` | ≥ 24 h | ≥ 80 s |
| `cltv-delta`, `cltv-final` | ≥ 2.4 h | ≥ 80 s |
| `funding-confirms` | ≥ 3 | ≥ 1 |
| `max-concurrent-htlcs` | ≤ 30 | ≤ 30 |

Block values are the time divided by `INTERVAL`, rounded up.

> [!warning] `--network=regtest` is shared
> All EVM networks look like Bitcoin regtest to CLN: the same chain hash and the same `bcrt` invoice prefix. Keep `evm-require-peer-identity` on, so nodes on different contracts refuse each other. Production needs a core custom-network option; see [[Upstream Status]].

Money: `evm-deposit` puts ETH or tokens into the contract as a UTXO that CLN's wallet sees. `evm-withdraw` takes it out after an exit. `evm-status` shows balances, sends and alarms. One node serves one contract, so one coin. See [[evmbackend Plugin]].

## Swap provider and payer

```ini
# provider (the node that receives leg 1)
important-plugin=/path/to/assetswap-plugin      # required to take new swaps
plugin=/path/to/pricetiers-plugin
pricetiers-pair=<from_asset>:<to_asset>:<rate_ppm>:<fee_base_msat>
assetswap-remote=eth:/path/to/evm-node/regtest/lightning-rpc   # leg 2 on another node
# assetswap-block-seconds=<asset>:<secs>, assetswap-leg2-maxfee-ppm=5000, assetswap-leg2-maxfee-base-msat=5000

# payer
plugin=/path/to/assetswap-plugin
plugin=/path/to/pricetiers-plugin
pricetiers-pair=…            # for the local estimate
assetswap-provider=<provider node id>
```

The payee creates `assetinvoice … asset=eth`. The payer runs `payassetinvoice <wrapped> [tier] [from_asset] [from_amount_msat]` (or by name, e.g. `from_asset=lbtc`). See [[Cross-Asset Swaps]].

## Tests

| What | Command | Needs |
|---|---|---|
| C unit tests | `gmake -j10 check-units` | the tree configured (mako on PATH) |
| Liquid asset suite | `contrib/assetchan/run-tests.sh -n 4 tests/test_assetchan.py` | Docker |
| Upstream tests on Bitcoin regtest | `ASSETCHAN_TEST_NETWORK=regtest contrib/assetchan/run-tests.sh tests/test_closing.py` | Docker |
| EVM unit + anvil suite | `contrib/evmchan/run-tests.sh` | Docker |
| Liquid↔EVM swaps | `contrib/liquid-poc/asset-swap/run-tests.sh` | Docker |
| Rust plugins | `cargo test -p cln-assetswap -p cln-pricetiers` | Rust |

> [!note] One run at a time
> Each runner keeps a Docker build volume per checkout. Don't run two runners on the same checkout at once.

## See also
- [[Safety Invariants]]
- [[assetchan Plugin]]
- [[evmbackend Plugin]]
- [[assetswap Plugin]]
