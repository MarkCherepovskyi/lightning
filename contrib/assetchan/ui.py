#!/usr/bin/env python3
"""ui.py: a web page for lightningd nodes running the assetchan plugin.

    contrib/assetchan/ui.py                                   # nodes.sh's A and B on liquid-testnet
    contrib/assetchan/ui.py --network liquid-regtest          # ... on liquid-regtest
    contrib/assetchan/ui.py --node alice=/path/to/lightning-dir --node bob=...
    contrib/assetchan/ui.py --port 8765

Then open http://127.0.0.1:8765.  The page shows the nodes, who is connected
to whom, wallet balances and every channel with its split, and has forms to
open (asset or L-BTC), pay, close (also force close), send on-chain and fund
a node.  Everything goes through the nodes' RPCs: the plugin's assetchan-*
methods for assets, core's for the rest.  One command runs at a time; its
output shows in the Activity panel.

On a regtest network the page mines the blocks a command waits for, through
elementsd's RPC (--elements-rpc, default $ELEMENTS_RPC, wallet
--elements-wallet, default $ELEMENTS_WALLET or poc); elsewhere it just waits.
Python 3 and pyln-client only.  It listens on localhost and spends test
coins: don't expose it.  `?demo` shows the page with sample data.
"""
import argparse
import contextlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from elements import ElementsError, ElementsRpc, config_value
from pyln.client import LightningRpc, RpcError

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, 'ui')
HEX64 = re.compile(r'^[0-9a-f]{64}$')
ADDRESS = re.compile(r'^[A-Za-z0-9]{20,150}$')

# Filled by main().
NETWORK = 'liquid-testnet'
NODES = {}          # name -> lightning-dir
ELEMENTS = None     # ElementsRpc, when we can mine
PORT = 8765


class Fail(Exception):
    """A command failed: the message is shown as is."""


# ---------------------------------------------------------------- rpc
def rpc(node):
    """A fresh client for the node: one per call, so threads don't share a socket."""
    return LightningRpc(os.path.join(NODES[node], NETWORK, 'lightning-rpc'))


def call(node, method, **params):
    try:
        return rpc(node).call(method, params)
    except RpcError as e:
        raise Fail('{}: {}: {}'.format(node, method, e.error.get('message', e.error)
                                       if isinstance(e.error, dict) else e.error))
    except OSError as e:
        raise Fail('{}: not reachable ({})'.format(node, e))


def config(node, name, default=None):
    return config_value(call(node, 'listconfigs')['configs'], name, default)


def asset_label(asset):
    return 'L-BTC' if asset in (None, 'lbtc', 'bitcoin') else 'ASSET({})'.format(asset[:8])


def can_mine():
    return ELEMENTS is not None and 'regtest' in NETWORK


def mine(n):
    if can_mine():
        try:
            ELEMENTS.call('generatetoaddress', n, ELEMENTS.call('getnewaddress'))
        except ElementsError as e:
            raise Fail(str(e))


def wait_for(what, fn, mine_between=False, timeout=None):
    """Poll fn() until it returns something true; on regtest mine a block per
    try when asked.  Testnet blocks take about a minute, so the default
    timeout is generous there."""
    if timeout is None:
        timeout = 60 if can_mine() else 20 * 60
    end = time.time() + timeout
    while True:
        got = fn()
        if got:
            return got
        if time.time() > end:
            raise Fail('timed out waiting for {}'.format(what))
        if mine_between:
            mine(1)
        time.sleep(1 if can_mine() else 10)


