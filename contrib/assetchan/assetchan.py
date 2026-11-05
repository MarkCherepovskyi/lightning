#!/usr/bin/env python3
"""Asset channels on Liquid: the plugin half (DESIGN.md section 5).

Core knows a channel's asset and builds and signs its transactions; this
plugin does the rest, with an elementsd wallet holding the assets:

- `assetchan-open` funds an asset channel from that wallet: the asset to the
  channel's 2-of-2 at output n, an L-BTC fee reserve to it at output n+1;
- the `openchannel` hook polices incoming offers (allowed assets, fee reserve)
  and, like `assetchan-open` and `assetchan-close`, makes our side of a
  channel close to an address of that wallet, so swept assets are spendable;
- `assetchan-invoice` ties an invoice to an asset, and the `htlc_accepted`
  hook refuses payments that arrive in another one;
- `assetchan-pay` pays over channels in the invoice's asset only: gossip
  doesn't say which asset a channel is in, so the plugin reads it off the
  funding output (`assetchan-listchannels` shows that) and keeps an askrene
  layer per asset.

Amounts are in the asset's base units; on the wire a unit is 1000 msat.
"""
from elements import ElementsError, ElementsRpc, base_units, coins, config_value, rpc_url
from pyln.client import Plugin, RpcError, RpcException

import hashlib
import json

plugin = Plugin()
plugin.elements = None       # ElementsRpc, built on first use
plugin.lbtc = None           # L-BTC's asset id
plugin.invoice_assets = {}   # payment_hash -> asset of that invoice of ours
plugin.gossip_assets = {}    # gossiped scid -> asset of its funding output (None: can't tell)
plugin.layers = {}           # askrene layer we created -> scids disabled in it

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
    with open(path) as f:
        line = f.read().splitlines()[int(linenum) - 1]
    return line.split('=', 1)[1].strip()


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


def gossiped_channel_asset(plugin, scid):
    """The asset a gossiped channel is in: gossip doesn't say, its funding
    output's asset tag does (read from elementsd, cached).  'lbtc' for an
    ordinary channel, None if we can't tell (cached too, when the output
    is gone or has no tag; not when elementsd failed)."""
    if scid in plugin.gossip_assets:
        return plugin.gossip_assets[scid]
    try:
        height, index, outnum = (int(x) for x in scid.split('x'))
        ed = elements(plugin)
        txid = ed.call('getblock', ed.call('getblockhash', height), 1)['tx'][index]
        out = ed.call('gettxout', txid, outnum, False)
    except (ElementsError, IndexError, ValueError, KeyError) as e:
        plugin.log("can't tell the asset of channel {}: {}".format(scid, e), 'info')
        return None
    asset = None
    if out and 'asset' in out:
        asset = LBTC if out['asset'] == lbtc_asset(plugin) else out['asset']
    plugin.gossip_assets[scid] = asset
    return asset


def gossiped_assets(plugin, chans):
    """scid -> asset (None if we can't tell) for listchannels' `chans`,
    forgetting the channels gossip no longer has."""
    scids = {c['short_channel_id'] for c in chans}
    for scid in set(plugin.gossip_assets) - scids:
        del plugin.gossip_assets[scid]
    return {scid: gossiped_channel_asset(plugin, scid) for scid in scids}


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
    """The assets core takes channels in (--channel-asset)."""
    return plugin.rpc.listconfigs()['configs'].get('channel-asset', {}).get('values_str', [])


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


