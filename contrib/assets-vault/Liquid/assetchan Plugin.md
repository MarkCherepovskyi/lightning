---
tags: [liquid, plugin, python]
aliases: [assetchan, assetchan.py]
---

# assetchan Plugin

`contrib/assetchan/assetchan.py` (807 lines) and its helper `contrib/assetchan/elements.py` (82 lines) are the Python plugin half of Liquid asset channels. Core knows a channel's asset and builds and signs its transactions. The plugin turns the feature on (feature bit 233), decides which assets are allowed, funds channels from an elementsd wallet, polices incoming offers, and keeps HTLCs from crossing assets. It must run as an `important-plugin`, and it disables itself otherwise (`assetchan.py:628`).

> [!note]
> Amounts in this plugin are base units of the asset. On the wire one unit is 1000 msat (`assetchan.py:29`). Hence `// 1000` in `channel_summary` (`:217-219`) and `* 1000` in `assetchan-invoice` (`:430`).

## 1. Module docstring and feature bits (`assetchan.py:1-57`)

The docstring (`:2-29`) is the plugin's contract. It lists eight duties:

1. Turn asset channels on, because core only takes them when a plugin offers the feature bit.
2. Fund channels from the elementsd wallet: the asset goes to the 2-of-2 at output n, and an L-BTC fee reserve goes to the same script at n+1.
3. Police incoming offers (`openchannel` hook).
4. Make our side of a close pay to an address of that wallet.
5. Tie invoices to an asset (`assetchan-invoice`) and refuse payments or forwards in the wrong asset (`htlc_accepted`).
6. Pay only over channels in the invoice's asset through an askrene layer (`assetchan-pay`, `assetchan-layer`).
7. After a unilateral close, let core sweep our asset outputs, paying fees with L-BTC from CLN's own wallet, not elementsd's.
8. Run as an important plugin.

Constants and the `Plugin` object:

| Line | Item | Meaning |
|---|---|---|
| `:42` | `CHANNEL_ASSET_FEATURE = 1 << 233` | Optional (odd) bit. Core names it `OPT_CHANNEL_ASSET` (232 is the compulsory twin) in `common/features.h`. |
| `:46` | `ANCHORS_FEATURE = 1 << 23` | `option_anchors_zero_fee_htlc_tx`, optional. Lightningd turns it off on Elements, and the plugin turns it back on. On Bitcoin it is already on, and lightningd refuses to load the plugin. |
| `:47-49` | `Plugin(dynamic=False, init_features=..., node_features=...)` | Both bits go into the `init` message and `node_announcement` features. Plugins can set feature bits only at startup, hence `dynamic=False`. |
| `:50-54` | `plugin.elements`, `plugin.lbtc`, `plugin.invoice_assets`, `plugin.layers`, `plugin.layers_lock` | Lazy elementsd client, cached L-BTC asset id, `payment_hash -> asset` map, askrene layers created so far with the scids disabled in each, and a lock for that map. |
| `:56-57` | `LBTC = 'lbtc'`, `INVOICE_KEY = ['assetchan','invoice']` | `'lbtc'` is the pseudo-asset of ordinary channels. Datastore key `assetchan/invoice/<payment_hash>` holds the asset string. |

## 2. Validation helpers (`:60-73`)

- `check_asset(asset)` (`:60`) requires 32 bytes of hex and raises `RpcException` otherwise.
- `check_asset_or_lbtc` (`:70`) also allows the literal `lbtc`.

## 3. elementsd access (`:76-195`)

The helpers are listed in the table below, in file order. The ElementsRpc client they use is covered in section 11.