# ---------------------------------------------------------------- state
class Poller:
    """Reads the nodes when the page asks for the state, at most every
    MAX_AGE seconds: nothing runs without a viewer."""
    MAX_AGE = 3

    def __init__(self):
        self.lock = threading.Lock()
        self.state = None
        self.read_at = 0
        self.pool = ThreadPoolExecutor(max_workers=16)

    def node_snapshot(self, node):
        calls = {
            'info': lambda: call(node, 'getinfo'),
            'funds': lambda: call(node, 'listfunds'),
            'peers': lambda: call(node, 'listpeers')['peers'],
            'chans': lambda: call(node, 'listpeerchannels')['channels'],
            'config': lambda: call(node, 'listconfigs')['configs'],
        }
        futures = {k: self.pool.submit(fn) for k, fn in calls.items()}
        try:
            snap = {k: f.result() for k, f in futures.items()}
        except Fail:
            return None
        try:
            snap['elements'] = call(node, 'assetchan-funds', channels=False)['elements']
        except Fail:
            snap['elements'] = {}
        return snap

    def poll(self):
        snaps = {n: f.result() for n, f in
                 [(n, self.pool.submit(self.node_snapshot, n)) for n in NODES]}
        ids = {s['info']['id']: n for n, s in snaps.items() if s}
        assets = {}
        for s in snaps.values():
            for a in config_value((s or {}).get('config', {}), 'assetchan-asset', []):
                assets.setdefault(a, asset_label(a))

        nodes = []
        for name in NODES:
            s = snaps[name]
            if not s:
                nodes.append({'name': name, 'id': '', 'ok': False, 'lbtc': 0, 'assets': {}, 'peers': []})
                continue
            lbtc = sum(int(o['amount_msat']) // 1000 for o in s['funds']['outputs']
                       if o['status'] == 'confirmed' and not o.get('reserved'))
            held = {a: u for a, u in s['elements'].items() if HEX64.match(a)}
            for a in held:
                assets.setdefault(a, asset_label(a))
            nodes.append({
                'name': name, 'id': s['info']['id'], 'ok': True, 'lbtc': lbtc, 'assets': held,
                'peers': [{'name': ids.get(p['id'], p['id'][:10]), 'connected': p['connected']}
                          for p in s['peers']],
            })

        # Every channel shows up on both nodes; keep the opener's view.
        chans = {}
        for name in NODES:
            for c in (snaps[name] or {}).get('chans', []):
                key = c.get('channel_id') or c['peer_id'] + str(c.get('short_channel_id'))
                if key in chans and c.get('opener') != 'local':
                    continue
                a = c.get('asset')
                if a:
                    assets.setdefault(a, asset_label(a))
                ours, total = int(c.get('to_us_msat', 0)) // 1000, int(c.get('total_msat', 0)) // 1000
                chans[key] = {
                    'id': key, 'a': name, 'b': ids.get(c['peer_id'], c['peer_id'][:10]), 'asset': a,
                    'state': self.state_name(c['state']), 'a_units': ours, 'b_units': total - ours,
                    'scid': c.get('short_channel_id'),
                }
        order = {'open': 0, 'opening': 1, 'closing': 2, 'closed': 3}
        channels = sorted(chans.values(), key=lambda c: (order[c['state']], c['a'], c['b']))

        first = next((s for s in snaps.values() if s), None) or {}
        info, cfg = first.get('info', {}), first.get('config', {})
        elements = {asset_label(k): int(v) for k, v in first.get('elements', {}).items()}
        block = info.get('blockheight', 0)
        headers = block
        if ELEMENTS is not None:
            try:
                headers = ELEMENTS.call('getblockchaininfo')['headers']
            except ElementsError:
                pass
        return {
            'net': info.get('network', NETWORK), 'block': block, 'headers': headers,
            'syncing': 'warning_lightningd_sync' in info, 'mining': can_mine(),
            'assets': assets, 'elements': elements,
            'fee_reserve': config_value(cfg, 'assetchan-default-fee-reserve', 2000),
            'confirms': config_value(cfg, 'funding-confirms', 1),
            'nodes': nodes, 'channels': channels,
        }

    @staticmethod
    def state_name(s):
        if s == 'CHANNELD_NORMAL':
            return 'open'
        if s in ('ONCHAIN', 'CLOSED', 'FUNDING_SPEND_SEEN'):
            return 'closed'
        if s.startswith(('CHANNELD_AWAITING', 'OPENINGD', 'DUALOPEND')):
            return 'opening'
        return 'closing'

    def snapshot(self):
        """The state, re-read if older than MAX_AGE (one reader at a time;
        the others get what it read).  Keeps the last good one if reading
        fails."""
        with self.lock:
            if time.time() - self.read_at > self.MAX_AGE:
                try:
                    self.state = self.poll()
                except Exception as e:
                    print('poll failed: {}'.format(e), file=sys.stderr)
                self.read_at = time.time()
            return dict(self.state) if self.state else None


