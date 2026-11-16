#!/usr/bin/env python3
"""Asset channels on Liquid: the plugin half (DESIGN.md section 5).

Core knows a channel's asset and builds and signs its transactions; this
plugin does the rest, with an elementsd wallet holding the assets:

- it turns asset channels on: core only takes them when a plugin offers
  their feature bit, and the plugin decides which assets (`assetchan-asset`);
- `assetchan-open` funds an asset channel from that wallet: the asset to the
  channel's 2-of-2 at output n, an L-BTC fee reserve to it at output n+1;
- the `openchannel` hook polices incoming offers (allowed assets, fee reserve)
  and, like `assetchan-open` and `assetchan-close`, makes our side of a
  channel close to an address of that wallet, so swept assets are spendable;
- `assetchan-invoice` ties an invoice to an asset, and the `htlc_accepted`
  hook refuses payments that arrive in another one, and forwards that would
  leave in another asset (core refuses those too);
- `assetchan-pay` pays over channels in the invoice's asset only, through an
  askrene layer per asset (`assetchan-layer`): asset channels are never
  announced, so every gossiped channel is an ordinary L-BTC one, and an
  asset payment goes over our own channels and the invoice's route hints;
- after a unilateral close, core sweeps our asset outputs to our close
  address itself, paying the fees with L-BTC from CLN's own wallet (keep
  some there; elementsd's wallet only funds opens).

It must run as an important plugin (`important-plugin=`), and refuses to
start otherwise: without it, a node would take asset-channel offers in any
asset, and payments in the wrong asset.

Amounts are in the asset's base units; on the wire a unit is 1000 msat.
"""
from elements import ElementsError, ElementsRpc, base_units, coins, config_value, rpc_url
from pyln.client import Millisatoshi, Plugin, RpcError, RpcException

import hashlib
import json
import os
import sys
import threading

# Asset channels' feature bit (common/features.h OPT_CHANNEL_ASSET, odd:
# optional).  Plugins can only set feature bits at startup.
CHANNEL_ASSET_FEATURE = 1 << 233
# Asset channels are built on option_anchors_zero_fee_htlc_tx (optional,
# bit 23), which lightningd turns off on Elements: we turn it back on.  (On
# Bitcoin, where it is already on, lightningd refuses to load this plugin.)
ANCHORS_FEATURE = 1 << 23
plugin = Plugin(dynamic=False,
                init_features=CHANNEL_ASSET_FEATURE | ANCHORS_FEATURE,
                node_features=CHANNEL_ASSET_FEATURE | ANCHORS_FEATURE)
plugin.elements = None       # ElementsRpc, built on first use
plugin.lbtc = None           # L-BTC's asset id
plugin.invoice_assets = {}   # payment_hash -> asset of that invoice of ours
plugin.layers = {}           # askrene layer we created -> scids disabled in it
plugin.layers_lock = threading.Lock()

LBTC = 'lbtc'                           # the "asset" of an ordinary channel
INVOICE_KEY = ['assetchan', 'invoice']  # datastore: + [payment_hash] -> asset


def check_asset(asset):
    """An asset id is 32 bytes of hex (as elements-cli prints it)."""
    try:
        ok = len(bytes.fromhex(asset)) == 32
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise RpcException('{!r} is not an asset id (64 hex digits)'.format(asset))


def check_asset_or_lbtc(asset):
    """An asset id, or 'lbtc' for ordinary channels."""
    if asset != LBTC:
        check_asset(asset)


# ---------------------------------------------------------------- elementsd
def rpc_password(cfg):
    """bitcoin-rpcpassword: listconfigs conceals it as "...", but says where
    it was set; read it back from the config file if that's where."""
    opt = cfg.get('bitcoin-rpcpassword', {})
    if opt.get('value_str') not in (None, '...'):
        return opt['value_str']
    path, _, linenum = opt.get('source', '').rpartition(':')
    if not path or not linenum.isdigit():
        return None
    try:
        with open(path) as f:
            line = f.read().splitlines()[int(linenum) - 1]
        key, sep, value = line.partition('=')
    except (OSError, IndexError):
        return None
    if not sep or key.strip() != 'bitcoin-rpcpassword':
        return None
    return value.strip()


def elements(plugin):
    """Our elementsd client, built on first use."""
    if plugin.elements is None:
        url = plugin.get_option('assetchan-elements-rpc')
        if not url:
            cfg = plugin.rpc.listconfigs()['configs']
            user = config_value(cfg, 'bitcoin-rpcuser')
            password = rpc_password(cfg)
            port = config_value(cfg, 'bitcoin-rpcport')
            if not (user and password and port):
                raise RpcException("can't find elementsd's RPC credentials (bitcoin-rpcuser,"
                                   " bitcoin-rpcpassword, bitcoin-rpcport): set"
                                   " assetchan-elements-rpc=http://user:password@host:port")
            url = rpc_url(user, password, config_value(cfg, 'bitcoin-rpcconnect') or '127.0.0.1',
                          port)
        plugin.elements = ElementsRpc(url, plugin.get_option('assetchan-elements-wallet'))
    return plugin.elements


