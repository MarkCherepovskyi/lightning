# Liquid asset channels: local test network

Proof of concept, not for real funds: asset channels can't be force-closed yet.

A Liquid regtest node (`elementsd`) and three Core Lightning nodes (`alice`,
`bob`, `carol`) built from this branch, plus `lnpoc.py` to drive them.

An **asset channel** holds an issued asset (here a test asset `setup` issues)
as the channel balance, plus an L-BTC **fee reserve** from the opener that pays
on-chain fees. Ordinary **L-BTC channels** work next to them. Nodes never
forward between channels of different assets, and an invoice in an asset can
only be paid over channels in that asset.

## Start

```bash
cd contrib/liquid-poc
./lnpoc.py up          # docker compose up -d --build (first build takes a while)
./lnpoc.py setup       # elements wallet, 101 blocks, test asset, fund nodes, connect alice-bob-carol
```

`setup` remembers the issued asset in `.lnpoc-state.json`.

## Use

```bash
./lnpoc.py open-lbtc  alice bob 5000000      # L-BTC channel, 5,000,000 sat
./lnpoc.py open-asset alice bob 5000000      # asset channel, 5,000,000 asset units
./lnpoc.py open-lbtc  bob carol 5000000
./lnpoc.py open-asset bob carol 5000000
./lnpoc.py channels                          # every channel, which asset, balances

./lnpoc.py route alice carol 1000000000      # path + route in the asset (msat = milli-units)
./lnpoc.py pay   alice carol 1000000000      # invoice on carol, pay from alice along that path
./lnpoc.py pay   alice carol 1000000000 --lbtc
./lnpoc.py balances

./lnpoc.py close alice bob                   # cooperative close of the asset channel (+ mine)
./lnpoc.py close alice bob --lbtc
```

Or all of it at once: `./lnpoc.py demo`.

## Gossip (announced channels)

`setup` also makes every node follow the asset's gossip network
(`liquid-gossip-asset=<asset>` in its config, then a restart). Channels opened
with `--announce` are gossiped; an asset channel goes on the asset's own
network (the asset id where gossip messages carry the chain hash), so nodes
that don't follow the asset ignore it.

```bash
./lnpoc.py open-lbtc  alice bob 5000000 --announce
./lnpoc.py open-asset alice bob 5000000 --announce
./lnpoc.py open-lbtc  bob carol 5000000 --announce
./lnpoc.py open-asset bob carol 5000000 --announce
./lnpoc.py graph alice                    # alice's gossip map, each channel with its asset
./lnpoc.py xpay alice carol 1000000000    # xpay finds the path itself, in the asset
./lnpoc.py xpay alice carol 1000000000 --lbtc
```

Or `./lnpoc.py demo-gossip`. Routing stays inside one network: `getroutes`
and `xpay` use L-BTC channels unless given `asset=<id>`.

Other commands: `mine [n]`, `invoice <node> <msat> [--lbtc]`, `down [-v]`
(`-v` also wipes the chain, the nodes and the saved asset id).
Any `lightning-cli` / `elements-cli` call works directly too:

```bash
docker compose exec alice lightning-cli --network=liquid-regtest --lightning-dir=/data getinfo
docker compose exec elementsd elements-cli -chain=liquid-regtest -rpcport=18884 -rpcuser=poc -rpcpassword=poc getblockchaininfo
```

## How an asset channel is opened

`lightningd` can't hold assets yet, so elementsd's wallet builds the funding
transaction (`lnpoc.py open-asset` does all of this):

1. `fundchannel_start id=<peer> amount=<units> asset=<id> fee_reserve=<sat> announce=false`
   returns the channel's funding address.
2. elementsd builds a tx paying `<units>` of the asset to that address at
   output 0 and `<sat>` of L-BTC to the same address at output 1 (explicit,
   unblinded outputs).
3. `fundchannel_complete id=<peer> psbt=<signed psbt>`, then broadcast.

## Paths

`route` / `pay` work with private channels, so they don't use `xpay` (that
needs announced channels, see above). `lnpoc.py` reads `listpeerchannels` on all three nodes,
keeps only normal channels in the requested asset with enough spendable
balance, takes the shortest path, works out fees and CLTV deltas back from the
payee, and pays with `sendpay`. Forwarding nodes are addressed by their local
alias for the channel, as BOLT 2 requires for private channels.

## Limits

- No unilateral close for asset channels.
- One asset per channel. Invoices don't carry the asset yet: `xpay` is told
  it with `asset=`.
- Regtest has no fee estimates, so the nodes use the minimum feerate (as on
  Bitcoin regtest).