# ---------------------------------------------------------------- commands
def channel_of(node, channel_id):
    return next((c for c in call(node, 'listpeerchannels')['channels']
                 if c.get('channel_id') == channel_id), None)


def cmd_open(frm, to, units, asset, announce):
    info = call(to, 'getinfo')
    binding = next((b for b in info.get('binding', []) if b.get('type') in ('ipv4', 'ipv6')), None)
    if binding is None:
        raise Fail('{} has no address to connect to'.format(to))
    call(frm, 'connect', id=info['id'], host=binding['address'], port=binding['port'])
    if asset is None:
        res = call(frm, 'fundchannel', id=info['id'], amount=units, announce=announce)
        print('funding tx {}'.format(res['txid']))
    else:
        res = call(frm, 'assetchan-open', id=info['id'], amount=units, asset=asset, announce=announce)
        print('funding tx {}:{}, {} sat L-BTC fee reserve next to the asset'
              .format(res['txid'], res['outnum'], res['fee_reserve']))
    cid = res['channel_id']
    mine(config(frm, 'funding-confirms', 1))
    wait_for('the channel to open',
             lambda: (channel_of(frm, cid) or {}).get('state') == 'CHANNELD_NORMAL', mine_between=True)
    c = channel_of(frm, cid)
    print('{} channel {} -> {} ready: {} {} ({})'.format(
        asset_label(asset), frm, to, units, 'units' if asset else 'sat', c.get('short_channel_id')))


def cmd_pay(frm, to, units, asset):
    inv = call(to, 'assetchan-invoice', amount=units, asset=asset or 'lbtc',
               label='ui-{}'.format(time.time_ns()), description='ui payment {} -> {}'.format(frm, to))
    print('invoice on {} for {} {}: {}'.format(to, units, 'units' if asset else 'sat', inv['bolt11'][:40] + '…'))
    res = call(frm, 'assetchan-pay', bolt11=inv['bolt11'], asset=asset or 'lbtc')
    fee = (int(res['amount_sent_msat']) - int(res['amount_msat'])) // 1000
    print('paid: preimage {}, {} part(s), fee {} {}'.format(
        res['payment_preimage'], res.get('successful_parts', 1), fee, 'units' if asset else 'sat'))


def cmd_close(frm, to, channel_id, force):
    c = channel_of(frm, channel_id)
    if c is None:
        raise Fail('{} has no such channel'.format(frm))
    before = elements_assets(frm)
    if not force:
        res = call(frm, 'assetchan-close', id=channel_id)
        if res.get('type') != 'mutual':
            print('close is {}'.format(res.get('type')))
        print('closing tx {}'.format(res.get('txid')))
        mine(1)
    else:
        try:
            call(frm, 'dev-fail', id=c['peer_id'])
            print("{} failed the channel on purpose (dev-fail)".format(frm))
        except Fail:
            # Without --developer: drop the peer and give up on negotiating at once.
            call(frm, 'disconnect', id=c['peer_id'], force=True)
            call(frm, 'close', id=channel_id, unilateraltimeout=1)
            print("{} disconnected and closed unilaterally".format(frm))
        mine(1)
        wait_for('the commitment to confirm',
                 lambda: (channel_of(frm, channel_id) or {}).get('state') in ('ONCHAIN', None),
                 mine_between=True)
        print('commitment tx confirmed; each side sweeps its outputs as the timelocks pass')
        if can_mine():
            delay = max(config(frm, 'watchtime-blocks', 6), config(to, 'watchtime-blocks', 6))
            mine(delay + 1)
            time.sleep(3)
            mine(2)
    after = elements_assets(frm)
    for a in sorted(set(before) | set(after)):
        if after.get(a, 0) != before.get(a, 0):
            print("{}'s elementsd wallet: {} {:+,}".format(frm, asset_label(a), after.get(a, 0) - before.get(a, 0)))


def elements_assets(node):
    try:
        return {k: int(v) for k, v in call(node, 'assetchan-funds')['elements'].items()}
    except Fail:
        return {}