| Function | Lines | What it does |
|---|---|---|
| `rpc_password(cfg)` | `:77-94` | `listconfigs` conceals `bitcoin-rpcpassword` as `"..."`. If so, it reads `source` (`path:line`) and re-reads that config line, accepting only a `bitcoin-rpcpassword=` line. It returns None on any failure. |
| `elements(plugin)` | `:97-113` | Builds `ElementsRpc` once. It uses `assetchan-elements-rpc` if set. Otherwise it assembles a URL from `bitcoin-rpcuser`, `-rpcpassword`, `-rpcport` and `-rpcconnect` (default `127.0.0.1`). If the user, password or port is missing, it raises a message telling you to set `assetchan-elements-rpc`. The wallet comes from `assetchan-elements-wallet`. |
| `lbtc_asset(plugin)` | `:116-120` | `dumpassetlabels['bitcoin']`, cached. |
| `new_address(plugin)` | `:123-127` | `getnewaddress` then `getaddressinfo[...]['unconfidential']`. It must be unconfidential because channel transactions pay explicit outputs and core only knows those addresses. |
| `script_address(ed, spk)` | `:130-134` | `decodescript`. The address is at the top level for a witness script, or under `segwit` for a wrappable one. |
| `is_explicit(utxo)` | `:140-143` | True if both blinders are all zeros. |
| `explicit_utxo(plugin, asset, amount)` | `:146-166` | Finds an unblinded wallet UTXO (`listunspent 0`, so unconfirmed counts) with at least `amount` of `asset`. If none exists, it pays itself `amount` to a fresh address with `sendtoaddress`, using positional params ending in the `assetlabel`. It then decodes that tx and returns the output that is explicit and pays the new script. Otherwise it raises. |
| `funding_fee(ed, n_in, n_out)` | `:169-176` | Takes `max(minrelaytxfee, mempoolminfee)` per kvB, estimates vbytes as `12 + 70*in + 45*out`, doubles it, and applies a floor of 1000 sat. This is a deliberately generous fixed estimate. |
| `pays(out, spk, asset, units)` | `:179-182` | True if a decoded output is an explicit `units` of `asset` to `spk`. |
| `funding_outnum(...)` | `:185-194` | Scans for index n where output n pays the asset amount to the channel script and output n+1 pays the L-BTC reserve. It raises if elementsd's transaction lacks that pair. This is the plugin's own cross-check of the n / n+1 layout core expects (see [[Asset Transactions]]). |

> [!warning]
> The funding transaction is explicit throughout, with no confidential values. Blinded inputs would make a blinded tx that core cannot verify. `explicit_utxo` exists to guarantee that.

## 4. Channel helpers (`:197-252`)

- `our_channels`, `find_channel`, `channel_summary`, `allowed_assets`, `channel_asset`, `load_invoice_assets`, `in_background`.
- `our_channels` (`:198`) is `listpeerchannels()['channels']`. Core adds an `asset` field to asset channels, and its absence means L-BTC.
- `find_channel` (`:202-208`) matches the argument against the channel id, peer id, scid or aliases. It returns the match only if exactly one channel matches, so a peer with two channels is ambiguous and yields None.
- `channel_summary` (`:211-220`) selects `peer_id`, `channel_id`, `short_channel_id`, `state`, `opener` and `close_to_addr`, plus `asset` (default `lbtc`), `fee_reserve` (from `fee_reserve_msat // 1000`), `to_us` and `total`.
- `load_invoice_assets` (`:223-227`) rebuilds `plugin.invoice_assets` from `listdatastore(key=INVOICE_KEY)`. It runs at init, so the map survives restarts.
- `allowed_assets` (`:230`) is the multi-valued option `assetchan-asset`, or `[]`.
- `channel_asset(plugin, scid, channels=None)` (`:235-241`) matches `short_channel_id` or the local alias. It returns the asset, or None if we have no such channel.

> [!warning]
> The comment on `channel_asset` says callers must refuse on None, not guess. The forward path does so (`:567-570`).

- `in_background` (`:244-252`) is explained in section 6.

## 5. RPC methods (`:255-517`)

### Which methods are async, and why

Only `assetchan-open` (`:269`) and `assetchan-pay` (`:487`) use `@plugin.async_method`. Both just call `in_background`, which starts a daemon thread. The thread calls `request.set_result(fn(...))`, or `request.set_exception(e)` on failure. In pyln a sync method's return value is sent immediately, whereas an async method must answer itself (`pyln/client/plugin.py:704-720`).

