# Asset-swap quick guide

Step-by-step for running a cross-asset swap-provider ("server") and a payer
("client") on top of the `contrib/liquid-poc` network, and for creating and
paying invoices, same-asset or cross-asset. Design background:
[`CROSS_ASSET_SWAP.md`](CROSS_ASSET_SWAP.md). All commands below are
run from `contrib/liquid-poc/`.

## 1. Plugins required

Two Rust plugins, both already built into the `cln` Docker image and loaded
on **every** node from startup — nothing to install or enable by hand in
this PoC:

- `assetswap-plugin` — the swap mechanics (`assetinvoice`, `payassetinvoice`,
  `quoteassetinvoice` RPCs; the provider-side `htlc_accepted` hook).
- `pricetiers-plugin` — the pricing policy (`pricetiers` RPC), only
  consulted on a node configured as a provider.

`docker-compose.yml`'s shared `x-cln` entrypoint is what actually loads
them, with a `--plugin=` flag per plugin on every node's command line:

```yaml
--plugin=/opt/lightning/libexec/c-lightning/plugins/assetswap-plugin
--plugin=/opt/lightning/libexec/c-lightning/plugins/pricetiers-plugin
```

Outside this PoC's Docker setup, a provider node needs both loaded via a
`plugin=` line in its `config` file (not `lightning-cli plugin start` —
a dynamically-started plugin isn't reloaded on restart, and a provider must
have it loaded before `lightningd` can replay a pending HTLC; see
`CROSS_ASSET_SWAP.md` §7).

## 2. UI example

The same flows are available from the web UI (`./lqui.py`, see the main
`README.md`'s "Web page" section), without touching the command line:

1. **Make a node a swap-provider**: "Add node" tab → check "Set up as a
   swap service" → pick the pair and rate. (An existing node can also be
   made one with `swap-provider` on the command line, step 2 below — the
   UI only wires this up for a newly added node.) Provider nodes show up in
   the "Network" map with a dashed outline and a "SWAP SERVICE" tag, and as
   a "Swap service" pill in the Nodes list.
2. **Create a cross-asset invoice**: "Invoice" tab → pick the node to be
   paid and the asset it wants (its own asset, or L-BTC) → the hint line
   shows the live swap rate for that pair → submit to get the wrapped
   invoice string.
3. **Pay it**: "Pay invoice" tab → pick the paying node, paste the invoice
   string, leave "Asset to pay with" on "Auto" (direct payment, swapping
   only if the node doesn't hold the invoice's asset) or pick one
   explicitly, choose a tier → submit. The hint line shows what the swap
   will actually cost before you confirm.

If you're a true Mandalorian that respects raw CLI commands - next steps are for you!

*This is the way*

## 3. Launch the swap-provider ("server")

A provider is just a node that already holds channels in both assets and is
configured with a price for the pair.

1. Start the network and have the provider node (e.g. `bob`) hold channels
   in both assets, e.g.:
   ```bash
   ./lnpoc.py up && ./lnpoc.py setup
   ./lnpoc.py open-lbtc  alice bob 5000000
   ./lnpoc.py open-asset bob carol 5000000
   ```
   (`./lnpoc.py start` does `up` + `setup` + the web UI in one call.)
2. Configure the pricing for the pair it will quote:
   ```bash
   ./lnpoc.py swap-provider bob lbtc <asset> 1000000 --fee-base-msat 500
   ```
   `rate_ppm` is msat of `from_asset` required per 1,000,000 msat worth of
   `to_asset`; `1000000` = 1:1. This writes a persistent
   `pricetiers-pair=lbtc:<asset>:1000000:500` line to bob's config and
   restarts him — no separate pricing config file.

That's it — the node is now a provider. It needs no connection step: a
payer's leg-1 payment reaches it like any other routed payment.

## 4. Launch a swap client ("payer")

1. Point the payer at the provider's node id (also persistent, survives a
   restart):
   ```bash
   ./lnpoc.py swap-payer alice bob
   ```
   This just writes `assetswap-provider=<bob's node id>` to alice's config.
