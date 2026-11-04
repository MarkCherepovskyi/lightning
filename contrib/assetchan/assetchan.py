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
- `assetchan-pay` pays over channels in the invoice's asset only.

Amounts are in the asset's base units; on the wire a unit is 1000 msat.
"""
from decimal import Decimal
from pyln.client import Plugin, RpcError, RpcException

import base64
import hashlib
import json
import struct
import urllib.error
import urllib.parse
import urllib.request

plugin = Plugin()
plugin.elements = None       # ElementsRpc, built on first use
plugin.lbtc = None           # L-BTC's asset id
plugin.channel_assets = {}   # scid or alias -> asset of that channel of ours

COIN = 10**8
LBTC = 'lbtc'                           # the "asset" of an ordinary channel
INVOICE_KEY = ['assetchan', 'invoice']  # datastore: + [payment_hash] -> asset


class ElementsError(RpcException):
    """An elementsd RPC call failed."""
    def __init__(self, message):
        super().__init__(message, code=-1)


class ElementsRpc:
    """elementsd's JSON-RPC, with the standard library only (positional params)."""

    def __init__(self, url, user, password, wallet=''):
        self.url = url.rstrip('/')
        if wallet:
            self.url += '/wallet/' + urllib.parse.quote(wallet, safe='')
        auth = base64.b64encode('{}:{}'.format(user, password).encode()).decode()
        self.headers = {'Content-Type': 'application/json',
                        'Authorization': 'Basic ' + auth}
        self.next_id = 0

    def call(self, method, *params):
        self.next_id += 1
        req = {'jsonrpc': '1.0', 'id': self.next_id, 'method': method,
               'params': list(params)}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, json.dumps(req).encode(),
                                                               self.headers), timeout=120) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            # An RPC error is an HTTP 500 with the usual JSON body.
            body = e.read()
        except OSError as e:
            raise ElementsError('elementsd {}: {}'.format(method, e))
        try:
            resp = json.loads(body.decode(), parse_float=Decimal)
        except ValueError:
            raise ElementsError('elementsd {}: unexpected reply {!r}'.format(method, body[:200]))
        err = resp.get('error')
        if err:
            if isinstance(err, dict):
                err = err.get('message', err)
            raise ElementsError('elementsd {}: {}'.format(method, err))
        return resp.get('result')


def coins(units):
    """Base units -> the decimal amount string elementsd takes."""
    return '{}.{:08d}'.format(*divmod(int(units), COIN))


def base_units(amount):
    """elementsd's decimal amount (a number or a string) -> base units."""
    return int(Decimal(str(amount)) * COIN)


def check_asset(asset):
    """An asset id is 32 bytes of hex (as elements-cli prints it)."""
    try:
        ok = len(bytes.fromhex(asset)) == 32
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise RpcException('{!r} is not an asset id (64 hex digits)'.format(asset))


# ---------------------------------------------------------------- elementsd
def config_value(cfg, name):
    """A lightningd option's value from listconfigs' configs, or None."""
    opt = cfg.get(name, {})
    return opt.get('value_str', opt.get('value_int'))


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
        if url:
            u = urllib.parse.urlsplit(url)
            user = urllib.parse.unquote(u.username or '')
            password = urllib.parse.unquote(u.password or '')
            base = '{}://{}{}'.format(u.scheme, u.netloc.rpartition('@')[2], u.path)
        else:
            cfg = plugin.rpc.listconfigs()['configs']
            user = config_value(cfg, 'bitcoin-rpcuser')
            password = rpc_password(cfg)
            port = config_value(cfg, 'bitcoin-rpcport')
            if not (user and password and port):
                raise RpcException("can't find elementsd's RPC credentials (bitcoin-rpcuser,"
                                   " bitcoin-rpcpassword, bitcoin-rpcport): set"
                                   " assetchan-elements-rpc=http://user:password@host:port")
            base = 'http://{}:{}'.format(config_value(cfg, 'bitcoin-rpcconnect') or '127.0.0.1',
                                         port)
        plugin.elements = ElementsRpc(base, user, password,
                                      plugin.get_option('assetchan-elements-wallet'))
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
def channels(plugin):
    return plugin.rpc.listpeerchannels()['channels']


def refresh_channels(plugin):
    """Rebuild the map from a channel's scid (or alias) to its asset."""
    assets = {}
    for c in channels(plugin):
        for scid in [c.get('short_channel_id')] + list(c.get('alias', {}).values()):
            if scid:
                assets[scid] = c.get('asset', LBTC)
    plugin.channel_assets = assets


def channel_asset(plugin, scid):
    """The asset of our channel scid (a real scid or an alias), or None."""
    if scid not in plugin.channel_assets:
        refresh_channels(plugin)
    return plugin.channel_assets.get(scid)