These two are the ones that can wait for a long time: `assetchan-open` does many elementsd round trips plus `fundchannel_*`, and `assetchan-pay` blocks inside `xpay` until the payment resolves. A blocked plugin main loop would delay the `htlc_accepted` hook, and core waits on that hook for every incoming HTLC. So they run in a thread. Every other method is short and stays synchronous.

> [!note]
> `plugin.layers_lock` (`:54`) exists because `assetchan-pay` threads and the main loop can both call `asset_layer`.

### `assetchan-open` (`:269-337`)

Parameters: `id, amount, asset, fee_reserve=None, announce=False, close_to=None`. The work happens in `open_channel` (`:282`) in this order:

1. `check_asset(asset)`. Reject `announce=true` (`:284-286`), since gossip cannot say which asset a channel is in.
2. `fee_reserve` defaults to option `assetchan-default-fee-reserve` (`:288-290`).
3. If no `close_to` was given and `assetchan-close-to == 'elements'`, use a fresh wallet address (`:291-292`).
4. `fundchannel_start` with `amount`, `announce:false`, `asset`, `fee_reserve` and optionally `close_to` (`:296-300`). This is the core RPC extended in this fork. It returns the funding `scriptpubkey` and address, and the channel enters the "awaiting funding" step.
5. Inside a `try` (`:303-333`), the plugin builds the explicit funding PSBT with elementsd. It uses the helpers from section 3:
   - fee estimate with `funding_fee(ed, 2, 5)`: 2 inputs, 5 outputs.
   - `x_in = explicit_utxo(asset, amount)`, `l_in = explicit_utxo(lbtc, fee_reserve + fee)` (`:310-311`).
   - `lockunspent False` on both inputs, so the wallet does not spend them meanwhile (`:313`).
   - outputs are the asset to the channel address, the L-BTC reserve to the same address, asset change if any, L-BTC change if at least 1000 sat (smaller change goes into the fee), and an explicit `fee` output (`:314-322`).
   - `createpsbt`, then `walletprocesspsbt`. It requires `complete`, then runs `finalizepsbt` and `decoderawtransaction` (`:323-327`).
   - `funding_outnum` verifies the asset output is followed by the L-BTC reserve output (`:328`).
6. `fundchannel_complete(id, psbt, withhold=False)` gives the PSBT to core (`:329`). `withhold=False` makes core broadcast nothing and publish nothing itself, so the plugin must do it.
7. `sendrawtransaction` broadcasts the finalized tx via elementsd (`:330`).
8. On any exception, `cancel_funding` (`:256-266`) runs `fundchannel_cancel` and unlocks the inputs, then re-raises (`:331-333`).
9. It returns `{txid, channel_id, outnum, asset, fee_reserve}` (`:336-337`).

```mermaid
sequenceDiagram
    participant U as user
    participant P as assetchan plugin
    participant C as lightningd
    participant E as elementsd
    U->>P: assetchan-open id amount asset
    P->>C: fundchannel_start (asset, fee_reserve, close_to)
    C-->>P: scriptpubkey, funding_address
    P->>E: listunspent / lockunspent / createpsbt / walletprocesspsbt / finalizepsbt
    P->>C: fundchannel_complete (psbt, withhold=false)
    C-->>P: channel_id
    P->>E: sendrawtransaction
    P-->>U: txid, channel_id, outnum
```

> [!warning]
> The broadcast happens after core accepted the PSBT, and the failure path calls `fundchannel_cancel`. If `sendrawtransaction` fails, the channel is cancelled and the UTXOs unlocked. A crash between `fundchannel_complete` and the broadcast is not handled here. See [[Asset Channel Lifecycle]] for what core does with an unbroadcast funding.

### `assetchan-close` (`:340-349`)

