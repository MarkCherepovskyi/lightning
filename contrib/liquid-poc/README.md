# Liquid asset channels: local test network

Proof of concept, not for real funds: asset channels can be force-closed only
while no HTLC is in flight, and there are no penalty transactions for them.

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
./lnpoc.py force-close bob carol             # unilateral close, mine until both sides swept
./lnpoc.py force-close bob carol --to-elements   # ... and bob's asset to an elementsd address
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

## Unilateral close

`force-close` stops the peer, so the `close` goes unilateral, then mines
until both sides have swept their outputs.

- The commitment tx spends both funding outputs: the peer signs the fee
  reserve input too (`fee_reserve_sig` in `funding_created`, `funding_signed`
  and `commitment_signed`), so it can always be broadcast.
- Fees on Liquid are L-BTC, so an asset output can't pay for its own sweep.
  The opener's L-BTC change from the fee reserve goes to the same script as
  its asset output, and is swept with it to pay the fee; the other side pays
  from its L-BTC wallet. Leftover L-BTC returns to the node's wallet.
- `lightningd`'s wallet can't hold assets, so a swept asset goes where a
  cooperative close would pay it: the `close_to` address of
  `fundchannel_start`, else the `destination` of `close`, else the
  channel's default close address (a key of the node's HD wallet, which
  `listfunds` doesn't show).

## Limits

Still missing for asset channels:

- **HTLCs on-chain.** A unilateral close works only without HTLCs in flight.
  HTLC outputs are in the asset, and HTLC-success/timeout txs are pre-signed
  and would have to pay their L-BTC fee from somewhere (CLN runs Liquid
  without anchors). `onchaind` logs such outputs as BROKEN and leaves them
  unresolved, so the payment stays stuck.
- **Penalties.** A revoked commitment's asset outputs aren't claimed (BROKEN,
  ignored); our own to_remote output on it is still swept.
- **Assets in the wallet.** `lightningd`'s wallet ignores non-L-BTC outputs,
  so swept assets go to the channel's close address (see above) and don't
  show up in `listfunds`; funding uses an external wallet (elementsd).
- Sweeps are rebuilt (with the wallet's current L-BTC) whenever they're
  rebroadcast, until they confirm, but there's no RBF fee bumping; and an
  asset left at the default close address can only be spent with an external
  wallet holding CLN's HD keys.
- Commitments of channels opened before `fee_reserve_sig` existed can't be
  broadcast, and both peers must run this version (an older peer doesn't send
  the signature, and the channel fails).
- One asset per channel. Invoices don't carry the asset yet: `xpay` is told
  it with `asset=`.
- Regtest has no fee estimates, so the nodes use the minimum feerate (as on
  Bitcoin regtest).
