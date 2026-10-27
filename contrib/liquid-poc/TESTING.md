# Testing Liquid asset channels

What to run, from fastest to slowest. Everything is a proof of concept: use test
coins only.

| Level | What it covers | Needs | Time |
| --- | --- | --- | --- |
| 1. Unit tests | wire format, transaction builders, chainparams | a build of this tree | minutes |
| 2. Liquid regtest suite | open, pay, close, force close, HTLCs on-chain, penalties, gossip, bookkeeper | Docker | ~12 min |
| 3. Regtest demo network | hand-driven playground (3 nodes) | Docker | ~10 min first time |
| 4. Real Liquid testnet | the same cases on the real network (4 nodes) | Docker, 7 GB disk, internet | hours (first sync) |

## 1. Unit tests

```sh
make check-units          # gmake on macOS
```

Includes `bitcoin/test/run-chainparams_fee_asset.c`, which checks each Liquid
network's fee asset (L-BTC) against its published id. That mix-up is invisible on
regtest and breaks every transaction on the real network.

## 2. Liquid regtest suite (pytest, in Docker)

```sh
contrib/liquid-poc/run-tests.sh                  # tests/test_liquid_asset_poc.py + gossip tests
contrib/liquid-poc/run-tests.sh -k penalty       # some of them
contrib/liquid-poc/run-tests.sh logs DIR         # copy out the last run's test directories
```

The tree is built for Linux in a Docker volume (the first build is slow, later ones
are incremental). Expect all tests to pass (28 at the time of writing).

## 3. Regtest demo network

```sh
contrib/liquid-poc/lnpoc.py up           # one elementsd (liquid-regtest) + alice, bob, carol
contrib/liquid-poc/lnpoc.py setup        # blocks, a test asset, fund and connect the nodes
contrib/liquid-poc/lnpoc.py demo         # open both kinds of channels, pay in both, close
contrib/liquid-poc/lnpoc.py -h           # every command
contrib/liquid-poc/lnpoc.py down -v      # delete everything
```

See `README.md` for the details.

## 4. Real Liquid testnet

Four nodes (alice, bob, carol, dave) and one `elementsd`, from
`docker-compose.testnet.yml`. Blocks come every minute and nobody can mine them, so
opening or closing a channel takes minutes, and a sweep after a force close waits for
real blocks.

### Setup (once)

```sh
cd contrib/liquid-poc
docker compose -f docker-compose.testnet.yml up -d --build elementsd
```

Wait for `elementsd` to sync (about 2.7 million blocks, 2 to 3 hours, ~7 GB):

```sh
docker compose -f docker-compose.testnet.yml exec elementsd \
  elements-cli -chain=liquidtestnet -rpcport=7039 -rpcuser=poc -rpcpassword=poc \
  getblockchaininfo        # initialblockdownload must be false
```

Then start the nodes and set everything up:

```sh
docker compose -f docker-compose.testnet.yml up -d --build
export LNPOC_NET=testnet
./lnpoc.py testnet-setup   # elementsd wallet, faucet L-BTC, our own asset, fund and connect the nodes
```

`testnet-setup` sends the wallet address to https://liquidtestnet.com/faucet
(100,000 sat per request). Ask again with `./lq.py faucet` when you need more:
each channel costs about 7,000 sat (a 6,000 sat fee reserve plus fees; the rest
of the funding input comes back).

### The `lq` command line

```sh
export LNPOC_NET=testnet            # (the default of lq.py)
./lq.py status                      # block height, wallets, channels
./lq.py open alice bob 500000       # asset channel (units); --lbtc for a plain L-BTC one
./lq.py pay alice bob 10000
./lq.py close alice bob             # cooperative; --force: peer offline, unilateral
./lq.py send alice <address> 1000   # spend on-chain (asset); --lbtc for L-BTC
./lq.py -h
```

### The test cases

`TESTNET_TESTS.md` lists the cases with their results and transactions (T1 to T14).
The ones that need more than a few commands have scripts in `testnet/`. Run them
from that directory:

| Case | Command |
| --- | --- |
| T7 HTLC in flight, payer force-closes (also restarts the payer: T11) | `python3 t_htlc.py PAYER PAYEE FUNDING_PREFIX payer restart` |
| T8 HTLC in flight, payee force-closes | `python3 t_htlc.py PAYER PAYEE FUNDING_PREFIX payee` |
| T9 revoked commitment | `python3 t9.py FUNDING_PREFIX` (a channel bob → carol) |
| T10 announced channels, gossip, `xpay` | `python3 t10.py` (channels alice → bob and bob → carol opened with `--announce`) |
| T12 bookkeeper after a force close where the peer sweeps | `python3 t12b.py` |

`FUNDING_PREFIX` is the first 8 characters of the channel's funding txid. The HTLC
cases hold the payee's reply with `testnet/delay.py` (a small plugin that waits 40 s
before an incoming HTLC is processed), so there is time to take the right side
offline while the HTLC is committed. Run one case per node pair at a time, since
the scripts stop and restart nodes.

### Cleaning up, and what to avoid

- `docker compose -f docker-compose.testnet.yml down` keeps the chain and the node
  databases. **`down -v` deletes the synced chain and the node keys**: another
  multi-hour sync, and any channel funds are gone.
- A channel can be stuck if its closing transaction was signed by a broken build.
  After fixing the cause, only a fresh channel works.
- The wallets are for tests only: never fund them with anything that matters.
- If a node's wallet shows an "unconfirmed" coin that never confirms (the leftover
  of a transaction the network rejected), an on-chain spend may keep choosing it.
  Remove it from the node's database while the node is stopped.