Synchronous. It calls core's `close`. If no `destination` was given and `assetchan-close-to == 'elements'`, it looks the channel up with `find_channel`. If the channel is an asset channel (`'asset' in c`) with no `close_to` already fixed at open, it passes a fresh wallet address (`:345-348`). That way the swept assets are spendable by elementsd.

### `assetchan-list` (`:352-356`)
Asset channels only (`'asset' in c`), optionally filtered by asset, via `channel_summary`.

### `assetchan-funds` (`:359-367`)
`getbalances.mine.trusted` of the elementsd wallet, converted to base units, as `elements`. Unless `channels=false`, it also returns all channels (not only asset ones) as `channels`.

### `assetchan-send` (`:370-378`)
`sendtoaddress` from elementsd's wallet. `asset` may be an asset id or an elementsd label such as `bitcoin`. Returns `{txid}`.

### `assetchan-listchannels` (`:381-395`)
Gossip channels, each tagged `asset: lbtc`. Asset channels are never announced and core accepts announcements only with an L-BTC funding output, so the gossiped graph is all L-BTC. For any other asset the result is `[]`.

### `assetchan-newaddr` (`:398-401`)
A fresh unconfidential address of the wallet.

### `assetchan-invoice` (`:424-436`) and `asset_hints` (`:404-421`)
`asset_hints` collects the scids and local/remote aliases of our `CHANNELD_NORMAL` channels in the asset. It intersects them with `listincoming` (peer connected, channel_update received). It raises "No channel in X can receive yet" if the intersection is empty.

`assetchan-invoice`:
1. Pick a random preimage, and store `asset` under `assetchan/invoice/sha256(preimage)` (`must-create`) and in `plugin.invoice_assets`, **before** the invoice exists. An invoice of ours can then never be paid while the plugin would take it for an L-BTC one (a failed datastore write after the invoice used to leave exactly that).
2. `invoice(amount*1000, label, description, expiry, preimage, exposeprivatechannels=hints)`. The explicit hints are needed because core would otherwise pick channels in any asset, and none at all through a peer without public channels. If the invoice fails, the datastore entry is deleted again.
3. Return the invoice with an extra `asset` field.

### `assetchan-layer` and `assetchan-pay` (`:450-517`)
`asset_layer(plugin, asset)` builds or refreshes the askrene layer `assetchan.<asset>`:

- It creates the layer if `askrene-listlayers` does not list it. It is non-persistent (`:463`), so it vanishes on restart and is recreated lazily.
- `disable(scid)` disables both directions with `askrene-update-channel` and remembers the scid in `plugin.layers[layer]`, so each is disabled only once (`:466-473`).
- For each of our channels, it disables names (scid and local alias) of channels in other assets (`:475-482`).
- For an asset other than L-BTC, it also disables every gossiped channel that is not ours (`:483-486`). Gossip is all L-BTC, and our own channels in the asset are the only ones available. The invoice's route hints, which askrene adds itself, stay usable.

`assetchan-layer` just returns `{layer, asset}`. `pay` (`:511-517`) calls `xpay` with `invstring` and `layers=[asset_layer]`, and optionally `maxfee`. It adds `asset` to the result.

`assetchan-pay` takes a bolt11 invoice only (`decode`): `assetchan-invoice` makes those, and a BOLT12 blinded path could name our first channel itself.

> [!note]
> `xpay` is not told the asset. The protection is only that routing is restricted to channels in the asset. The receiver's `htlc_accepted` hook then checks the asset at the final hop, and every forwarder checks again (section 6).

### Plain payments stay off asset channels (`on_rpc_command`, `:520-643`)
An `rpc_command` hook, filtered to every payment command that routes for itself (`PARAMS`), treats a plain payment as an L-BTC one. It runs `before` `cln-xpay`, which takes `pay` and `keysend` over by replacing them with an xpay command no other hook sees; with ours first, ours wins.