2. Make sure alice has a path to bob in whichever asset she'll pay leg 1
   with (an ordinary channel/route — no direct peering with bob required).

## 5. Invoices and payments

### Same asset on both sides (no swap)

- `./lnpoc.py invoice <node> <amount_msat> [--asset ID | --lbtc]` — create a
  bolt11 invoice requiring that asset.
- `./lnpoc.py pay <frm> <to> <amount_msat> [--asset ID | --lbtc]` — invoices
  `to` and pays it from `frm` over a path `lnpoc.py` finds itself (private
  channels, `sendpay`).
- `./lnpoc.py xpay <frm> <to> <amount_msat> [--asset ID | --lbtc]` — same,
  but routed by `xpay`/gossip (channels must be `--announce`d).

### Cross-asset (swap through a provider)

- `./lnpoc.py assetinvoice <node> <amount_msat> <asset>` — create the
  invoice on the payee and wrap it (bolt11 + asset) into one opaque string.
- `./lnpoc.py quoteassetinvoice <node> <assetinvoice-string>` — *optional*:
  ask a node's own local pricing what paying that string would cost, without
  paying (only meaningful run against the provider itself).
- `./lnpoc.py payassetinvoice <node> <assetinvoice-string> [--tier low|medium|high] [--from-asset ID] [--from-amount-msat N]` —
  pay the wrapped invoice from `node`. With no `--from-asset`, it tries a
  direct same-asset payment first and falls back to a swap via the
  configured provider.
- `./lnpoc.py swap <frm> <to> <amount_msat> <asset> [--tier low|medium|high]` —
  does the two steps above in one call: invoices `to` in `asset`, then pays
  it from `frm`.

Either way, the receiver and the direct-payment path never need to know a
swap happened — `assetinvoice`/`payassetinvoice` are drop-in replacements
for `invoice`/`pay` when the payer doesn't hold the invoice's asset.

## 6. Testing the RPCs directly (shell)

`lnpoc.py`'s swap commands are thin wrappers around `assetswap-plugin`'s own
RPCs; the same calls work directly against any node with `lightning-cli`
(`docker compose exec <node> lightning-cli --network=liquid-regtest
--lightning-dir=/data ...`, same pattern as the main `README.md`), useful
for poking at the plugin itself:

```bash
# payee (carol): wrap an invoice for 1,000,000,000 msat of <asset>
docker compose exec carol lightning-cli --network=liquid-regtest --lightning-dir=/data \
  -k assetinvoice amount_msat=1000000000 label=lnpoc-test description="test invoice" asset=<asset>
# -> prints the wrapped assetinvoice string

# optional: ask what paying it would cost (only meaningful against the provider itself)
docker compose exec bob lightning-cli --network=liquid-regtest --lightning-dir=/data \
  -k quoteassetinvoice assetinvoice="<assetinvoice-string>"

# payer (alice): pay it -- auto (direct, falling back to the configured provider)
docker compose exec alice lightning-cli --network=liquid-regtest --lightning-dir=/data \
  -k payassetinvoice assetinvoice="<assetinvoice-string>"

# ... or force a swap priced in a specific asset/amount instead of auto
docker compose exec alice lightning-cli --network=liquid-regtest --lightning-dir=/data \
  -k payassetinvoice assetinvoice="<assetinvoice-string>" tier=medium from_asset=lbtc from_amount_msat=1000500
```

Plain same-asset invoices are just the underlying core RPCs:

```bash
docker compose exec carol lightning-cli --network=liquid-regtest --lightning-dir=/data \
  -k invoice amount_msat=1000000000 label=lnpoc-test description="test invoice" asset=<asset>
docker compose exec alice lightning-cli --network=liquid-regtest --lightning-dir=/data \
  -k xpay invstring=<bolt11> asset=<asset>
```

## 7. End-to-end example

```bash
./lnpoc.py swap-demo
```

Does all of the above in one shot: opens alice↔bob (L-BTC) and bob↔carol
(the test asset), makes bob a provider between them, points alice at bob,
then runs `swap alice carol <amount> <asset>` and prints balances.
