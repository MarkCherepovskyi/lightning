# Cross-asset payments via a swap-provider plugin

Design doc, not yet implemented. Proof of concept, not for real funds (see
[README.md Limits](../README.md#limits) for the asset-channel limits this builds
on).

## Problem

Today an invoice can require a specific asset (`invoice ... asset=<id>`) and
a payer can pay it (`xpay ... asset=<id>`), but only when the whole route
stays inside that one asset's network: nodes never forward between channels
of different assets (see README.md). There is no way to pay an asset-B
invoice while only holding asset-A channels.

## Why not a core change

The natural-sounding fix — let one hop in the route convert asset A to asset
B — is blocked by `lightningd` itself: `check_fwd_amount()` and this fork's
`liquid_asset_eq()` check in `forward_htlc()`
(`../../../lightningd/peer_htlcs.c`) are unconditional, run after any plugin hook
returns, and have no plugin override point. Making a single HTLC change
amount/asset mid-route would require patching that core forwarding logic.

Instead, this design uses **two independent, hash-linked LN payments** — the
standard "submarine swap" pattern — stitched together entirely by a plugin,
with **no changes to `lightningd`, `gossipd`, `channeld`, or `../../../common`**. It
relies on the `htlc_accepted` plugin hook's documented ability to hold an
HTLC and later `resolve`/`fail` it, which *does* bypass the two checks above
by design (the hook's own schema: "When the plugin resolves, lightningd
skips the normal checks for a final node... the plugin must make sure these
values are correct").

The asset requirement also isn't cryptographically bound into the invoice:
BOLT11 has no asset field (nothing in `../../../common/bolt11.c`), consistent with
this fork's existing limitation that invoices don't carry the asset — it's
communicated out-of-band today, and stays out-of-band here too, via a small
plugin-level wrapper format instead of a BOLT11/core change.

## Architecture

Picture three people. **Bob** wants to be paid in asset B. **Alice** wants
to pay him but only has asset A. **Carol** is a "swap provider" — a node
that happens to hold channels in both asset A and asset B, and is willing
to act as a money changer for a small fee.

Normally, Lightning payments work like a single sealed envelope that gets
passed hop by hop from payer to payee: every node along the way forwards
the *same* envelope, and the only thing that changes hop to hop is a
shrinking fee. That "single envelope" model is exactly why a currency
change mid-route isn't possible without touching `lightningd`'s core
forwarding code — the envelope's contents aren't allowed to change shape at
a hop, only its size.

So instead of trying to pass one envelope all the way from Alice to Bob,
this design uses **two separate envelopes, glued together by a shared
secret**:

1. **Bob creates his invoice as normal** (asset B), which comes with a
   secret number only Bob knows at first — call it the *preimage* — and a
   public fingerprint of that secret, the *payment hash* `H`, which anyone
   can see on the invoice.

2. **Alice sends envelope #1 to Carol**, paid in asset A, *addressed with
   that same fingerprint `H`* — even though Alice doesn't have Bob's
   secret and isn't paying Bob directly. Carol receives this envelope but
   doesn't open it (doesn't claim the money) yet — she just holds onto it.

3. **Carol then sends envelope #2 to Bob**, paid in asset B, which is just
   an ordinary Lightning payment of Bob's real invoice. To collect this
   money, Bob has to reveal his secret preimage back to Carol — that's how
   Lightning payments always complete.

4. **The moment Carol learns Bob's secret, she uses it to unlock envelope
   #1** from Alice, which was fingerprinted with that exact same secret.
   Now Carol has been paid in asset A, Bob has been paid in asset B, and
   Alice's payment has gone through — a currency swap happened, but no
   single hop ever had to "convert" an HTLC; two independent, ordinary
   payments were simply chained together by one shared secret.

The safety property that makes this trustworthy: **Carol can only unlock
envelope #1 *after* she has already paid Bob and learned the secret.** If
paying Bob fails for any reason, Carol never learns the secret, can never
unlock envelope #1, and it simply expires back to Alice untouched. There is
no window where Carol could take Alice's money without the payment to Bob
having already gone through.

In Lightning terms: "envelope #1" is a normal multi-hop HTLC payment from
Alice to Carol (`sendpay`, asset-A network), and "envelope #2" is a normal
payment from Carol to Bob (`xpay`, asset-B network) — both using the same
payment hash `H`. Carol's node holds envelope #1 open using the
`htlc_accepted` plugin hook's documented ability to delay its answer, fires
off envelope #2, and only then tells `lightningd` "resolve envelope #1,
here's the secret." Nothing about how `lightningd` forwards or validates
HTLCs needs to change for this to work.

### Topology

Carol is the only node that needs channels in both assets. Alice and Bob
each only ever touch one asset network:

```
   asset A network              asset B network
 ┌────────────────┐           ┌────────────────┐
 │ Alice ───── Carol ───────── Carol ───── Bob  │   <- Carol is one node,
 └────────────────┘  (same    └────────────────┘      with a foot in both
                       node)                           networks
```

### Sequence (time flows downward)

```
  Alice                       Carol                        Bob
    │                           │                           │
    │                           │       1. creates invoice  │
    │                           │◄──────  for asset B,      │
    │◄───── "assetinvoice" ─────┼──────── amount Y, hash H  │
    │       (bolt11 + asset,    │                           │
    │        out-of-band)       │                           │
    │                           │                           │
    │ 2. Alice estimates what   │                           │
    │    it'll cost in asset A  │                           │
    │    -- her own local       │                           │
    │    pricetiers-plugin, or  │                           │
    │    any other source she   │                           │
    │    trusts. NOT a message  │                           │
    │    to Carol, and not      │                           │
    │    binding on anyone.     │                           │
    │                           │                           │
    │ 3. sendpay: HTLC for      │                           │
    │    amount X, asset A,     │                           │
    │    locked to hash H,      │                           │
    │    carrying Bob's bolt11  │                           │
    │    + asset B in the       │                           │
    │    HTLC's own             │                           │
    │    payment_metadata       │                           │
    │ ─────────────────────────►│                           │
    │                           │  [HTLC HELD — Carol        │
    │                           │   decodes payment_metadata,│
    │                           │   decodes Bob's bolt11     │
    │                           │   herself, and checks the  │
    │                           │   LIVE price: does X cover │
    │                           │   Y at today's rate? No    │
    │                           │   quote was pre-approved —  │
    │                           │   this IS the only check.  │
    │                           │   Short → fail, no leg 2   │
    │                           │   attempted.]              │
    │                           │                           │
    │                           │ 4. xpay: pay Bob's real   │
    │                           │    invoice, amount Y,     │
    │                           │    asset B, hash H        │
    │                           │ ─────────────────────────►│
    │                           │                           │
    │                           │ 5. Bob reveals the        │
    │                           │    preimage P to claim it │
    │                           │ ◄───────── preimage P ────┤
    │                           │                           │
    │                           │ 6. Carol now has P, so    │
    │                           │    she resolves Alice's   │
    │                           │    held HTLC with it      │
    │ ◄──── HTLC settled, ──────┤                           │
    │       using preimage P    │                           │
    ▼                           ▼                           ▼
 Alice is out X        Carol is out Y,          Bob received Y
 in asset A            back in X in asset A      in asset B
                        (net: fee only)
```

Step 6 can only happen *after* step 5 — Carol physically cannot settle
Alice's HTLC with the right preimage until Bob has already revealed it to
her, which only happens once Carol has actually paid him. That ordering is
the entire safety argument: there is no point on this timeline where Carol
holds Alice's money without Bob having already been paid, or vice versa.

Both legs share the **same payment_hash H** — the payee's real invoice hash.
The provider never resolves leg 1 until leg 2 has actually succeeded and it
holds the preimage, so it can never pay out leg 2 without being made whole
on leg 1; if leg 2 fails, leg 1 is simply failed too and no funds move on
either side. Each leg's own pathfinding reuses the existing
`askrene`/`xpay` `asset=` filtering (`../../../plugins/askrene/askrene.c`,
`param_liquid_asset_network`) unchanged — nothing new needed there.

## New component: `../../../plugins/assetswap-plugin` (Rust)

Written against this repo's own in-tree Rust plugin framework
(`cln_plugin` + `cln_rpc`, the same stack used by
`../../../plugins/currencyrate-plugin`, `plugins/lsps-plugin`, `plugins/bip353-plugin`),
not Python. One crate, loaded identically on payer, payee, and provider
nodes — role is driven by which commands/options are used, not by separate
binaries. Registering it is the same one-line addition every sibling
plugin required: add `"plugins/assetswap-plugin"` to the root
`Cargo.toml`'s `[workspace] members` list — still zero touches to
`lightningd`/`gossipd`/`channeld`/`common`/`wire`.

Using Rust instead of a Python/pyln-client plugin also removes a real wart
the first draft of this design had: holding an HTLC open while waiting on
a slow RPC call (leg 2). `pyln-client`'s dispatch loop is single-threaded,
so a Python hook handler would have to manually spawn a thread to avoid
stalling every other hook/notification for the duration of leg 2.
`cln_plugin` is built on `tokio`, so a hook handler can simply hold the
request and `.await` the leg-2 call directly — no manual threading needed.
This exact "hold now, resolve later" pattern already exists in this repo
as a working example:
[`../../../plugins/examples/cln-plugin-reentrant.rs`](../../../plugins/examples/cln-plugin-reentrant.rs)
holds an `htlc_accepted` hook on a channel and only replies once a
separate async event fires — the swap provider's hook does the same thing,
substituting "leg 2 finished" for that event.

### 1. `assetinvoice` RPC (payee side)

Wraps the existing `invoice` RPC (`asset=`, already supported) and packages
the result into one opaque, self-decoding string:

```rust
#[derive(Serialize)]
struct AssetInvoiceWrapper {
    v: u8,
    bolt11: String,
    payment_hash: String,
    asset: String,
    amount_msat: u64,
}

async fn assetinvoice(p: Plugin<State>, args: Value) -> Result<Value, Error> {
    let mut rpc = ClnRpc::new(p.configuration().rpc_file).await?;
    let inv: InvoiceResponse = rpc.call_typed(&InvoiceRequest { asset: Some(asset.clone()), .. }).await?;
    let wrapper = AssetInvoiceWrapper { v: 1, bolt11: inv.bolt11, payment_hash: inv.payment_hash,
                                         asset, amount_msat };
    let encoded = base64::engine::general_purpose::URL_SAFE.encode(serde_json::to_vec(&wrapper)?);
    Ok(json!({ "assetinvoice": encoded }))
}
```

This is the plugin-level stand-in for "an invoice that specifies the
asset" — no BOLT11/core change needed, since `asset` genuinely isn't in the
signed bytes.

### 2. Swap-provider config (static for v1)

Pricing policy — which pairs are quotable, at what reference rate, with
what tier margins — is deliberately **not** `assetswap-plugin`'s own
config at all. It lives in a separate, swap-mechanics-agnostic plugin,
**`../../../plugins/pricetiers-plugin`** (`pricetiers-plugin` binary), which
`assetswap-plugin` calls as a plain local RPC (`pricetiers`, via
`cln_rpc::ClnRpc::call_raw` — not a core lightningd method, so not
`call_typed`). This plugin knows nothing about Liquid assets, swaps, or
`htlc_accepted` at all; asset ids are opaque strings to it, just matched
against what a `pricetiers` caller asks for. Splitting it out this way
means a different pricing policy (e.g. a genuine oracle-backed one, see
below) can be swapped in without touching `assetswap-plugin`.

Plugin options read at startup via `cln_plugin::options`, no new RPC
required:

- `pricetiers-plugin`'s `--pricetiers-pair=<from_asset>:<to_asset>:<rate_ppm>:<fee_base_msat>`
  (repeatable) — which asset pairs are quotable, and at what reference
  price. **As implemented, this `rate_ppm` is the live price source** —
  the "fixed" source described below, not an external feed. There's no
  separate "recheck": with no pre-negotiated quote to recheck against (see
  §3), this is simply *the* price check, run once, live, when the HTLC
  arrives — so (as noted in Limits) it's a permanently-satisfied-by-a-
  static-number check in this increment, not yet a real defense against
  price movement.
- `assetswap-plugin`'s `--assetswap-provider=<node_id>` (repeatable, payer
  side) — which node leg 1 gets routed to when a direct, same-asset
  payment isn't possible. Onion-message-based discovery is a natural v2;
  not needed for v1.
