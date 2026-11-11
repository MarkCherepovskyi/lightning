# `lq`: command line for the Liquid asset-channel nodes

`lq.py` drives four Core Lightning nodes (alice, bob, carol, dave) and one
`elementsd` that run in Docker, so you can open channels, pay, close and move coins
without typing `lightning-cli` and `elements-cli` calls. It is a thin layer over
`lnpoc.py` and the nodes' RPCs, for testing. It is not a wallet: use test coins
only.

```sh
cd contrib/liquid-poc
./lq.py -h
./lq.py status
```

## Before you start

- Docker running, and the stack up:
  - real Liquid testnet (default): `docker-compose.testnet.yml`, a synced
    `elementsd`, and `./lnpoc.py testnet-setup` done once (see `TESTING.md`);
  - regtest: `LNPOC_NET=regtest ./lnpoc.py up && ./lnpoc.py setup`, then
    `LNPOC_NET=regtest ./lq.py ...`.
- Python 3, no extra packages.
- **Amounts are base units**: asset units for an asset, satoshis for L-BTC. An
  asset has 8 decimals like L-BTC, so `100000` units is `0.001` of the asset.
- "The default asset" is the one `testnet-setup` (or `lq issue`) made. Every command
  that takes `--asset ID` uses it when you don't pass one; `--lbtc` switches the
  command to L-BTC.
- Waiting: on the testnet a block takes about a minute. `open` waits for the funding
  tx and 6 blocks, so it takes about ten minutes; `close` waits for one block.

## Commands

### `lq status`

Block height, the `elementsd` wallet, each node's wallet (L-BTC and assets) and every
channel.

```
chain liquidtestnet, block 2679213 of 2679213
elementsd wallet: ASSET(723376e4) 9994000000, L-BTC 55509
alice  wallet: L-BTC 9059, ASSET(723376e4) 1050000
...
node   peer   asset            state            ours     total  scid / alias
alice  bob    ASSET(723376e4)  ONCHAIN        400000    500000  ...
```

### `lq address NODE|elements`

A new address: of the node's wallet (`bech32`, where coins and swept assets arrive),
or of the `elementsd` wallet (`elements`, unblinded; what you give the faucet).

### `lq faucet`

Asks https://liquidtestnet.com/faucet for 100,000 sat of L-BTC to the `elementsd`
wallet. This sends that wallet's address to the faucet. Testnet only.

### `lq issue AMOUNT`

Issues a new asset with `AMOUNT` whole units from the `elementsd` wallet (explicit,
not blinded), and makes it the default asset. The previous default asset is still in
the wallet.

### `lq fund NODE SATS`

Sends L-BTC from the `elementsd` wallet to a node's wallet. Nodes need some L-BTC:
it pays the fee of every on-chain spend of an asset (sweeps, HTLC transactions,
`lq send`), because on Liquid fees can only be paid in L-BTC.

### `lq open FROM TO UNITS [--lbtc] [--asset ID] [--announce] [--fee-reserve SATS] [--change SATS]`

Opens a channel from `FROM` to `TO` with `UNITS` of the asset (or `UNITS` sats with
`--lbtc`), and waits until it is ready.

- For an asset channel the funding transaction is built by `elementsd`: the asset to
  the channel's 2-of-2, and `--fee-reserve` sats of L-BTC (default 6000) to the same
  2-of-2. Every on-chain transaction of the channel pays its fee from that reserve.
  The funding input carries `--change` extra sats (default 2000) that come back to
  `elementsd`.
- `--announce` makes the channel public (gossip, on the asset's own network).
  Otherwise it is private.
- The `elementsd` wallet needs the asset, and about `fee-reserve + 1,000 + change`
  sats of L-BTC.
- The maximum channel size is 16,777,215 units.

### `lq channels`

All channels of all nodes: asset, state, our balance and the total, in units.

### `lq invoice NODE UNITS [--lbtc] [--asset ID]`

Prints an invoice for `UNITS` that `NODE` receives in the asset (or in L-BTC with
`--lbtc`). One payment is capped at 2^32 msat (about 4.29 million units).

### `lq pay FROM TO UNITS [--lbtc] [--asset ID]`

Makes an invoice on `TO` and pays it from `FROM` along our open channels of that
asset (private channels are fine: the route is built from the channel list, with
fees and timelocks). Prints the payment preimage. The sender cannot spend all of its
balance: the channel reserve and the HTLC fee are kept back, so check
`spendable_msat` in `lightning-cli listpeerchannels` if a payment is refused.

### `lq close FROM TO [--force] [--lbtc] [--asset ID]`

- Without `--force`: a cooperative close. Both nodes must be online. It waits for the
  closing transaction to be mined and prints its outputs.
- With `--force`: stops `TO`'s container, so `FROM`'s close goes unilateral, then
  mines/waits through the whole resolution: `TO`'s sweep and `FROM`'s sweep after the
  CSV delay. On the testnet this takes the delay in blocks (6 on these channels), plus
  confirmations. Anything you started on `TO` is stopped; `TO` is started again.

### `lq send NODE ADDRESS UNITS [--lbtc] [--asset ID]`

Spends `NODE`'s coins on-chain to `ADDRESS`: the asset with `withdrawasset`, or
L-BTC with `--lbtc` (`withdraw`). Prints the txid. For an asset, only confirmed
outputs are spent, and the fee is paid from the node's L-BTC.

## Underlying RPCs

`lq` uses two RPCs added for asset channels; you can call them with
`lightning-cli` on a node.

### `withdrawasset destination asset amount [feerate]`

Spends an asset from the wallet to an address.

- `destination`: an on-chain address.
- `asset`: the asset id as `elements-cli` prints it (64 hex characters).
- `amount`: asset units, non-zero.
- `feerate`: optional, in sat per 1000 weight; default from the fee estimates.

It selects confirmed, unreserved outputs of the asset, adds wallet L-BTC for the fee,
sends the asset change and the L-BTC change to new wallet addresses, and broadcasts.
The outputs it uses are reserved for 6 blocks, or released if the broadcast fails.
Returns `tx` (hex) and `txid`.

Errors: `Not on Liquid`; `amount must be non-zero`; a bad asset id; `Cannot afford
N units of ASSET: only M confirmed and unreserved`; `Not enough L-BTC in the wallet to
pay the fee (need X, have Y)`; and an error from the broadcast, in which case the
inputs are released.

### `listassetfunds [spent]`

Assets the node holds. With `spent` (boolean, default false) the spent outputs are
included.

- `outputs`: assets paid to the wallet's keys: `txid`, `output`, `asset`, `amount`,
  `scriptpubkey`, `status` (`unconfirmed`, `confirmed` or `spent`), `blockheight`,
  `spendheight`.
- `channels`: the node's share of each asset channel: `peer_id`, `state`,
  `channel_id`, `asset`, `our_amount_msat`, `amount_msat`, `fee_reserve_msat` (all
  asset units times 1000 in the `msat` fields).

`listfunds` shows only L-BTC.

## Related

- `TESTING.md`: what to run to test, and the testnet setup.
- `TESTNET_TESTS.md`: the cases run on the real testnet, with their transactions.
- `README.md`: the regtest network and `lnpoc.py`.