@plugin.method('assetchan-open')
def assetchan_open(plugin, id, amount, asset, fee_reserve=None, announce=False, close_to=None):
    """Open a channel to (connected) peer {id} holding {amount} units of
    {asset}, funded by elementsd's wallet, with {fee_reserve} sat of L-BTC
    next to it for on-chain fees (default assetchan-default-fee-reserve).
    Our side closes to {close_to} (default: a fresh address of that wallet)."""
    check_asset(asset)
    amount = int(amount)
    if fee_reserve is None:
        fee_reserve = plugin.get_option('assetchan-default-fee-reserve')
    fee_reserve = int(fee_reserve)
    if close_to is None and plugin.get_option('assetchan-close-to') == 'elements':
        close_to = new_address(plugin)
    ed = elements(plugin)
    lbtc = lbtc_asset(plugin)

    params = {'id': id, 'amount': amount, 'announce': bool(announce),
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
    """The gossiped channels with the asset each is in ('lbtc' for an
    ordinary channel; from the funding output, as gossip doesn't say),
    only those in {asset} if given."""
    if asset is not None:
        check_asset_or_lbtc(asset)
    out = []
    chans = plugin.rpc.listchannels()['channels']
    assets = gossiped_assets(plugin, chans)
    for c in chans:
        found = assets[c['short_channel_id']]
        if asset is None or found == asset:
            out.append({'short_channel_id': c['short_channel_id'],
                        'direction': c['direction'], 'source': c['source'],
                        'destination': c['destination'],
                        'amount_msat': c['amount_msat'], 'asset': found})
    return {'channels': out}


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
    inv = plugin.rpc.invoice(int(amount) * 1000, label, description, expiry=expiry,
                             exposeprivatechannels=asset_hints(plugin, asset))
    plugin.rpc.datastore(key=INVOICE_KEY + [inv['payment_hash']], string=asset,
                         mode='create-or-replace')
    plugin.invoice_assets[inv['payment_hash']] = asset
    inv['asset'] = asset
    return inv


def asset_layer(plugin, asset):
    """The askrene layer `assetchan.<asset>`: every channel we know to be
    in another asset disabled (ours, and the gossiped ones by their funding
    output), so a payment in `asset` is only routed over channels in it."""
    layer = 'assetchan.' + asset
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

    for c in our_channels(plugin):
        # (what askrene calls the channel: its scid, or its local alias)
        scid = c.get('short_channel_id') or c.get('alias', {}).get('local')
        if scid is not None and c.get('asset', LBTC) != asset:
            disable(scid)
    gossiped = gossiped_assets(plugin, plugin.rpc.listchannels()['channels'])
    for scid, other in gossiped.items():
        if other is not None and other != asset:
            disable(scid)
    return layer


@plugin.method('assetchan-pay')
def assetchan_pay(plugin, bolt11, asset, maxfee=None):
    """Pay {bolt11} in {asset} ('lbtc' for an ordinary payment): xpay, kept
    off every channel known to be in another asset; {maxfee} as xpay's."""
    check_asset_or_lbtc(asset)
    params = {'invstring': bolt11, 'layers': [asset_layer(plugin, asset)]}
    if maxfee is not None:
        params['maxfee'] = maxfee
    res = plugin.rpc.call('xpay', params)
    res['asset'] = asset
    return res


# -------------------------------------------------------------------- hooks
@plugin.hook('openchannel')
def on_openchannel(plugin, openchannel, **kwargs):
    """Take asset-channel offers in an allowed asset with a sane fee reserve,
    closing to elementsd's wallet; leave ordinary offers alone."""
    asset = openchannel.get('asset')
    if asset is None:
        return {'result': 'continue'}
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


@plugin.hook('htlc_accepted')
def on_htlc_accepted(plugin, htlc, onion, **kwargs):
    """Final hop: the HTLC must arrive in the invoice's asset (core says
    which asset channel it came by; an ordinary one, none)."""
    if 'short_channel_id' in onion or 'next_node_id' in onion:
        return {'result': 'continue'}  # a forward: core keeps the asset
    want = plugin.invoice_assets.get(htlc['payment_hash'], LBTC)
    got = htlc.get('asset', LBTC)
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


@plugin.init()
def init(options, configuration, plugin, **kwargs):
    if options['assetchan-close-to'] not in ('elements', 'none'):
        return {'disable': "assetchan-close-to must be 'elements' or 'none'"}
    load_invoice_assets(plugin)
    plugin.log('fee reserve {}..{} sat (default {}), closing to {}'.format(
        options['assetchan-min-fee-reserve'], options['assetchan-max-fee-reserve'],
        options['assetchan-default-fee-reserve'], options['assetchan-close-to']))


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