- **Not yet implemented**: a second pricing-policy plugin, implementing
  the same `pricetiers` RPC contract but backed by
  `../../../plugins/currencyrate-plugin`'s `currencyconvert` RPC (already in this
  tree) as a genuine live-price oracle instead of a static configured
  rate. Because pricing is already its own plugin, this needs no change
  to `assetswap-plugin` at all — just loading a different pricing plugin.
  This is the natural next step for making the price-tier mechanism's
  protection real rather than theoretical — see Limits.

### 3. No pre-negotiation: `payment_metadata` + a live price check

**Revised from the original design below.** The first implementation pass
used a `custommsg` request/reply round trip here — the payer asked the
provider for a quote, the provider pre-approved a price ceiling and
recorded it, and the payer's leg-1 HTLC was checked against that
pre-approval. That had a real cost: `sendcustommsg` only delivers to an
already-`connect`ed peer, so every payer needed a direct connection to
every provider it might use, regardless of whether their leg-1 payment
would ever route through anyone else. That doesn't scale, and there's no
other reason the two ever needed to talk ahead of time.

The replacement: the payer packs what the provider needs directly into leg
1's own HTLC, via `sendpay`/`xpay`'s existing `payment_metadata` field —
arbitrary bytes riding in the final-hop onion TLV, which `htlc_accepted`'s
hook payload already surfaces back out (`onion.payment_metadata`, see
`../../../cln-rpc/src/hooks.rs`'s `HtlcAcceptedOnion`). No new wire protocol, no
`custommsg` hook, no requirement that payer and provider ever be directly
peered — leg 1 is routed exactly like any other multi-hop payment, through
whatever path the asset's announced channels provide.

```rust
// protocol.rs
#[derive(Serialize, Deserialize)]
struct SwapMetadata {
    final_bolt11: String,
    to_asset: String,
}
```

The payer encodes this as hex JSON and sets it as leg 1's
`payment_metadata` (`pay.rs`). There is no reply, no pre-approval, and
nothing written to the provider's `datastore` until the HTLC actually
lands — the provider decides live:

1. Decode `onion.payment_metadata` → `SwapMetadata`. Missing or
   undecodable means this isn't a swap HTLC at all — `{"result":
   "continue"}`, not ours to second-guess.
2. Decode `final_bolt11` itself (`decode`) to learn the real
   `amount_msat`, `payment_hash`, and `min_final_cltv_expiry` — never
   trust a field the payer could have gotten wrong or lied about when a
   decode is one local call away.
3. **The hash-link check, now explicit.** In the old design this was
   implicit: the provider could only ever *find* a quote record by looking
   it up under the leg-1 HTLC's own `payment_hash`, so a mismatched invoice
   simply found nothing. With no pre-written record to look up by, the
   check has to be explicit: `decoded.payment_hash != htlc.payment_hash` →
   fail. Skipping this would let a leg-1 HTLC reference a `final_bolt11`
   whose hash doesn't match what leg 1 itself will resolve with — the
   provider could pay leg 2, learn a preimage, and then be unable to use it
   to claim leg 1 at all (lightningd itself will only resolve an HTLC with
   a preimage that hashes to *its own* `payment_hash`), leaving the
   provider paid out on leg 2 with nothing collected on leg 1.
4. **Live pricing is the only check, not a recheck.** Call
   `pricetiers-plugin`'s `pricetiers` RPC for `to_asset`/`amount_msat` and
   require the leg-1 amount received to cover at least the cheapest
   returned tier. There's no prior quote to compare against — this single
   live call *is* the provider's entire pricing decision, made at the
   moment it matters. Anything above the floor is accepted; the provider
   just keeps the surplus. (Tiers still exist — see §8/pricetiers-plugin —
   but now describe margins a payer might choose to overpay by for a
   better chance of clearing the floor if the price moves between when
   they estimated and when the HTLC lands, not a menu the provider
   validates an exact match against.)
5. Channel-asset and CLTV-budget checks, unchanged in substance from the
   original design (§5 below).

How much the payer actually sends for leg 1 is therefore *its own*
decision, made with no help from the provider: either an explicit
`(from_asset, from_amount_msat)` the caller already knows (see
`payassetinvoice`'s own doc comment in `pay.rs` — `..`'s
`lqui.py` computes this from config it reads directly off every node's
container), or a same-node `pricetiers-plugin` lookup, which only returns
real numbers if this particular node happens to hold a `pricetiers-pair=`
matching the provider's. Underestimating isn't a protocol violation, just
a failed HTLC the payer can retry with more — exactly the same failure
shape as misjudging the CLTV margin (§8).

### 4. `payassetinvoice`/`quoteassetinvoice` RPCs (payer side)

```rust
async fn payassetinvoice(p: Plugin<State>, args: PayAssetInvoiceArgs) -> Result<Value, Error> {
    let wrapper = decode_wrapper(&args.assetinvoice)?;
    let mut rpc = ClnRpc::new(p.configuration().rpc_file).await?;

    // 1. try the direct, no-swap path first — a payer may hold channels in
    //    several asset networks, so don't assume a swap is even needed.
    let direct_err = match rpc.call_typed(&XpayRequest { bolt11: wrapper.bolt11.clone(),
                                                           asset: Some(wrapper.asset.clone()), .. }).await {
        Ok(res) => return Ok(json!(res)),
        Err(e) => e,
    };

    // 2. decide how much to send for leg 1: either the caller already told
    //    us (an explicit from_asset/from_amount_msat — e.g. lqui.py, which
    //    reads every node's config directly, not just this one's), or this
    //    node's own local pricetiers-plugin, if it happens to hold a
    //    matching pricetiers-pair= (only true for a node that's also a
    //    provider). Either way this is an ESTIMATE, not a commitment —
    //    nobody has approved it yet.
    let (from_asset, from_amount_msat) = match args.from_override() {
        Some(o) => o,
        None => {
            let priced = fetch_price_tiers(&mut rpc, &wrapper.asset, wrapper.amount_msat).await
                .map_err(|e| anyhow!("direct payment failed ({direct_err}); can't price a swap locally: {e}"))?;
            let tier = priced.tiers.iter().find(|t| t.name == args.tier.unwrap_or("medium".into()))
                .ok_or_else(|| anyhow!("no {:?} tier for this pair", args.tier))?;
            (priced.from_asset, tier.from_amount_msat)
        }
    };

    // 3. a generous CLTV margin over the invoice's own min_final_cltv_expiry
    //    -- not negotiated (there's no quote reply to learn the provider's
    //    exact requirement from anymore); too little just fails the HTLC,
    //    same failure shape as sending too little for the price.
    let decoded = rpc.call_typed(&DecodeRequest { string: wrapper.bolt11.clone() }).await?;
    let final_cltv = decoded.min_final_cltv_expiry.unwrap() + LEG1_CLTV_MARGIN;

    // 4. pathfind leg 1, in from_asset's network, to the provider
    let routes: GetroutesResponse = rpc.call_typed(&GetroutesRequest {
        source: my_id, destination: provider_node_id,
        amount_msat: from_amount_msat, asset: Some(from_asset.clone()),
        layers: vec!["auto.localchans".into(), "auto.sourcefree".into()],
        final_cltv, ..
    }).await?;
    let route = getroutes_path_to_sendpay_route(&routes.routes[0].path);

    // 5. send with the FINAL recipient's payment_hash (not a fresh one) and
    //    the invoice + to_asset packed into payment_metadata -- this is
    //    the provider's ONLY source of truth for what leg 2 should be.
    let metadata = encode_swap_metadata(&SwapMetadata {
        final_bolt11: wrapper.bolt11.clone(), to_asset: wrapper.asset.clone(),
    })?;
    rpc.call_typed(&SendpayRequest { route, payment_hash: wrapper.payment_hash.clone(),
                                      amount_msat: Some(from_amount_msat),
                                      payment_metadata: Some(metadata), .. }).await?;
    let res: WaitsendpayResponse = rpc.call_typed(&WaitsendpayRequest {
        payment_hash: wrapper.payment_hash, timeout: Some(60), ..
    }).await?;
    Ok(json!(res))
}
```

`quoteassetinvoice` is just step 2's local-pricetiers branch, exposed on
its own, for a caller that wants an estimate without paying — see its doc
comment in `pay.rs` for why it's explicitly *not* a quote from the
provider.

`getroutes_path_to_sendpay_route()` is a small required conversion —
`getroutes`' `path` shape (`node_id_in/out`, `amount_out_msat`, `cltv_out`,
...) differs from `sendpay`'s route-hop shape (`id`, `channel`,
`amount_msat`, `delay`; `lightningd/pay.c:1274-1327`'s `param_route_hop`).
`sendpay` is used directly (not `pay`/`xpay`) specifically because leg 1
has no local invoice to decode from and must carry the *payee's* hash, not
a freshly generated one (`lightningd/pay.c:1596-1611` confirms no local
invoice is required).

### 5. Swap-provider `htlc_accepted` hook

Registered via `.hook("htlc_accepted", on_htlc_accepted)` on the
`Builder`, same as
[`../../../plugins/examples/cln-plugin-reentrant.rs`](../../../plugins/examples/cln-plugin-reentrant.rs).
Must be idempotent across restarts, since the hook's own docs warn that
lightningd replays HTLCs with no final verdict on startup.

```rust
async fn on_htlc_accepted(p: Plugin<State>, v: Value) -> Result<Value, Error> {
    let htlc = HtlcAccepted::parse(v)?;

    // Restart-replay idempotency (§7): a "claimed" record means we already
    // decided before a prior process exited -- re-derive the verdict from
    // leg 2's own status rather than re-decide. "failed" means we already
    // gave lightningd a verdict in a past run; let a fresh HTLC for the
    // same hash (e.g. the payer retrying with more) be judged again.
    if let Some(rec) = lookup_quote_record(&p, &htlc.payment_hash).await? {
        if rec.state == "claimed" {
            return Ok(resolve_from_leg2_status(&p, &rec).await);
        }
    }

    // No prior negotiation -- decode what the payer packed into leg 1
    // itself. Missing/undecodable: not a swap HTLC, not ours to
    // second-guess.
    let Some(meta) = decode_swap_metadata(&htlc.onion.payment_metadata) else {
        return Ok(json!({"result": "continue"}));
    };
    let decoded = decode_bolt11(&p, &meta.final_bolt11).await?;

    // The hash-link check, now explicit (it used to be implicit in how the
    // quote record was looked up -- see §3): leg 1 must be for the SAME
    // payment_hash as the invoice it claims to settle, or we could pay leg
    // 2 and be left holding a preimage leg 1 won't accept.
    if decoded.payment_hash != htlc.payment_hash {
        return Ok(fail_incorrect_details());
    }

    // Live pricing: the ONLY check that decides whether we'd lose money,
    // since nothing was pre-approved. Today's rate, right now -- not a
    // recheck against an earlier snapshot.
    let priced = fetch_price_tiers(&p, &meta.to_asset, decoded.amount_msat).await?;
    let floor_msat = priced.tiers.iter().map(|t| t.from_amount_msat).min().unwrap();
    if htlc.amount_msat < floor_msat {
        return Ok(fail_incorrect_details());   // we'd lose money at our own current price
    }

    if channel_asset(&htlc.short_channel_id).await? != priced.from_asset
        || htlc.cltv_expiry_relative < decoded.min_final_cltv_expiry + SAFETY_BLOCKS + MIN_HOP_BUDGET {
        return Ok(fail_incorrect_details());
    }

    let rec = QuoteRecord { state: "claimed".into(), from_asset: priced.from_asset,
                             to_asset: meta.to_asset, final_bolt11: meta.final_bolt11 };
    if !claim_quote_atomically(&p, &htlc.payment_hash, &rec).await? {
        return Ok(fail_incorrect_details());          // replay: someone already claimed this hash
    }

    // No manual threading needed: just await the leg-2 call. tokio keeps
    // servicing other hooks/RPC calls while this one is pending -- this
    // request simply isn't answered until the match arm below returns.
    match p.rpc().call_typed(&XpayRequest {
        bolt11: rec.final_bolt11.clone(), asset: Some(rec.to_asset.clone()),
        maxdelay: Some(htlc.cltv_expiry_relative - decoded.min_final_cltv_expiry - SAFETY_BLOCKS), ..
    }).await {
        Ok(res) => Ok(json!({"result": "resolve", "payment_key": res.payment_preimage})),
        Err(_) => Ok(fail_incorrect_details()),
    }
}
```

Every rejection uses `incorrect_or_unknown_payment_details` (hex `400f`)
uniformly — it needs no extra data (unlike `expiry_too_soon`) and doesn't
leak which check failed (bad amount, wrong asset, insufficient CLTV
margin, a live price we no longer clear, or leg-2 failure all look
identical to the payer).

### 6. Replay protection

In the original design a `"quoted"` record always already existed by the
time a claim was attempted (written during the quote step), so the claim
was always a `must-replace`. With nothing written until first accept,
`claim_quote_atomically` now has two cases: nothing on disk yet
(`must-create`), or an earlier `"failed"` record from a previous attempt
for the same hash, which a fresh retry is allowed to overwrite
(`must-replace` with its generation as the CAS check). Either way, exactly
one concurrent/replayed HTLC for a given hash wins:

```rust
async fn claim_quote_atomically(p: &Plugin<State>, payment_hash: &str, rec: &QuoteRecord) -> Result<bool> {
    let mut rpc = ClnRpc::new(p.configuration().rpc_file).await?;
    let existing = rpc.call_typed(&ListdatastoreRequest { key: quote_key(payment_hash) }).await?.datastore.into_iter().next();
    let (mode, generation) = match &existing {
        None => (DatastoreMode::MUST_CREATE, None),
        Some(d) => (DatastoreMode::MUST_REPLACE, d.generation),
    };
    let claimed = QuoteRecord { state: "claimed".into(), ..rec.clone() };
    match rpc.call_typed(&DatastoreRequest {
        key: quote_key(payment_hash), string: Some(serde_json::to_string(&claimed)?),
        mode: Some(mode), generation, ..
    }).await {
        Ok(_) => Ok(true),
        Err(_) => Ok(false),   // a concurrent/replayed HTLC already claimed this hash
    }
}
```

On a genuine leg-2 failure (not a replay — `xpay` itself returned an
error), the record is moved to a further terminal `"failed"` state rather
than left sitting in `"claimed"` forever, which would otherwise look like
an in-flight swap indefinitely:

```rust
async fn mark_quote_failed(rpc: &mut ClnRpc, payment_hash: &str, rec: &QuoteRecord, claimed_generation: u64) {
    let failed = QuoteRecord { state: "failed".into(), ..rec.clone() };
    let _ = rpc.call_typed(&DatastoreRequest {
        key: quote_key(payment_hash), string: Some(serde_json::to_string(&failed).unwrap()),
        mode: Some(DatastoreMode::MUST_REPLACE), generation: Some(claimed_generation), ..
    }).await;
}
```

### 7. Restart idempotency — and why it needs more than just code

A replayed `htlc_accepted` call for an already-`"claimed"` record (the
hook's own docs: lightningd replays HTLCs with no final verdict on
restart) must re-derive the right answer from leg 2's own payment status
rather than guess or re-run it:

```rust
async fn resolve_from_leg2_status(rpc: &mut ClnRpc, rec: &QuoteRecord) -> Value {
    let res = rpc.call_typed(&ListsendpaysRequest { bolt11: Some(rec.final_bolt11.clone()), .. }).await?;
    if let Some(pm) = res.payments.iter().find(|pm| pm.status == COMPLETE) {
        return resolve_with_preimage(pm.payment_preimage);   // leg 2 already paid out
    }
    if let Some(pm) = res.payments.iter().find(|pm| pm.status == PENDING) {
        // Leg 2 was genuinely in flight at restart — the common case right
        // after a crash. Wait on it rather than failing leg 1 prematurely:
        // failing immediately risks leaving the provider out of pocket if
        // leg 2 lands a moment later with nothing to show for it.
        return match rpc.call_typed(&WaitsendpayRequest {
            payment_hash: pm.payment_hash, timeout: Some(30), ..
        }).await {
            Ok(res) => resolve_with_preimage(res.payment_preimage),
            Err(_) => fail_incorrect_details(),   // failed, or timed out waiting
        };
    }
    fail_incorrect_details()   // no leg-2 attempt found at all
}
```

**This alone is not sufficient — a real test against the live Docker
environment caught a second, operational gap.** A plugin started with
`lightning-cli plugin start` is *not* persisted across restarts. Dynamic
plugins are runtime-only; lightningd doesn't write them to any config
file. In a controlled test (hold the leg-2 HTLC at the payee, restart the
provider mid-swap), the provider's own `lightningd` replayed the
still-pending leg-1 HTLC and rejected it with the default "unknown
invoice" error *before the dynamically-started plugin could reload* — the
hook ran with no plugin registered for it at all, so the idempotency code
above never got a chance to run. The result: the provider's own leg-2
payment later completed (the payee got paid), but leg 1 was never
resolved — the provider was left out of pocket with no compensating
mechanism, exactly the failure mode this section exists to prevent.

The fix is operational, not code: **a swap-provider must load this plugin
before lightningd can ever replay a pending HTLC**, never only via
`plugin start`. In `..`, this is now structural rather
than something an operator has to remember: the Dockerfile's
`rust-plugins-builder` stage builds both `assetswap-plugin` and
`pricetiers-plugin` into the `cln` image itself, and
`docker-compose.yml`'s shared entrypoint passes `--plugin=` for each of
them on every node's command line — so both are loaded from each node's
very first startup, the same way for alice, bob, and carol, with nothing
left for `lnpoc.py`'s swap commands to configure beyond the
`pricetiers-pair=`/`assetswap-provider=` lines themselves (see Files,
below). Outside this PoC's Docker setup, the equivalent is a
`plugin=/path/to/assetswap-plugin` and
`plugin=/path/to/pricetiers-plugin` line in a provider's own `config`
file. Verified by repeating the same
hold-and-restart test against the baked-in setup: the replayed hook fires
with the plugin already loaded, `resolve_from_leg2_status` finds leg 2
`pending`, waits on it, and correctly resolves leg 1 once leg 2 lands. A
payer's own instance doesn't strictly need this (it never holds anyone
else's HTLC), but gets it for free here too since every node shares the
same image and entrypoint.

### 8. CLTV margin policy

Reuse this fork's own BOLT2-derived constants (`../../../lightningd/options.c`)
rather than inventing new numbers:

- `SAFETY_BLOCKS` (`--assetswap-cltv-safety-blocks`, default **18** =
  mainnet `cltv_final`): the provider is a terminal node for leg 1, so it
  needs the same worst-case margin BOLT2 gives any terminal node to safely
  redeem or walk away on-chain.
- `MIN_HOP_BUDGET` (**34** = mainnet `cltv_expiry_delta`): a cheap
  up-front sanity gate before even attempting leg 2.
- The precise leg-2 budget (`maxdelay` passed into `xpay`) is computed
  per-HTLC from its actual `cltv_expiry_relative`, letting `xpay`/`askrene`'s
  own multi-hop budget enforcement do the real work rather than hand-summing
  hop deltas.
- `LEG1_CLTV_MARGIN` (payer side, `pay.rs`, **60**): with no quote reply to
  learn the provider's exact `min_leg1_cltv` from anymore (§3), the payer
  just requests a margin generous enough to cover the provider's *default*
  `SAFETY_BLOCKS` + `MIN_HOP_BUDGET` + slack when routing leg 1. Not
  negotiated — a provider configured with a bigger budget just rejects an
  insufficient margin, same failure shape as an insufficient price, and
  the payer retries with more.

## Files

- **New**: `../../../plugins/assetswap-plugin` — a Cargo crate (`Cargo.toml`,
  `src/main.rs` + modules for the invoice wrapper (`assetinvoice.rs`), the
  payer-side RPCs (`pay.rs`), the provider-side `htlc_accepted` hook
  (`htlc.rs`), the shared `payment_metadata`/datastore-record types
  (`protocol.rs`), and small RPC/config helpers), with its own `README.md`
  mirroring this directory's.
- **New**: `../../../plugins/pricetiers-plugin` (Rust crate `cln-pricetiers`,
  binary `pricetiers-plugin`) — the standalone, swap-agnostic pricing
  policy described in §2, with its own `pricetiers` RPC method. Has no
  `cln-rpc` dependency at all (it never calls back into lightningd) and
  no notion of swaps, Liquid assets, or `htlc_accepted`.
- **One-line changes**: add `"plugins/assetswap-plugin"` and
  `"plugins/pricetiers-plugin"` to the root `../../../Cargo.toml`'s `[workspace]
  members` — the same step every sibling plugin crate required, not a
  behavioral change to anything.
- **New**: `tests/test_assetswap.py` (pytest, same harness as
  `../../../tests/test_liquid_asset_gossip.py`/`tests/test_liquid_asset_poc.py`:
  `node_factory`, `fund_asset_channel`, `asset_invoice`,
  `TEST_NETWORK=liquid-regtest` skip-guard, nodes loaded with
  `opts=[{'plugin': os.path.join(os.getcwd(), 'target/debug/assetswap-plugin')}, ...]`
  plus `pricetiers-plugin` and its `--pricetiers-pair=` option — the test
  harness itself stays Python/pytest as this fork already does for its
  Rust-adjacent plugins; only the plugin binaries are Rust).
- **`..` infra** (this PoC's test harness, not core):
  - `../../../Dockerfile`: a `rust-plugins-builder` stage (`rust:1-bookworm`,
    building just `cln-rpc`/`cln-grpc`/`plugins` so unrelated C-side
    changes don't invalidate its cache) building both
    `cln-assetswap`/`cln-pricetiers`, and one `COPY --from=` line per
    binary dropping it into the `cln` stage's default plugins directory.
  - `../docker-compose.yml`: two added `--plugin=...` flags on the shared
    `x-cln` entrypoint, so alice/bob/carol all load both plugins from
    their very first startup — see §7's restart-idempotency requirement.
  - `../lnpoc.py`: `swap-provider`/`swap-payer`/`assetinvoice`/
    `quoteassetinvoice`/`payassetinvoice`/`swap`/`swap-demo` subcommands
    (see its own docstring). `swap-provider`/`swap-payer` only ever need
    to write the `pricetiers-pair=`/`assetswap-provider=` config lines —
    both plugins are already loaded, and (since §3's redesign) no
    `connect()` step is needed either, as there's no longer a custommsg
    round trip requiring one. `swap_quote()` asks a real provider's own
    `quoteassetinvoice` for the `(from_asset, from_amount_msat)` a plain
    payer needs to pass as `payassetinvoice`'s explicit override, since
    only a provider's own local `pricetiers-plugin` has real pricing —
    `cmd_swap`/`cmd_payassetinvoice` and `lqui.py`'s `payinvoice` action
    all use it.
- **Unchanged**: everything under `../../../lightningd`, `gossipd/`, `channeld/`,
  `common/`, `wire/`. The only reuse of existing core features is the
  already-shipped `invoice ... asset=` and `xpay ... asset=` support, plus
  `sendpay`/`xpay`'s existing `payment_metadata` field and
  `htlc_accepted`'s existing `onion.payment_metadata` (§3) — no new core
  capability needed for either. `plugins/currencyrate-plugin`'s
  `currencyconvert` RPC is *not* wired in anywhere yet, despite being
  mentioned as the natural next step for a real price oracle (§2) — still
  not yet implemented.

## Implementation & testing sequence

**This section (and Verification, below) documents the testing pass run
against the *original* `custommsg` quote-protocol implementation,
before §3's redesign.** Steps 1, 2, 4, 5, 8 and 9 exercised mechanics
(invoice wrapper, direct-payment fallback, the hash-linked two-leg
happy/failure paths, restart-replay) that are still structurally the same
after the redesign, just reached without a quote step — they have *not*
been re-run end-to-end against a live Docker environment since (the
redesign is currently verified only by `cargo check`/`cargo clippy`
against the plugin crate, not by a live swap). Steps 3 and 7's
quote-specific checks (quote-tier reply format, `quote_expiry_unix`) no
longer apply at all — there's no quote to check. Re-running the full
sequence against a live stack is the natural next step before trusting
this as more than a design sketch again.

1. **`assetinvoice` only.** ✅ Verified: wrapper round-trips to the right
   `invoice` fields (asset, amount, payment_hash all match the real bolt11).
   Unaffected by the redesign.
2. **Same-asset fallback path.** ✅ Verified: `payassetinvoice` resolves via
   the direct-`xpay` branch when payer and payee share an asset channel;
   channel balances moved by exactly the paid amount on both sides.
   Unaffected by the redesign — still the first thing `payassetinvoice`
   tries, still a verified-unchanged code path (`tests/test_assetswap.py`
   also still covers exactly this).
3. ~~Quote protocol in isolation.~~ **Removed by the redesign** — there's
   no `custommsg` quote request/reply to test anymore (§3).
4. **Single swap-provider happy path.** ✅ Verified *against the original
   design*: leg 1 and leg 2 shared one payment_hash, the preimage only
   became known after leg 2 settled (confirmed via
   `listinvoices`/`listsendpays`), and balances on all three nodes moved
   by exactly the expected amounts including the provider's margin. ⚠️ Not
   yet re-verified against the `payment_metadata`-based redesign.
   - **Bug found and fixed in the original pass**: leg 1 was originally
     routed with a hardcoded `final_cltv`, smaller than the provider's own
     required safety budget — every swap failed. Fixed at the time by
     having the quote reply carry the provider's required `min_leg1_cltv`;
     the redesign replaces that with a generous fixed payer-side margin
     instead (`LEG1_CLTV_MARGIN`, §8) — the same class of bug (insufficient
     CLTV budget) is worth specifically re-checking once re-verified live.
5. **Leg-2 failure / refund path.** ✅ Verified *against the original
   design* (by deleting the payee's invoice between quoting and leg 1,
   forcing `xpay` to fail): leg 1 also failed, no funds moved on either
   leg, and the record correctly reached the terminal `"failed"` state
   (not left stuck in `"claimed"`). The redesign's `mark_quote_failed`
   path is the same code, just reached without a prior `"quoted"` state —
   ⚠️ not yet re-verified live.
6. **Price-tier / live-price edge cases.** ⚠️ Partially covered — the tier
   math itself is verified (exact tier amounts match hand-computed
   values), but a genuine live-price-moved-the-floor scenario isn't
   exercised, since only the "fixed" price source exists so far (see
   Limits). Revisit once a real oracle is wired in. The redesign makes
   this check the *only* price gate (§3), so it's worth more scrutiny
   than before, not less.
7. **Replay edge cases.** ✅ CLTV-too-tight verified *against the original
   design* (undersized `delay` on leg 1; rejected immediately, `xpay`
   never reached, no state mutation) — the equivalent check still exists
   post-redesign, ⚠️ not yet re-verified. ~~Quote-expiry~~ no longer
   applies (no `quote_expiry_unix` anymore). ⚠️ Concurrent double-claim
   still *not* directly forced — `sendpay`'s own dedup-by-payment_hash
   blocks the obvious way to simulate it from a single sender, and MPP
   parts need a `payment_secret` this scenario doesn't have. The
   `datastore` generation CAS itself is still verified correct in every
   test above (exactly one write wins); the redesign's version of it
   additionally needs its `must-create`-vs-`must-replace` branch (§6)
   exercised, which wasn't relevant before (a record always already
   existed by claim time in the original design).
8. **Restart/idempotency.** ✅ Verified *against the original design*
   against a real mid-flight restart (hold leg 2 at the payee with a
   throwaway test plugin, restart the provider's `lightningd` while leg 1
   is `"claimed"` and leg 2 still pending). Two real findings came out of
   this, both documented in §7 above and both structurally unaffected by
   the redesign: (a) a `"claimed"`-but-not-yet-resolved record needs to
   *wait* on leg 2 via `waitsendpay`, not just check once and give up; (b)
   the plugin must be loaded before lightningd can replay a pending HTLC.
   ⚠️ Not yet re-verified against the redesigned code.
9. **(Optional) hand-driven demo** on real `elementsd`-backed
   `liquid-regtest` nodes — effectively what steps 1-8 already were,
   driven by hand via `lightning-cli` rather than `../lnpoc.py` subcommands.

## Verification

- All of the above (pre-redesign) was exercised against a live
  `..` environment (`./lnpoc.py start`), using `lnpoc.py`'s
  own `swap-provider`/`swap-payer`/`assetinvoice`/`payassetinvoice`/
  `swap`/`swap-demo` subcommands — the plugin itself needs no separate
  build or load step (see Files, below); these subcommands only ever
  write the per-pair/per-provider `assetswap-*` config lines.
- The §3 redesign (this plugin now also has a `quoteassetinvoice`
  subcommand/RPC, and `payassetinvoice` takes an optional
  `from_asset`/`from_amount_msat` override — see Files) is currently
  verified only by `cargo check`/`cargo clippy` against the
  `assetswap-plugin` crate, not by a live swap. Re-running
  implementation-sequence steps 2, 4, 5, 7 and 8 against a live
  `..` stack is the natural next step.
- **Still to do**: wire up `tests/test_assetswap.py` so this is automated
  rather than driven via `../lnpoc.py` — it needs this fork's own built
  `lightningd` and the `pyln` packages installed (`contrib/pyln-client`,
  `contrib/pyln-testing`), neither of which is set up in a fresh checkout.
  (It currently only covers steps 1-2, which the redesign didn't touch.)
- Confirm no diffs outside `../../../plugins/assetswap-plugin`, the root
  `Cargo.toml`'s `members` line, `tests/test_assetswap.py`, and
  `contrib/liquid-poc/{Dockerfile,docker-compose.yml,lnpoc.py,lqui.py}` —
  the last four are this PoC's own test harness, not core, so they don't
  count against the zero-core-change goal.

## Limits (carried over / new)

- No onion-message-based provider discovery in v1 — payers need a static
  list of provider node_ids.
- ~~A payer connecting to a provider for a quote reveals intent...~~
  **Resolved by the §3 redesign**: there's no connection or message to the
  provider at all anymore, before or during a swap attempt — leg 1 is
  routed like any other payment, so a provider learns nothing about a
  payer's intent until (if) an HTLC from them actually arrives.
- Only one provider is routed to per payment in v1 — no shopping across
  multiple configured providers for the best price. (A natural v1.1: try
  each configured provider in turn, or pick by some local preference.)
- **The price check isn't backed by a real oracle yet.** Only the "fixed"
  source exists — a pair's `rate_ppm` is a static config value, so the
  `htlc_accepted` hook's price floor (§3 — no longer a "recheck" against
  an earlier quote, just the one live check that exists) is
  permanently satisfied by construction. Wiring up
  `../../../plugins/currencyrate-plugin`'s `currencyconvert` RPC as a genuine
  oracle (an `--assetswap-price-source=oracle` option) is the natural next
  step to make this protection real; until then the tiers only differ in
  margin, not in actual risk covered.
- **A swap-provider must load this plugin before lightningd can replay a
  pending HTLC** — `plugin start`-ing it leaves it unloaded across a
  restart, and a replayed leg-1 HTLC can arrive before a
  dynamically-started plugin reloads, so the idempotency logic in §7
  never gets a chance to run and the provider can be left out of pocket
  (paid leg 2, never reimbursed on leg 1). `..` closes
  this structurally (built into the image, `--plugin=` on every node's
  command line — see Files), but it's still worth stating as a hard
  requirement for any other deployment: a `plugin=` line in `config`, not
  `plugin start`. Not a concern for a payer's own instance, only a
  provider's.
- Concurrent-replay/double-claim protection (the `datastore` generation
  CAS) is verified correct in isolation, but a genuine two-HTLCs-at-once
  race wasn't forced in testing — `sendpay`'s own payment_hash dedup blocks
  the straightforward way to simulate it from a single sender.
- Same caveats as the rest of this PoC: test coins only, no RBF/fee-bumping
  considerations beyond what asset channels already have.