def cmd_send(node, address, units, asset):
    res = call(node, 'assetchan-send', addr=address, asset=asset or 'bitcoin', amount=units)
    print('sent {} {} from {}\'s elementsd wallet to {}: txid {}'.format(
        units, asset_label(asset), node, address, res['txid']))
    mine(1)


def cmd_fund(node, sats):
    addr = call(node, 'newaddr', addresstype='bech32')['bech32']
    res = call(node, 'assetchan-send', addr=addr, asset='bitcoin', amount=sats)
    print('sent {} sat L-BTC to {} ({}): txid {}'.format(sats, node, addr, res['txid']))
    mine(1)


# ---------------------------------------------------------------- jobs
class Jobs:
    """One command at a time: they mine and wait for blocks."""

    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        self.counter = 0

    def current(self):
        with self.lock:
            return dict(self.job, log=list(self.job['log'])) if self.job else None

    def start(self, title, fn):
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise Busy()
            self.counter += 1
            self.job = {'id': self.counter, 'title': title, 'status': 'running',
                        'started': time.time(), 'ended': None, 'log': []}
            job = self.job
        threading.Thread(target=self.run, args=(job, fn), daemon=True).start()
        return job['id']

    def run(self, job, fn):
        out = _LineSink(job['log'])
        status = 'done'
        try:
            with contextlib.redirect_stdout(out):
                fn()
        except Fail as e:
            job['log'].append('error: {}'.format(e))
            status = 'error'
        except Exception as e:
            job['log'].append('error: {}: {}'.format(type(e).__name__, e))
            status = 'error'
        out.flush()
        job['ended'] = time.time()
        job['status'] = status


class _LineSink:
    def __init__(self, lines):
        self.lines, self.buf = lines, ''

    def write(self, s):
        self.buf += s
        *done, self.buf = self.buf.split('\n')
        self.lines.extend(done)
        return len(s)

    def flush(self):
        if self.buf:
            self.lines.append(self.buf)
            self.buf = ''


class Busy(Exception):
    pass


class BadRequest(Exception):
    pass


# ---------------------------------------------------------------- requests
def node_arg(body, key):
    v = body.get(key)
    if v not in NODES:
        raise BadRequest('{}: pick one of {}'.format(key, ', '.join(NODES)))
    return v


def int_arg(body, key):
    try:
        v = int(body.get(key))
    except (TypeError, ValueError):
        raise BadRequest('{}: enter a whole number'.format(key))
    if not 1 <= v <= 10**13:
        raise BadRequest('{}: out of range'.format(key))
    return v


def asset_arg(body):
    """The asset id, or None for L-BTC."""
    a = body.get('asset')
    if a in (None, '', 'lbtc'):
        return None
    if not HEX64.match(a):
        raise BadRequest('asset: not an asset id')
    return a


def build_job(body):
    """(title, function) for a request; raises BadRequest."""
    action = body.get('action')
    if action == 'open':
        frm, to = node_arg(body, 'frm'), node_arg(body, 'to')
        if frm == to:
            raise BadRequest('pick two different nodes')
        units, asset, announce = int_arg(body, 'units'), asset_arg(body), bool(body.get('announce'))
        return ('Open {} channel {} to {}, {:,}'.format(asset_label(asset), frm, to, units),
                lambda: cmd_open(frm, to, units, asset, announce))
    if action == 'pay':
        frm, to = node_arg(body, 'frm'), node_arg(body, 'to')
        if frm == to:
            raise BadRequest('pick two different nodes')
        units, asset = int_arg(body, 'units'), asset_arg(body)
        return ('Pay {:,} {} from {} to {}'.format(units, asset_label(asset), frm, to),
                lambda: cmd_pay(frm, to, units, asset))
    if action == 'close':
        frm, to = node_arg(body, 'frm'), node_arg(body, 'to')
        chan, force = body.get('chan', ''), bool(body.get('force'))
        if not HEX64.match(chan):
            raise BadRequest('chan: not a channel id')
        verb = 'Force close' if force else 'Close'
        title = '{} {} channel {} and {}'.format(verb, asset_label(asset_arg(body)), frm, to)
        return (title, lambda: cmd_close(frm, to, chan, force))
    if action == 'send':
        node, address = node_arg(body, 'node'), body.get('address', '')
        if not ADDRESS.match(address):
            raise BadRequest('address: not a Liquid address')
        units, asset = int_arg(body, 'units'), asset_arg(body)
        return ('Send {:,} {} on-chain via {}'.format(units, asset_label(asset), node),
                lambda: cmd_send(node, address, units, asset))
    if action == 'fund':
        node, sats = node_arg(body, 'node'), int_arg(body, 'sats')
        return ('Fund {} with {:,} sat L-BTC'.format(node, sats), lambda: cmd_fund(node, sats))
    raise BadRequest('unknown action')