def lbtc_asset(plugin):
    """The asset id of L-BTC, the fee asset, on this chain."""
    if plugin.lbtc is None:
        plugin.lbtc = elements(plugin).call('dumpassetlabels')['bitcoin']
    return plugin.lbtc


def new_address(plugin):
    """A fresh address of elementsd's wallet, unconfidential: channel
    transactions pay explicit outputs, and core only knows those addresses."""
    ed = elements(plugin)
    return ed.call('getaddressinfo', ed.call('getnewaddress'))['unconfidential']


def script_address(ed, scriptpubkey):
    """The (unconfidential) address elementsd gives a script, if any: at the
    top level for a witness script, under 'segwit' for a wrappable one."""
    decoded = ed.call('decodescript', scriptpubkey)
    return decoded.get('segwit', decoded).get('address')


ZERO_BLINDER = '0' * 64


def is_explicit(utxo):
    """Is this listunspent entry unblinded?  (elementsd prints zero blinders.)"""
    return (utxo.get('amountblinder', ZERO_BLINDER) == ZERO_BLINDER
            and utxo.get('assetblinder', ZERO_BLINDER) == ZERO_BLINDER)


def explicit_utxo(plugin, asset, amount):
    """An unblinded UTXO of elementsd's wallet holding at least `amount` units
    of `asset`, as {'txid', 'vout', 'units'}.  A channel's funding transaction
    is explicit throughout, and a transaction with blinded inputs can't be:
    if the wallet has no such UTXO, it pays itself one first."""
    ed = elements(plugin)
    for u in ed.call('listunspent', 0):
        if u['asset'] == asset and is_explicit(u) and base_units(u['amount']) >= amount:
            return {'txid': u['txid'], 'vout': u['vout'], 'units': base_units(u['amount'])}
    addr = new_address(plugin)
    spk = ed.call('getaddressinfo', addr)['scriptPubKey']
    # sendtoaddress address amount comment comment_to subtractfeefromamount
    #               replaceable conf_target estimate_mode avoid_reuse assetlabel
    txid = ed.call('sendtoaddress', addr, coins(amount), '', '', False, False, None, None, None,
                   asset)
    tx = ed.call('decoderawtransaction', ed.call('gettransaction', txid)['hex'])
    for v in tx['vout']:
        if v['scriptPubKey'].get('hex') == spk and v.get('asset') == asset and 'value' in v:
            return {'txid': txid, 'vout': v['n'], 'units': base_units(v['value'])}
    raise RpcException('elementsd paid {} of {} to {} without an explicit output ({})'
                       .format(amount, asset, addr, txid))