| Command | What the hook does |
|---|---|
| `xpay`, `xpay-as-pay`, `sendamount`, `xkeysend` | add the `assetchan.lbtc` layer (every asset channel disabled), unless a layer starting `assetchan.` is already named (`assetchan-pay`, `payassetinvoice` say which asset they pay in): then left alone |
| `pay` | rewritten to `xpay-as-pay` with the `assetchan.lbtc` layer (`pay_as_xpay`). `exclude` is refused (use xpay's `layers`); `maxfeepercent`/`exemptfee` become a `maxfee` (`fee_as_maxfee`), as xpay does |
| `keysend` | rewritten to `xkeysend` with the layer (`keysend_as_xkeysend`). `routehints` is refused; `maxfeepercent`/`exemptfee` become a `maxfee` |
| `renepay` | our asset channels added to `exclude`: both directions, by scid and local alias (`asset_exclusions`) |

- Positional params are named first (`named`). A call with more positional params than the hook knows names for is refused ("pass them by name"): it can't tell what they do to the route.
- Any error (layer, decode, a refused param) fails the command with "cannot keep this payment off asset channels": fail closed.
- Raw `sendpay` and `sendonion` are left alone: the caller names the first hop, so that is the caller's choice.

Without this, a payee with an asset channel to us could get N units of a valuable asset for an invoice of N sat. Core covers the one case no layer can: a BOLT12 blinded path that starts at us picks our first channel itself, and `injectpaymentonion` refuses to send it over an asset channel. Test: `test_plain_payments_avoid_asset_channels` (`tests/test_assetchan.py:887`) covers every command above, positional calls included.

## 6. Hooks (`:646-736`)

### `openchannel` policy (`:646-671`)
This is the policy for incoming offers.

1. If the offer has no `asset` field, it is an ordinary channel: `continue` (`:650-652`).
2. If `channel_flags & 1` (announce) is set: reject (`:653-656`).
3. If the asset is not in `assetchan-asset`: reject (`:657-659`).
4. The fee reserve is `fee_reserve_msat // 1000` and must lie in `[assetchan-min-fee-reserve, assetchan-max-fee-reserve]`, otherwise reject (`:660-665`).
5. If `assetchan-close-to == 'elements'`, return `close_to` with a fresh wallet address (`:667-668`). The channel then closes to the elementsd wallet.

### `htlc_accepted` (`:694-736`)
Registered with `after=['keysend']` (`:696`). Keysend's hook turns a keysend payment into an invoice. Running after it means we see that invoice as an ordinary invoice and treat it as L-BTC like any other (`:694-695`).

Two branches, split on whether the onion carries `short_channel_id` or `next_node_id` (`:703`):

- Forward branch.
  - If core found no channel to forward to (`forward_to is None`), return `continue` and core fails it (`:704-705`).
  - Look up the asset of the incoming channel with `channel_asset` (`:707`). If we have no such channel, fail with `400a` (`unknown_next_peer`) (`:708-711`).
  - `forward_channel` (`:674-691`) returns core's chosen channel if it is in the incoming asset, or if core's choice is unknown. Otherwise it picks another channel to the same peer that is in that asset, `CHANNELD_NORMAL`, with the same `fee_base_msat` and `fee_proportional_millionths` and with `spendable_msat >= forward_msat`, preferring the largest spendable. It returns None if there is none.
  - If the result equals `forward_to`, `continue`. If it is another channel, `continue` with `forward_to` set to it (`:713-716`). If None, fail `400a` (`:717-719`).
- Final-hop branch (`:720-736`).
  - `want = plugin.invoice_assets.get(payment_hash)` (`:720`).
  - If there is no entry and `listinvoices(payment_hash)` returns nothing, it is not our invoice: `continue`. Core fails it, unless another plugin takes it, such as a swap provider's leg 1 (see [[assetswap Plugin]]) (`:721-725`).
  - If it is an ordinary invoice without an entry, `want = lbtc` (`:726`).
  - `got = channel_asset(plugin, htlc['short_channel_id'])`. If `got == want`, `continue`.
  - Otherwise fail with `10070000` (BOLT 4 `temporary_channel_failure` without a channel_update). The comment explains why: payers route around that channel, whereas `incorrect_or_unknown_payment_details` would make them give up on the invoice (`:732-736`).

```mermaid
flowchart TD
    H[htlc_accepted] --> F{onion has scid or next_node_id}
    F -- yes --> N{forward_to set}
    N -- no --> C1[continue: core fails]
    N -- yes --> A{incoming channel known}
    A -- no --> X1[fail 400a]
    A -- yes --> R{core choice in same asset}
    R -- yes --> C2[continue]
    R -- no --> S{sibling channel same peer, asset, fees, enough spendable}
    S -- yes --> C3[continue with forward_to = sibling]
    S -- no --> X1
    F -- no --> I{our invoice}
    I -- no --> C4[continue: core or other plugin]
    I -- yes --> M{arrived asset == invoice asset}
    M -- yes --> C5[continue]
    M -- no --> X2[fail 10070000]
```

> [!warning]
> If the hook raises, pyln fails the HTLC (docstring `:701-702`). pyln logs a "safe fallback response" for hooks that have one (`pyln/client/plugin.py:719-727`).

## 7. Subscription (`:740-749`)

`invoice_payment`: when an invoice is paid, the plugin computes `sha256(preimage)` and pops the hash from `plugin.invoice_assets`. If there was an entry, it also deletes the datastore key (`deldatastore`, errors are logged as warnings). A paid invoice no longer needs its asset noted. Ordinary invoices have no entry and are ignored.

## 8. Init and the important-plugin requirement (`:752-784`)

`is_important` (`:752-761`) asks `listconfigs('important-plugin')`, collects `values_str` plus a possible single `value_str`, and compares `realpath` of each to `realpath(sys.argv[0])`.

`init` checks in order and returns `{'disable': reason}` on the first failure (a disabled plugin offers no feature bit, so core starts no asset channel):

1. Network must start with `liquid` (`:766-767`).
2. Must be an important plugin (`:769-771`). The reason: without it, a node would take asset-channel offers in any asset and payments in the wrong asset.
3. `assetchan-asset` must have at least one value (`:772-773`).
4. Every `assetchan-asset` must be 64 hex digits (`:774-778`).
5. `assetchan-close-to` must be `elements` or `none` (`:779-780`).
6. Then it loads the invoice assets from the datastore and logs the fee reserve range (`:781-784`).

> [!info]
> The comment on `is_important` says lightningd stops if an important plugin dies. Core's channeld guard checks only that the feature bit is offered, which outlives the plugin. So it is the important flag that makes "no plugin" impossible while asset channels run. Core backs this up: it drops bits 232/233 from any plugin that is not an `important-plugin` (`lightningd/plugin.c`), so only an important plugin can turn asset channels on.

## 9. Options (`:787-806`)

| Option | Default | Type | Meaning |
|---|---|---|---|
| `assetchan-asset` | none | multi string | Asset ids we open and accept channels in. Required (init disables otherwise). |
| `assetchan-min-fee-reserve` | `1000` | int (sat) | Smallest fee reserve accepted from an opener. |
| `assetchan-max-fee-reserve` | `10000` | int (sat) | Largest fee reserve accepted. |
| `assetchan-default-fee-reserve` | `2000` | int (sat) | Reserve `assetchan-open` puts up by default. |
| `assetchan-close-to` | `elements` | string | `elements`: our asset goes to a fresh wallet address. `none`: whatever core picks. |
| `assetchan-elements-rpc` | unset | string | `http://user:password@host:port`. By default derived from the `bitcoin-rpc*` settings. |
| `assetchan-elements-wallet` | `''` | string | Wallet name. Empty means elementsd's default wallet. |

> [!question]
> The default reserve (2000) lies inside the accepted range (1000..10000), but nothing checks that `assetchan-default-fee-reserve` is itself within our own `min..max`. A user could open channels the plugin would reject if the peer ran the same limits.

## 10. What the plugin guarantees, and what core guarantees instead

| Concern | Plugin | Core |
|---|---|---|
| Which assets are accepted/opened | `assetchan-asset`, `openchannel` hook (`:657`) | Core refuses the asset channel unless the feature bit is offered (`lightningd/channel_control.c:1825`), and takes that bit only from an `important-plugin` (`lightningd/plugin.c`) |
| Private-only | Rejects `announce` in open and offer (`:284`, `:653`): this is the only place it is enforced | Nothing: a raw `fundchannel_start asset=` with `announce=true` is stopped only by the fundee's plugin. Gossip still carries only L-BTC capacity, since core takes announcements only with an L-BTC funding output |
| Forwards stay in one asset | Reroutes to a sibling in the same asset (`:674`, `:696`) | Final rule: a forward leaves in the asset it arrived in, whatever a plugin said (`lightningd/peer_htlcs.c:887-904`) |
| Payment in the invoice's asset | `htlc_accepted` final-hop check (`:720-736`) | Nothing: the asset of an invoice is a plugin notion |
| Transactions, signatures, sweeps, HTLC txs | None | Core builds and signs them. Sweeps are paid from CLN's own L-BTC wallet |
| Plain payments stay L-BTC | `on_rpc_command` (layer, rewrite or `exclude`) | `injectpaymentonion` refuses a blinded path that starts at us over an asset channel (`lightningd/pay.c`) |
| Fee reserve range | `min/max` options | Only that the fee reserve is above 660 sat (both anchors) and pays for at least 2 HTLCs at the relay floor (one per side: upstream refuses `max_accepted_htlcs` 0; `check_config_bounds`). The "≥ dust" rule in core is for the channel reserves, not the fee reserve |
| Funding layout n / n+1 | `funding_outnum` cross-check (`:185`) | Verifies the funding tx |

The plugin is policy and convenience. Core enforces the safety rules that cannot be delegated: no cross-asset forward, no asset channel without the feature bit offered, and the transaction structure. See [[Core - lightningd]], [[Safety Invariants]] and [[Asset Channel Lifecycle]].

> [!warning]
> The one safety property that lives only in the plugin is the final-hop invoice check. Without the plugin running, a node could accept an HTLC arriving in the wrong asset for an invoice. That is exactly why `init` insists on `important-plugin`.

## 11. elements.py (`contrib/assetchan/elements.py`, 82 lines)

Standard library only, so the tests can reuse it (`:1-2`).

| Item | Lines | What it does |
|---|---|---|
| `COIN = 10**8` | `:12` | Base units per coin. |
| `ElementsError(RpcException)` | `:15-18` | Subclass with code `-1`. |
| `ElementsRpc.__init__(url, wallet='')` | `:24-33` | Strips credentials from the URL, appends `/wallet/<quoted name>` if a wallet is given, and builds a Basic auth header. |
| `ElementsRpc.call(method, *params)` | `:35-57` | Positional JSON-RPC 1.0 over `urllib`, timeout 120 s. HTTP errors are read for their JSON body, because an RPC error is an HTTP 500. Network errors and unparsable replies raise `ElementsError`. Numbers parse as `Decimal`, so there is no float loss. A response `error` raises `ElementsError('elementsd <method>: <message>')`. |
| `rpc_url(user, password, host, port)` | `:60-63` | Quotes the credentials. |
| `coins(units)` | `:66-68` | Base units to an exact `X.XXXXXXXX` string. |
| `base_units(amount)` | `:71-73` | `Decimal(str(amount)) * COIN` to int. |
| `config_value(cfg, name, default)` | `:76-82` | The first of `value_int`, `value_str`, `value_bool`, `values_str` present in a `listconfigs` entry. |

> [!note]
> `next_id` is incremented without a lock. `open_channel` and `pay` run in background threads while `assetchan-funds` and others use the same `ElementsRpc` from the main thread. Only the JSON-RPC id is shared, and each call is a separate HTTP request, so interleaving is harmless, but IDs may collide.

## 12. nodes.sh (`contrib/assetchan/nodes.sh`, 131 lines)

A development launcher for up to six lightningd nodes A to F from this source tree, with the plugin, on Liquid testnet (default) or Liquid regtest.

Commands (`:8-13`, `:73-130`): `start <node> [lightningd options]`, `stop`, `cli <node> ...`, `ecli ...` (elements-cli with the wallet), `mine [n]` (regtest only, `:121`), `log <node> [n]` (tail -f).

Environment (`:15-28`): `ASSETCHAN_NETWORK` (`liquid-testnet` or `liquid-regtest`), `ASSETCHAN_PORT_BASE` (default 20740; node ports are base+1..6, `:56-69`), `ASSETCHAN_DIR` (default `~/.assetchan-testnet` or `~/.assetchan-regtest`), `ELEMENTS_RPC` (defaults `http://poc:poc@127.0.0.1:7039` on testnet and `...:18884` on regtest), `ELEMENTS_WALLET` (default `poc`), `ELEMENTS_CLI`, `CHANNEL_ASSET` (or file `$ASSETCHAN_DIR/asset`).

What `start` launches (`:74-108`):
- It writes a wrapper `$dir/elements-cli` that execs the docker `elements-cli` command (adding `-i` to `docker exec`), because lightningd's bcli plugin runs it (`:81-84`).
- It `exec`s `lightningd/lightningd` from the tree.

| Flag | Value | Purpose |
|---|---|---|
| `--network` | `$network` | Liquid chain. |
| `--lightning-dir` | `$dir/$node` | Per-node directory. |
| `--alias` | `assetchan-$node` | |
| `--disable-plugin=cln-grpc` | | Not needed. |
| `--bind-addr` | `127.0.0.1:<port>` | Local only. |
| `--bitcoin-rpcconnect/-rpcport/-rpcuser/-rpcpassword` | from `ELEMENTS_RPC` | Chain backend is elementsd. |
| `--bitcoin-cli` | `$dir/elements-cli` | The wrapper above. |
| `--important-plugin` | `contrib/assetchan/assetchan.py` | Required by the plugin. |
| `--assetchan-asset` | `$CHANNEL_ASSET` | The one allowed asset. |
| `--assetchan-elements-rpc` / `--assetchan-elements-wallet` | `$rpc` / `$wallet` | Plugin's own elementsd link. |
| `--watchtime-blocks=6`, `--funding-confirms=2` | | Short timers for testing. |
| `--log-level=debug`, `--log-file`, `--developer`, `--daemon` | | Debug logs, daemonised, stdout to `$dir/$node/stdout`. |

Extra options passed after the node name are appended last (`:107`).

## 13. How to operate it

Short version, with the full procedure in [[Operations]]:

1. Run elementsd with a wallet holding the asset and some L-BTC. Both are needed for `assetchan-open`.
2. Start lightningd on a `liquid-*` network with `--important-plugin=.../assetchan.py` and `--assetchan-asset=<id>`. Add `--assetchan-elements-rpc` if `bitcoin-rpc*` settings do not point at elementsd, which they normally do.
3. Check the log for the "fee reserve ... closing to ..." line (`:641`). If the plugin disabled itself, the reason is logged by lightningd.
4. `lightning-cli assetchan-open <peer> <amount> <asset>`, then mine or wait for confirmations. `assetchan-list` and `assetchan-funds` show the state.
5. Receive with `assetchan-invoice` and pay with `assetchan-pay`. Do not pay an asset invoice with plain `pay` or `xpay`: nothing then keeps the payment on channels in that asset.
6. Keep some L-BTC in CLN's own wallet. After a unilateral close, core pays sweep fees from it, and elementsd's wallet funds only opens (`:22-23`).

## See also

- [[Liquid Asset Channels]]
- [[Asset Channel Lifecycle]]
- [[Asset Transactions]]
- [[Core - lightningd]]
- [[Safety Invariants]]
- [[Operations]]
- [[assetswap Plugin]]
- [[Glossary]]