poller, jobs = Poller(), Jobs()


class Handler(BaseHTTPRequestHandler):
    server_version = 'assetchan-ui'

    def log_message(self, fmt, *a):
        pass

    def reply(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def local_only(self):
        """Refuse other Host names (DNS rebinding) and other origins (cross-site posts)."""
        ok = {'127.0.0.1:{}'.format(PORT), 'localhost:{}'.format(PORT)}
        if self.headers.get('Host') not in ok:
            return False
        origin = self.headers.get('Origin')
        return origin is None or origin in {'http://' + h for h in ok}

    def do_GET(self):
        if not self.local_only():
            return self.reply(403, {'error': 'forbidden'})
        path = self.path.split('?')[0]
        if path == '/api/state':
            state = poller.snapshot()
            if not state:
                return self.reply(503, {'error': 'reading the nodes, try again'})
            state['job'] = jobs.current()
            return self.reply(200, state)
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/demo.json': ('demo.json', 'application/json')}
        if path in files:
            name, ctype = files[path]
            with open(os.path.join(UI_DIR, name), 'rb') as f:
                return self.reply(200, f.read(), ctype)
        self.reply(404, {'error': 'not found'})

    def do_POST(self):
        if not self.local_only() or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.reply(403, {'error': 'forbidden'})
        if self.path != '/api/job':
            return self.reply(404, {'error': 'not found'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(min(length, 10_000)))
            if not isinstance(body, dict):
                raise BadRequest('bad request')
            title, fn = build_job(body)
            return self.reply(200, {'id': jobs.start(title, fn)})
        except Busy:
            return self.reply(409, {'error': 'Another action is still running.'})
        except (BadRequest, ValueError) as e:
            return self.reply(400, {'error': str(e)})


def main():
    global NETWORK, ELEMENTS, PORT
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--network', default=os.environ.get('ASSETCHAN_NETWORK', 'liquid-testnet'))
    p.add_argument('--node', action='append', metavar='NAME=LIGHTNING-DIR',
                   help="a node to show (default: A and B under nodes.sh's directory)")
    p.add_argument('--elements-rpc', default=os.environ.get('ELEMENTS_RPC'),
                   help='http://user:password@host:port, to mine on regtest and show the header count')
    p.add_argument('--elements-wallet', default=os.environ.get('ELEMENTS_WALLET', 'poc'))
    p.add_argument('--port', type=int, default=PORT)
    args = p.parse_args()
    NETWORK, PORT = args.network, args.port
    if args.node:
        for spec in args.node:
            name, _, d = spec.partition('=')
            if not name or not d:
                p.error('--node takes NAME=LIGHTNING-DIR')
            NODES[name] = os.path.expanduser(d)
    else:
        base = os.environ.get('ASSETCHAN_DIR') or os.path.expanduser(
            '~/.assetchan-' + NETWORK.replace('liquid-', ''))
        NODES.update({n: os.path.join(base, n) for n in ('A', 'B', 'C', 'D')
                      if os.path.isdir(os.path.join(base, n))})
        if not NODES:
            p.error('no nodes under {}: pass --node NAME=LIGHTNING-DIR'.format(base))
    if args.elements_rpc:
        ELEMENTS = ElementsRpc(args.elements_rpc, args.elements_wallet)
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print('{} ({}): http://127.0.0.1:{}'.format(NETWORK, ', '.join(NODES), PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