def funding_fee(ed, n_inputs, n_outputs):
    """A fee (sat) for an explicit funding transaction of that shape: twice
    what relay demands, and at least 1000 sat."""
    info = ed.call('getmempoolinfo')
    per_kvb = max(base_units(info.get('minrelaytxfee', 0)),
                  base_units(info.get('mempoolminfee', 0)))
    vbytes = 12 + 70 * n_inputs + 45 * n_outputs
    return max(1000, -(-2 * vbytes * per_kvb // 1000))


def pays(out, scriptpubkey, asset, units):
    """Is this decoded output an explicit `units` of `asset` to scriptpubkey?"""
    return (out['scriptPubKey'].get('hex') == scriptpubkey and out.get('asset') == asset
            and 'value' in out and base_units(out['value']) == units)


def funding_outnum(tx, scriptpubkey, asset, amount, lbtc, fee_reserve):
    """Where the decoded funding tx pays `amount` of `asset` to the channel,
    with `fee_reserve` of L-BTC to it right after."""
    outs = tx['vout']
    for n in range(len(outs) - 1):
        if (pays(outs[n], scriptpubkey, asset, amount)
                and pays(outs[n + 1], scriptpubkey, lbtc, fee_reserve)):
            return n
    raise RpcException('elementsd built a funding transaction without the channel outputs: {}'
                       .format(json.dumps(outs, default=str)))


# ----------------------------------------------------------------- channels
def our_channels(plugin):
    return plugin.rpc.listpeerchannels()['channels']


def find_channel(plugin, id):
    """The one channel `id` names (channel id, scid, alias, or a peer with a
    single channel), or None."""
    found = [c for c in our_channels(plugin)
             if id in [c['channel_id'], c['peer_id'], c.get('short_channel_id')]
             + list(c.get('alias', {}).values())]
    return found[0] if len(found) == 1 else None


def channel_summary(c):
    """A channel as assetchan-list and assetchan-funds show it: amounts in units."""
    s = {k: c[k] for k in ('peer_id', 'channel_id', 'short_channel_id', 'state',
                           'opener', 'close_to_addr') if k in c}
    s['asset'] = c.get('asset', LBTC)
    if 'fee_reserve_msat' in c:
        s['fee_reserve'] = int(c['fee_reserve_msat']) // 1000
    s['to_us'] = int(c.get('to_us_msat', 0)) // 1000
    s['total'] = int(c.get('total_msat', 0)) // 1000
    return s


def load_invoice_assets(plugin):
    """The assets assetchan-invoice noted, by payment hash, from the datastore."""
    plugin.invoice_assets = {e['key'][-1]: e['string']
                             for e in plugin.rpc.listdatastore(key=INVOICE_KEY)['datastore']
                             if len(e['key']) == len(INVOICE_KEY) + 1 and 'string' in e}


def allowed_assets(plugin):
    """The assets we open and accept channels in (assetchan-asset)."""
    return plugin.get_option('assetchan-asset') or []


def channel_asset(plugin, scid, channels=None):
    """The asset of our channel with this scid (or local alias), or None if
    we have no such channel: callers must then refuse, not guess."""
    for c in channels if channels is not None else our_channels(plugin):
        if scid in (c.get('short_channel_id'), c.get('alias', {}).get('local')):
            return c.get('asset', LBTC)
    return None


def in_background(request, fn, *args, **kwargs):
    """Run a method that waits on elementsd or on a payment in a thread of
    its own, so our hooks (htlc_accepted!) aren't held up meanwhile."""
    def run():
        try:
            request.set_result(fn(*args, **kwargs))
        except Exception as e:
            request.set_exception(e)
    threading.Thread(target=run, name=request.method, daemon=True).start()


# ------------------------------------------------------------------ methods
def cancel_funding(plugin, peer_id, inputs):
    """Undo a failed open: tell core, and give elementsd its inputs back."""
    try:
        plugin.rpc.fundchannel_cancel(peer_id)
    except RpcError as e:
        plugin.log('fundchannel_cancel: {}'.format(e), level='warn')
    if inputs:
        try:
            elements(plugin).call('lockunspent', True, inputs)
        except ElementsError as e:
            plugin.log(str(e), level='warn')


@plugin.async_method('assetchan-open')
def assetchan_open(plugin, request, id, amount, asset, fee_reserve=None, announce=False,
                   close_to=None):
    """Open a channel to (connected) peer {id} holding {amount} units of
    {asset}, funded by elementsd's wallet, with {fee_reserve} sat of L-BTC
    next to it for on-chain fees (default assetchan-default-fee-reserve).
    Our side closes to {close_to} (default: a fresh address of that wallet).
    Asset channels are private: gossip can't say which asset a channel is
    in, so {announce} must stay false."""
    in_background(request, open_channel, plugin, id, amount, asset, fee_reserve, announce,
                  close_to)


def open_channel(plugin, id, amount, asset, fee_reserve, announce, close_to):
    check_asset(asset)
    if announce:
        raise RpcException('asset channels are private (announce=false): gossip'
                           " can't say which asset a channel is in")
    amount = int(amount)
    if fee_reserve is None:
        fee_reserve = plugin.get_option('assetchan-default-fee-reserve')
    fee_reserve = int(fee_reserve)
    if close_to is None and plugin.get_option('assetchan-close-to') == 'elements':
        close_to = new_address(plugin)
    ed = elements(plugin)
    lbtc = lbtc_asset(plugin)

    params = {'id': id, 'amount': amount, 'announce': False,
              'asset': asset, 'fee_reserve': fee_reserve}
    if close_to:
        params['close_to'] = close_to
    start = plugin.rpc.call('fundchannel_start', params)
    script = start['scriptpubkey']
    inputs = []
    try:
        addr = script_address(ed, script) or start['funding_address']
        # The funding transaction, explicit throughout (so nothing is
        # blinded, and core can check it): the asset to the channel at
        # output 0, the L-BTC fee reserve to it at output 1, then the change
        # and the fee.
        fee = funding_fee(ed, 2, 5)
        x_in = explicit_utxo(plugin, asset, amount)
        l_in = explicit_utxo(plugin, lbtc, fee_reserve + fee)
        inputs = [{'txid': u['txid'], 'vout': u['vout']} for u in (x_in, l_in)]
        ed.call('lockunspent', False, inputs)
        outputs = [{addr: coins(amount), 'asset': asset}, {addr: coins(fee_reserve)}]
        if x_in['units'] > amount:
            outputs.append({new_address(plugin): coins(x_in['units'] - amount), 'asset': asset})
        l_change = l_in['units'] - fee_reserve - fee
        if l_change < 1000:   # not worth an output: into the fee
            fee, l_change = fee + l_change, 0
        if l_change:
            outputs.append({new_address(plugin): coins(l_change)})
        outputs.append({'fee': coins(fee)})
        signed = ed.call('walletprocesspsbt', ed.call('createpsbt', inputs, outputs))
        if not signed.get('complete'):
            raise ElementsError("elementsd couldn't sign the funding transaction")
        final = ed.call('finalizepsbt', signed['psbt'])
        tx = ed.call('decoderawtransaction', final['hex'])
        outnum = funding_outnum(tx, script, asset, amount, lbtc, fee_reserve)
        done = plugin.rpc.fundchannel_complete(id, signed['psbt'], withhold=False)
        txid = ed.call('sendrawtransaction', final['hex'])
    except Exception:
        cancel_funding(plugin, id, inputs)
        raise
    plugin.log('opened channel {} to {}: {} units of {} + {} sat fee reserve, funding {}:{}'
               .format(done['channel_id'], id, amount, asset, fee_reserve, txid, outnum))
    return {'txid': txid, 'channel_id': done['channel_id'], 'outnum': outnum,
            'asset': asset, 'fee_reserve': fee_reserve}


@plugin.method('assetchan-close')
def assetchan_close(plugin, id, destination=None):
    """Mutually close channel {id} (a channel id, short channel id or peer
    id).  Our asset goes to {destination}: by default the close_to address
    set at open, else a fresh address of elementsd's wallet."""
    if destination is None and plugin.get_option('assetchan-close-to') == 'elements':
        c = find_channel(plugin, id)
        if c is not None and 'asset' in c and 'close_to' not in c:
            destination = new_address(plugin)
    return plugin.rpc.close(id, destination=destination)


@plugin.method('assetchan-list')
def assetchan_list(plugin, asset=None):
    """Our asset channels (those in {asset}, if given)."""
    return {'channels': [channel_summary(c) for c in our_channels(plugin)
                         if 'asset' in c and asset in (None, c['asset'])]}


@plugin.method('assetchan-funds')
def assetchan_funds(plugin, channels=True):
    """What elementsd's wallet holds (by asset id, or label) and, unless
    {channels} is false, what our channels hold, in base units."""
    balances = elements(plugin).call('getbalances')['mine']['trusted']
    res = {'elements': {name: base_units(amount) for name, amount in balances.items()}}
    if channels:
        res['channels'] = [channel_summary(c) for c in our_channels(plugin)]
    return res


@plugin.method('assetchan-send')
def assetchan_send(plugin, addr, asset, amount):
    """Send {amount} units of {asset} (an asset id, or an elementsd label
    such as "bitcoin") from elementsd's wallet to {addr}."""
    # sendtoaddress address amount comment comment_to subtractfeefromamount
    #               replaceable conf_target estimate_mode avoid_reuse assetlabel
    txid = elements(plugin).call('sendtoaddress', addr, coins(amount), '', '',
                                 False, False, None, None, None, asset)
    return {'txid': txid}


@plugin.method('assetchan-listchannels')
def assetchan_listchannels(plugin, asset=None):
    """The gossiped channels with the asset each is in, only those in
    {asset} if given.  Asset channels are never announced, and core only
    takes announcements whose funding output is L-BTC, so every gossiped
    channel is an ordinary one ('lbtc')."""
    if asset is not None:
        check_asset_or_lbtc(asset)
    if asset not in (None, LBTC):
        return {'channels': []}
    return {'channels': [{'short_channel_id': c['short_channel_id'],
                          'direction': c['direction'], 'source': c['source'],
                          'destination': c['destination'],
                          'amount_msat': c['amount_msat'], 'asset': LBTC}
                         for c in plugin.rpc.listchannels()['channels']]}


@plugin.method('assetchan-newaddr')
def assetchan_newaddr(plugin):
    """A fresh unconfidential address of elementsd's wallet."""
    return {'address': new_address(plugin)}


def asset_hints(plugin, asset):
    """Our channels in `asset` that can receive right now (those listincoming
    lists: peer connected, its channel_update received), as invoice route
    hints.  Explicit hints: core would otherwise pick channels in any asset,
    and none at all through a peer without public channels."""
    ours = set()
    for c in our_channels(plugin):
        if c.get('asset', LBTC) != asset or c.get('state') != 'CHANNELD_NORMAL':
            continue
        ours.update(scid for scid in (c.get('short_channel_id'),
                                      c.get('alias', {}).get('local'),
                                      c.get('alias', {}).get('remote')) if scid)
    hints = [i['short_channel_id'] for i in plugin.rpc.listincoming()['incoming']
             if i['short_channel_id'] in ours]
    if not hints:
        raise RpcException('No channel in {} can receive yet (peer offline, or its '
                           'channel_update not received)'.format(asset))
    return hints


@plugin.method('assetchan-invoice')
def assetchan_invoice(plugin, amount, asset, label, description, expiry=None):
    """An invoice for {amount} units of {asset} ('lbtc' for an ordinary
    one): it must be paid over a channel in that asset, and it hints at our
    channels in it."""
    check_asset_or_lbtc(asset)
    hints = asset_hints(plugin, asset)
    # The asset is noted before the invoice exists, so an invoice of ours
    # can never be paid while we'd take it for an L-BTC one (a failed
    # datastore write after the invoice would have left exactly that).
    preimage = os.urandom(32)
    payment_hash = hashlib.sha256(preimage).hexdigest()
    plugin.rpc.datastore(key=INVOICE_KEY + [payment_hash], string=asset,
                         mode='must-create')
    plugin.invoice_assets[payment_hash] = asset
    try:
        inv = plugin.rpc.invoice(int(amount) * 1000, label, description, expiry=expiry,
                                 preimage=preimage.hex(), exposeprivatechannels=hints)
    except Exception:
        plugin.invoice_assets.pop(payment_hash, None)
        plugin.rpc.deldatastore(key=INVOICE_KEY + [payment_hash])
        raise
    inv['asset'] = asset
    return inv


def asset_layer(plugin, asset):
    """The askrene layer `assetchan.<asset>`: every channel known to be in
    another asset disabled, so a payment in `asset` is only routed over
    channels in it.  Those are our channels in other assets (by scid and
    local alias) and, for an asset other than L-BTC, every gossiped
    channel: asset channels are never announced, so gossip is all L-BTC.
    (The invoice's route hints, which askrene adds, stay usable.)"""
    layer = 'assetchan.' + asset
    with plugin.layers_lock:
        disabled = plugin.layers.get(layer)
        if disabled is None:
            layers = plugin.rpc.call('askrene-listlayers', {})['layers']
            if layer not in [lay['layer'] for lay in layers]:
                plugin.rpc.call('askrene-create-layer', {'layer': layer, 'persistent': False})
            disabled = plugin.layers[layer] = set()

        def disable(scid):
            if scid in disabled:
                return
            for direction in (0, 1):
                plugin.rpc.call('askrene-update-channel',
                                {'layer': layer, 'enabled': False,
                                 'short_channel_id_dir': '{}/{}'.format(scid, direction)})
            disabled.add(scid)

        ours = set()
        for c in our_channels(plugin):
            names = [n for n in (c.get('short_channel_id'), c.get('alias', {}).get('local'))
                     if n is not None]
            ours.update(names)
            if c.get('asset', LBTC) != asset:
                for scid in names:
                    disable(scid)
        if asset != LBTC:
            for c in plugin.rpc.listchannels()['channels']:
                if c['short_channel_id'] not in ours:
                    disable(c['short_channel_id'])
    return layer


@plugin.method('assetchan-layer')
def assetchan_layer(plugin, asset):
    """The askrene layer (brought up to date) that keeps a payment in
    {asset} ('lbtc' for an ordinary payment) on channels in that asset."""
    check_asset_or_lbtc(asset)
    return {'layer': asset_layer(plugin, asset), 'asset': asset}


@plugin.async_method('assetchan-pay')
def assetchan_pay(plugin, request, bolt11, asset, maxfee=None):
    """Pay {bolt11} in {asset} ('lbtc' for an ordinary payment): xpay, kept
    off every channel known to be in another asset; {maxfee} as xpay's."""
    check_asset_or_lbtc(asset)
    # bolt11 only: assetchan-invoice makes those, and a BOLT12 invoice's
    # blinded path could name our first channel itself (core refuses one
    # starting at us over an asset channel; this says why up front).
    if plugin.rpc.decode(bolt11).get('type') != 'bolt11 invoice':
        raise RpcException('assetchan-pay takes a bolt11 invoice')
    in_background(request, pay, plugin, bolt11, asset, maxfee)


def pay(plugin, bolt11, asset, maxfee):
    params = {'invstring': bolt11, 'layers': [asset_layer(plugin, asset)]}
    if maxfee is not None:
        params['maxfee'] = maxfee
    res = plugin.rpc.call('xpay', params)
    res['asset'] = asset
    return res


# -------------------------------------------------------------------- hooks
# Every payment command that routes for itself, with its params in order
# (for positional calls).  Those taking askrene layers get ours; the others
# are rewritten (pay, keysend) or given exclusions (renepay).  A raw
# sendpay/sendonion names its own first hop, and is the caller's choice.
XPAY_PARAMS = ['invstring', 'amount_msat', 'maxfee', 'layers', 'retry_for', 'partial_msat',
               'maxdelay', 'payer_note', 'label', 'localinvreqid']
LAYERED = {
    'xpay': XPAY_PARAMS,
    'xpay-as-pay': XPAY_PARAMS,
    'sendamount': ['invstring', 'amount_msat', 'maxfee', 'layers', 'retry_for', 'maxdelay',
                   'payer_note', 'label'],
    'xkeysend': ['destination', 'amount_msat', 'label', 'maxfee', 'layers', 'retry_for',
                 'maxdelay', 'extratlvs'],
}
PAY_PARAMS = ['bolt11', 'amount_msat', 'label', 'riskfactor', 'maxfeepercent', 'retry_for',
              'maxdelay', 'exemptfee', 'localinvreqid', 'exclude', 'maxfee', 'description',
              'partial_msat']
KEYSEND_PARAMS = ['destination', 'amount_msat', 'label', 'maxfeepercent', 'retry_for', 'maxdelay',
                  'exemptfee', 'extratlvs', 'routehints', 'maxfee']
RENEPAY_PARAMS = ['invstring', 'amount_msat', 'maxfee', 'maxdelay', 'retry_for', 'description',
                  'label', 'exclude']
PARAMS = {**LAYERED, 'pay': PAY_PARAMS, 'keysend': KEYSEND_PARAMS, 'renepay': RENEPAY_PARAMS}
# pay's params as xpay-as-pay's (what xpay does when it takes pay over);
# riskfactor and description it ignores too.
PAY_AS_XPAY = {'bolt11': 'invstring', 'invstring': 'invstring', 'amount_msat': 'amount_msat',
               'label': 'label', 'retry_for': 'retry_for', 'maxdelay': 'maxdelay',
               'localinvreqid': 'localinvreqid', 'maxfee': 'maxfee', 'partial_msat': 'partial_msat'}
KEYSEND_AS_XKEYSEND = {k: k for k in ('destination', 'amount_msat', 'label', 'retry_for',
                                      'maxdelay', 'extratlvs', 'maxfee')}


def fee_as_maxfee(plugin, params, out, amount):
    """pay's and keysend's maxfeepercent/exemptfee (defaults 0.5% and
    5000 msat) as a maxfee, as xpay does when it takes pay over."""
    if 'maxfeepercent' in params or 'exemptfee' in params:
        if 'maxfee' in params:
            raise RpcException('maxfee and maxfeepercent/exemptfee: use one or the other')
        amount = int(Millisatoshi(amount))
        pct = float(params.get('maxfeepercent', 0.5))
        exempt = int(Millisatoshi(params.get('exemptfee', 5000)))
        out['maxfee'] = max(exempt, int(amount * pct / 100))
    return out


def pay_as_xpay(plugin, params):
    """pay's params as xpay-as-pay's."""
    if params.get('exclude'):
        raise RpcException("pay's exclude is not supported with asset channels: use xpay's layers")
    out = {PAY_AS_XPAY[k]: v for k, v in params.items() if k in PAY_AS_XPAY}
    amount = params.get('amount_msat')
    if amount is None and ('maxfeepercent' in params or 'exemptfee' in params):
        amount = plugin.rpc.decode(out['invstring'])['amount_msat']
    return fee_as_maxfee(plugin, params, out, amount)


def keysend_as_xkeysend(plugin, params):
    """keysend's params as xkeysend's (which takes layers)."""
    if params.get('routehints'):
        raise RpcException("keysend's routehints are not supported with asset channels: use xkeysend")
    out = {KEYSEND_AS_XKEYSEND[k]: v for k, v in params.items() if k in KEYSEND_AS_XKEYSEND}
    return fee_as_maxfee(plugin, params, out, params['amount_msat'])


def asset_exclusions(plugin):
    """Both directions of each of our asset channels (scid and local
    alias), as renepay's `exclude` takes them."""
    out = []
    for c in our_channels(plugin):
        if c.get('asset', LBTC) == LBTC:
            continue
        for scid in (c.get('short_channel_id'), c.get('alias', {}).get('local')):
            if scid is not None:
                out += ['{}/0'.format(scid), '{}/1'.format(scid)]
    return out


def named(params, names):
    """Positional params as named ones.  More than we know the names of
    (a newer version's, or a dev_ one) is refused: we can't tell what
    they do to the route."""
    if isinstance(params, dict):
        return dict(params)
    if len(params) > len(names):
        raise RpcException('{} positional parameters: pass them by name'.format(len(params)))
    return {n: v for n, v in zip(names, params) if v is not None}


# Before xpay, which takes `pay` (and `keysend`) over by replacing it with
# an xpay command that no other rpc_command hook sees: ours wins.
@plugin.hook('rpc_command', filters=list(PARAMS), before=['cln-xpay'])
def on_rpc_command(plugin, rpc_command, **kwargs):
    """A plain payment is an L-BTC payment: keep it off our asset channels,
    which would otherwise carry N units of their asset for an invoice of N
    sat, 1:1 (to a payee with an asset channel to us, say).  A command that
    names an assetchan layer (assetchan-pay, payassetinvoice) already says
    which asset it pays in, and is left alone.  `pay` and `keysend` become
    xpay's `xpay-as-pay` and `xkeysend` with the L-BTC layer (what xpay does
    with them anyway, but its own takeover would skip every later hook);
    renepay gets our asset channels excluded.  Anything we can't vouch for
    is refused: fail closed."""
    method = rpc_command.get('method')
    try:
        params = named(rpc_command.get('params') or {}, PARAMS[method])
        if method in LAYERED:
            layers = list(params.get('layers') or [])
            if any(lay.startswith('assetchan.') for lay in layers):
                return {'result': 'continue'}
            params['layers'] = layers + [asset_layer(plugin, LBTC)]
        elif method == 'pay':
            params = pay_as_xpay(plugin, params)
            params['layers'] = [asset_layer(plugin, LBTC)]
            rpc_command = {**rpc_command, 'method': 'xpay-as-pay'}
        elif method == 'keysend':
            params = keysend_as_xkeysend(plugin, params)
            params['layers'] = [asset_layer(plugin, LBTC)]
            rpc_command = {**rpc_command, 'method': 'xkeysend'}
        else:   # renepay
            params['exclude'] = list(params.get('exclude') or []) + asset_exclusions(plugin)
    except Exception as e:
        return {'return': {'error': {'code': -32600,
                                     'message': 'assetchan: cannot keep this payment off asset channels: '
                                                '{}'.format(e)}}}
    return {'replace': {**rpc_command, 'jsonrpc': '2.0', 'params': params}}


@plugin.hook('openchannel')
def on_openchannel(plugin, openchannel, **kwargs):
    """Take asset-channel offers in an allowed asset with a sane fee reserve,
    closing to elementsd's wallet; leave ordinary offers alone."""
    asset = openchannel.get('asset')
    if asset is None:
        return {'result': 'continue'}
    if int(openchannel.get('channel_flags', 0)) & 1:
        return {'result': 'reject',
                'error_message': "asset channels are private: gossip can't say"
                                 ' which asset a channel is in'}
    if asset not in allowed_assets(plugin):
        return {'result': 'reject',
                'error_message': 'asset {} is not one we take channels in'.format(asset)}
    reserve = int(openchannel.get('fee_reserve_msat', 0)) // 1000
    lo = plugin.get_option('assetchan-min-fee-reserve')
    hi = plugin.get_option('assetchan-max-fee-reserve')
    if not lo <= reserve <= hi:
        return {'result': 'reject',
                'error_message': 'fee reserve {} sat not in {}..{}'.format(reserve, lo, hi)}
    res = {'result': 'continue'}
    if plugin.get_option('assetchan-close-to') == 'elements':
        res['close_to'] = new_address(plugin)
    plugin.log('accepting a channel from {} in asset {} ({} sat fee reserve)'
               .format(openchannel['id'], asset, reserve))
    return res


def forward_channel(channels, cid, asset, amount_msat):
    """Where to forward an HTLC that arrived in asset: core's choice (the
    channel cid), if it is in that asset; else the channel to the same peer
    in that asset, at the same fees, that can send the most; None if there
    is none."""
    chosen = next((c for c in channels if c.get('channel_id') == cid), None)
    if chosen is None or chosen.get('asset', LBTC) == asset:
        return cid
    fees = ('fee_base_msat', 'fee_proportional_millionths')
    same = [c for c in channels
            if c['peer_id'] == chosen['peer_id']
            and c.get('asset', LBTC) == asset
            and c['state'] == 'CHANNELD_NORMAL'
            and all(c.get(f) == chosen.get(f) for f in fees)
            and c.get('spendable_msat', 0) >= amount_msat]
    if not same:
        return None
    return max(same, key=lambda c: c['spendable_msat'])['channel_id']


# After keysend's hook, which makes the invoice a keysend payment pays: we
# then see that invoice (an ordinary one) like any other.
@plugin.hook('htlc_accepted', after=['keysend'])
def on_htlc_accepted(plugin, htlc, onion, forward_to=None, **kwargs):
    """A forward must leave in the asset it arrived in (amounts are in each
    channel's asset: another one would swap them 1:1).  At the final hop
    an HTLC paying an invoice of ours must arrive in the invoice's asset,
    that is by a channel in that asset.  (Should this raise, pyln fails
    the HTLC.)"""
    if 'short_channel_id' in onion or 'next_node_id' in onion:
        if forward_to is None:
            return {'result': 'continue'}  # core fails it: no such channel
        channels = our_channels(plugin)
        asset = channel_asset(plugin, htlc['short_channel_id'], channels)
        if asset is None:
            plugin.log('Refusing to forward {}: no channel {} of ours'
                       .format(htlc['payment_hash'], htlc['short_channel_id']), level='unusual')
            return {'result': 'fail', 'failure_message': '400a'}  # unknown_next_peer
        cid = forward_channel(channels, forward_to, asset, int(onion['forward_msat']))
        if cid == forward_to:
            return {'result': 'continue'}
        if cid is not None:
            return {'result': 'continue', 'forward_to': cid}
        plugin.log('Refusing to forward {} from {} to channel {}: not in {}'
                   .format(htlc['payment_hash'], htlc['short_channel_id'], forward_to, asset))
        return {'result': 'fail', 'failure_message': '400a'}  # unknown_next_peer
    want = plugin.invoice_assets.get(htlc['payment_hash'])
    if want is None:
        if not plugin.rpc.listinvoices(payment_hash=htlc['payment_hash'])['invoices']:
            # No invoice of ours: core fails it, unless another plugin
            # takes it (a swap provider's leg 1, say).
            return {'result': 'continue'}
        want = LBTC   # an ordinary invoice
    got = channel_asset(plugin, htlc['short_channel_id'])
    if got == want:
        return {'result': 'continue'}
    plugin.log('HTLC for {} arrived in {} but the invoice is in {}: failing it'
               .format(htlc['payment_hash'], got, want))
    # BOLT 4 temporary_channel_failure, without a channel_update: the
    # channel it came by can't carry this invoice, and payers route around
    # that channel (incorrect_or_unknown_payment_details would make them
    # give up on the invoice).
    return {'result': 'fail', 'failure_message': '10070000'}


# ------------------------------------------------------------ notifications
@plugin.subscribe('invoice_payment')
def on_invoice_payment(plugin, invoice_payment, **kwargs):
    """A paid invoice no longer needs its asset noted."""
    payment_hash = hashlib.sha256(bytes.fromhex(invoice_payment['preimage'])).hexdigest()
    if plugin.invoice_assets.pop(payment_hash, None) is None:
        return  # an ordinary invoice
    try:
        plugin.rpc.deldatastore(key=INVOICE_KEY + [payment_hash])
    except RpcError as e:
        plugin.log('deldatastore: {}'.format(e), level='warn')


def is_important(plugin):
    """Were we started with important-plugin=?  Then lightningd stops if we
    die: it never runs asset channels without us (core's guard only checks
    our feature bit, which outlives us)."""
    opt = plugin.rpc.listconfigs('important-plugin')['configs'].get('important-plugin', {})
    paths = opt.get('values_str', [])
    if 'value_str' in opt:
        paths = paths + [opt['value_str']]
    me = os.path.realpath(sys.argv[0])
    return any(os.path.realpath(p) == me for p in paths)


@plugin.init()
def init(options, configuration, plugin, **kwargs):
    if not configuration['network'].startswith('liquid'):
        return {'disable': 'asset channels only exist on Liquid'}
    # Disabled, we offer no feature bit, and core starts no asset channel.
    if not is_important(plugin):
        return {'disable': 'must run as important-plugin= (asset channels are'
                           ' unsafe if it stops: unchecked offers and payments)'}
    if not allowed_assets(plugin):
        return {'disable': 'no assetchan-asset configured'}
    try:
        for a in allowed_assets(plugin):
            check_asset(a)
    except RpcException as e:
        return {'disable': 'assetchan-asset: {}'.format(e)}
    if options['assetchan-close-to'] not in ('elements', 'none'):
        return {'disable': "assetchan-close-to must be 'elements' or 'none'"}
    load_invoice_assets(plugin)
    plugin.log('fee reserve {}..{} sat (default {}), closing to {}'.format(
        options['assetchan-min-fee-reserve'], options['assetchan-max-fee-reserve'],
        options['assetchan-default-fee-reserve'], options['assetchan-close-to']))


plugin.add_option('assetchan-asset', None,
                  'Liquid asset id we open and accept asset channels in',
                  multi=True)
plugin.add_option('assetchan-min-fee-reserve', 1000,
                  'Smallest L-BTC fee reserve (sat) we accept an asset channel with',
                  opt_type='int')
plugin.add_option('assetchan-max-fee-reserve', 10000,
                  'Largest L-BTC fee reserve (sat) we accept an asset channel with',
                  opt_type='int')
plugin.add_option('assetchan-default-fee-reserve', 2000,
                  'L-BTC fee reserve (sat) assetchan-open puts up by default',
                  opt_type='int')
plugin.add_option('assetchan-close-to', 'elements',
                  "Where our asset goes when a channel closes: a fresh address of"
                  " elementsd's wallet (elements), or whatever core picks (none)")
plugin.add_option('assetchan-elements-rpc', None,
                  "elementsd's JSON-RPC as http://user:password@host:port"
                  ' (default: the bitcoin-rpcuser/-rpcpassword/-rpcconnect/-rpcport settings)')
plugin.add_option('assetchan-elements-wallet', '',
                  "elementsd wallet to use (default: elementsd's default wallet)")
plugin.run()
