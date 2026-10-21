# Liquid asset channels: local test network

Proof of concept, not for real funds (see [Limits](#limits)).

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

### HTLCs on-chain

HTLC outputs are in the asset too, so an HTLC transaction can't take its
fee out of the HTLC the way it does on L-BTC channels. Asset channels work
like `option_anchors_zero_fee_htlc_tx` here, but without anchor outputs,
and the commitment and HTLC scripts are the ones for channels without
anchors:

- HTLC-success and HTLC-timeout transactions pay no fee: output 0 is the
  whole HTLC, in the asset. The peer signs them with
  `SIGHASH_SINGLE|SIGHASH_ANYONECANPAY` (only input 0 and output 0), and so do
  we.
- Whoever broadcasts one adds L-BTC inputs from its wallet, its L-BTC change
  and the fee output. It rebuilds the tx with what its wallet holds before
  each rebroadcast.
- Direct spends from the peer's commitment (fulfilling their HTLC with the
  preimage, or taking back our timed-out HTLC) are sweeps like the ones
  above: the asset goes to the close address, and wallet L-BTC pays the fee.
- The HTLC transaction's output (to the delayed key, after `to_self_delay`)
  is swept like `to_local`, also with wallet L-BTC.

The other choice was to pre-sign HTLC transactions that also spend an L-BTC
output of the commitment, paid from the fee reserve. That would need an extra
commitment output per HTLC and would fix the fee when the commitment is
signed. With `SIGHASH_SINGLE|SIGHASH_ANYONECANPAY` the commitments stay as
they are, and the fee is whatever is needed at broadcast time.

So a node needs some L-BTC in its wallet to resolve HTLCs on-chain, the
peer that didn't open the channel included. Trimmed HTLCs (below dust plus
what the HTLC tx would have paid as fee on an L-BTC channel) get no output.
Their asset goes to the opener's output, because the asset can't go to
miners.

### Penalties

If the peer broadcasts a revoked commitment, we take all of its asset outputs
with the revocation key: its `to_local`, its HTLC outputs, and the outputs
of any HTLC transactions it builds on them. Each one gets its own penalty
tx, sending the asset to the close address. The opener's L-BTC change from the
fee reserve has the same script as its `to_local`. When the cheater is the
opener, we take that change in the `to_local` penalty, and it pays the fee.
Every other penalty gets its fee from our wallet.

## Limits

Still missing for asset channels:

- **L-BTC to resolve HTLCs.** HTLC transactions, and spends of HTLC outputs
  and penalties (except the one that takes the cheater's fee reserve
  change), pay their fees with L-BTC from the node's wallet. A node with no
  L-BTC can't resolve HTLCs on-chain until it gets some (it keeps
  retrying every block), and its peer can then time them out or claim them.
  Concurrent asset spends don't pick the same wallet L-BTC (`lightningd`
  remembers which one each broadcast tx uses), but other wallet spends
  (`withdraw`, `fundchannel`) don't know about it. A wallet with fewer
  outputs than there are spends to make resolves them one after another,
  using the change of the previous one. The commitment itself has no
  anchors, so it can't be fee-bumped: its fee comes from the fee reserve
  when it's signed.
- **Assets in the wallet.** `lightningd`'s wallet ignores non-L-BTC outputs,
  so swept assets go to the channel's close address (see above) and don't
  show up in `listfunds`; funding uses an external wallet (elementsd).
- Sweeps, HTLC transactions and penalties are rebuilt (with the wallet's
  current L-BTC) whenever they're rebroadcast, until they confirm, but
  there's no RBF fee bumping, and no batching (one tx per output). An asset
  left at the default close address can only be spent with an external
  wallet holding CLN's HD keys.
- Asset outputs of a commitment we can't identify (neither ours, theirs nor
  revoked, e.g. after restoring an old database) aren't recovered (BROKEN).
- Commitments of channels opened before `fee_reserve_sig` existed can't be
  broadcast, and both peers must run this version (an older peer doesn't send
  the signature, or signs HTLC txs that pay fees in the asset, and the
  channel fails).
- One asset per channel. Invoices don't carry the asset yet: `xpay` is told
  it with `asset=`.
- Regtest has no fee estimates, so the nodes use the minimum feerate (as on
  Bitcoin regtest).