def find_channel(plugin, id):
    """The one channel `id` names (channel id, scid, alias, or a peer with a
    single channel), or None."""
    found = [c for c in channels(plugin)
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


def invoice_asset(plugin, payment_hash):
    """The asset an invoice is in: L-BTC unless assetchan-invoice noted one."""
    key = INVOICE_KEY + [payment_hash]
    for entry in plugin.rpc.listdatastore(key=key)['datastore']:
        if entry['key'] == key:
            return entry['string']
    return LBTC


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
    return {'channels': [channel_summary(c) for c in channels(plugin)
                         if 'asset' in c and asset in (None, c['asset'])]}


@plugin.method('assetchan-funds')
def assetchan_funds(plugin):
    """What elementsd's wallet holds (by asset id, or label) and what our
    channels hold, in base units."""
    balances = elements(plugin).call('getbalances')['mine']['trusted']
    return {'elements': {name: base_units(amount) for name, amount in balances.items()},
            'channels': [channel_summary(c) for c in channels(plugin)]}


@plugin.method('assetchan-send')
def assetchan_send(plugin, addr, asset, amount):
    """Send {amount} units of {asset} (an asset id, or an elementsd label
    such as "bitcoin") from elementsd's wallet to {addr}."""
    # sendtoaddress address amount comment comment_to subtractfeefromamount
    #               replaceable conf_target estimate_mode avoid_reuse assetlabel
    txid = elements(plugin).call('sendtoaddress', addr, coins(amount), '', '',
                                 False, False, None, None, None, asset)
    return {'txid': txid}


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
    for c in channels(plugin):
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
    """An invoice for {amount} units of {asset}: it must be paid over a
    channel in that asset, and it hints at our channels in it."""
    check_asset(asset)
    inv = plugin.rpc.invoice(int(amount) * 1000, label, description, expiry=expiry,
                             exposeprivatechannels=asset_hints(plugin, asset))
    plugin.rpc.datastore(key=INVOICE_KEY + [inv['payment_hash']], string=asset,
                         mode='create-or-replace')
    inv['asset'] = asset
    return inv


def asset_layer(plugin, asset):
    """The askrene layer `assetchan.<asset>`: our channels in other assets
    disabled, so a payment in `asset` can't leave over them."""
    layer = 'assetchan.' + asset
    if layer not in [lay['layer'] for lay in plugin.rpc.call('askrene-listlayers', {})['layers']]:
        plugin.rpc.call('askrene-create-layer', {'layer': layer, 'persistent': False})
    for c in channels(plugin):
        # (what askrene calls the channel: its scid, or its local alias)
        scid = c.get('short_channel_id') or c.get('alias', {}).get('local')
        if c.get('asset', LBTC) == asset or scid is None:
            continue
        for direction in (0, 1):
            plugin.rpc.call('askrene-update-channel',
                            {'layer': layer, 'enabled': False,
                             'short_channel_id_dir': '{}/{}'.format(scid, direction)})
    return layer


@plugin.method('assetchan-pay')
def assetchan_pay(plugin, bolt11, asset):
    """Pay {bolt11} in {asset}: xpay, kept off our channels in other assets."""
    check_asset(asset)
    res = plugin.rpc.call('xpay', {'invstring': bolt11, 'layers': [asset_layer(plugin, asset)]})
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
    """Final hop: the HTLC must arrive in the invoice's asset."""
    if 'short_channel_id' in onion or 'next_node_id' in onion:
        return {'result': 'continue'}  # a forward: core keeps the asset
    want = invoice_asset(plugin, htlc['payment_hash'])
    got = channel_asset(plugin, htlc['short_channel_id'])
    if got is None or got == want:
        return {'result': 'continue'}
    plugin.log('HTLC for {} arrived in {} but the invoice is in {}: failing it'
               .format(htlc['payment_hash'], got, want))
    # BOLT 4 incorrect_or_unknown_payment_details: htlc_msat, height
    data = struct.pack('>QI', int(htlc['amount_msat']), plugin.rpc.getinfo()['blockheight'])
    return {'result': 'fail', 'failure_message': '400f' + data.hex()}


# ------------------------------------------------------------ notifications
@plugin.subscribe('channel_state_changed')
def on_channel_state_changed(plugin, **kwargs):
    refresh_channels(plugin)


@plugin.subscribe('channel_opened')
def on_channel_opened(plugin, **kwargs):
    refresh_channels(plugin)


@plugin.subscribe('invoice_payment')
def on_invoice_payment(plugin, invoice_payment, **kwargs):
    """A paid invoice no longer needs its asset noted."""
    payment_hash = hashlib.sha256(bytes.fromhex(invoice_payment['preimage'])).hexdigest()
    try:
        plugin.rpc.deldatastore(key=INVOICE_KEY + [payment_hash])
    except RpcError:
        pass  # an ordinary invoice


@plugin.init()
def init(options, configuration, plugin, **kwargs):
    if options['assetchan-close-to'] not in ('elements', 'none'):
        return {'disable': "assetchan-close-to must be 'elements' or 'none'"}
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
