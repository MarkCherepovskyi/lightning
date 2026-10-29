#!/usr/bin/env python3
"""lqui: a web page for the Liquid asset-channel nodes (alice, bob, carol, dave).

    ./lqui.py                    # real Liquid testnet stack (docker-compose.testnet.yml)
    ./lqui.py --net regtest      # the regtest stack (docker-compose.yml)
    ./lqui.py --port 8765

Then open http://127.0.0.1:8765.  It is a thin layer over lq.py and lnpoc.py: the page
shows nodes, peers, balances and channels, and starts the same open, pay, close, send and
fund commands.  One command runs at a time; its output shows in the Activity panel.
Python 3 standard library only.  It listens on localhost and spends test coins: don't
expose it.
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

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, 'ui')

# lnpoc reads LNPOC_NET at import time, so settle the network first.
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument('--net', choices=['testnet', 'regtest'], default=os.environ.get('LNPOC_NET', 'testnet'))
os.environ['LNPOC_NET'] = _pre.parse_known_args()[0].net
sys.path.insert(0, HERE)
import lnpoc as L  # noqa: E402
import lq  # noqa: E402

NODES = L.NODES
HEX64 = re.compile(r'^[0-9a-f]{64}$')
ADDRESS = re.compile(r'^[A-Za-z0-9]{20,150}$')
ns = argparse.Namespace


# ---------------------------------------------------------------- state
class Poller:
    """Reads the nodes in the background so a page load never waits on docker."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = None
        self.pool = ThreadPoolExecutor(max_workers=12)

    def node_snapshot(self, node):
        calls = {
            'info': lambda: L.ln(node, 'getinfo'),
            'funds': lambda: lq.node_funds(node),
            'peers': lambda: L.ln(node, 'listpeers')['peers'],
            'chans': lambda: L.ln(node, 'listpeerchannels')['channels'],
        }
        futures = {k: self.pool.submit(fn) for k, fn in calls.items()}
        try:
            return {k: f.result() for k, f in futures.items()}
        except L.Fail:
            return None

    def poll(self):
        chain = self.pool.submit(lambda: L.ecli('getblockchaininfo', wallet=False))
        wallet = self.pool.submit(self.elements_wallet)
        snaps = {n: f.result() for n, f in
                 [(n, self.pool.submit(self.node_snapshot, n)) for n in NODES]}
        try:
            info = chain.result()
        except L.Fail:
            info = {'chain': '?', 'blocks': 0, 'headers': 0}

        ids = {s['info']['id']: n for n, s in snaps.items() if s}
        asset = L.load_state().get('asset')
        assets = {}
        if asset:
            assets[asset] = L.asset_label(asset)

        nodes = []
        for name in NODES:
            s = snaps[name]
            if not s:
                nodes.append({'name': name, 'id': '', 'ok': False, 'lbtc': 0, 'assets': {}, 'peers': []})
                continue
            lbtc, held = s['funds']
            for a in held:
                assets.setdefault(a, L.asset_label(a))
            nodes.append({
                'name': name, 'id': s['info']['id'], 'ok': True, 'lbtc': lbtc, 'assets': held,
                'peers': [{'name': ids.get(p['id'], p['id'][:10]), 'connected': p['connected']}
                          for p in s['peers']],
            })

        # Every channel shows up on both nodes; keep the opener's view.
        chans = {}
        for name in NODES:
            for c in (snaps[name] or {}).get('chans', []):
                key = c.get('channel_id') or c.get('peer_id') + str(c.get('short_channel_id'))
                if key in chans and c.get('opener') != 'local':
                    continue
                a = c.get('liquid_asset')
                if a:
                    assets.setdefault(a, L.asset_label(a))
                ours, total = c['to_us_msat'] // 1000, c['total_msat'] // 1000
                chans[key] = {
                    'id': key, 'a': name, 'b': ids.get(c['peer_id'], c['peer_id'][:10]), 'asset': a,
                    'state': self.state_name(c['state']), 'a_units': ours, 'b_units': total - ours,
                    'scid': c.get('short_channel_id'), 'announced': bool(c.get('public')),
                }
        order = {'open': 0, 'opening': 1, 'closing': 2, 'closed': 3}
        channels = sorted(chans.values(), key=lambda c: (order[c['state']], c['a'], c['b']))

        state = {
            'net': info['chain'], 'block': info['blocks'], 'headers': info['headers'],
            'syncing': bool(info.get('initialblockdownload')),
            'default_asset': asset, 'assets': assets, 'elements': wallet.result(),
            'nodes': nodes, 'channels': channels,
        }
        with self.lock:
            self.state = state

    @staticmethod
    def state_name(s):
        if s == 'CHANNELD_NORMAL':
            return 'open'
        if s in ('ONCHAIN', 'CLOSED'):
            return 'closed'
        if s.startswith(('CHANNELD_AWAITING', 'OPENINGD', 'DUALOPEND')):
            return 'opening'
        return 'closing'

    @staticmethod
    def elements_wallet():
        try:
            bal = L.ecli('getbalances')['mine']['trusted']
        except L.Fail:
            return {}
        return {L.asset_label(None if k == 'bitcoin' else k): int(v * L.COIN) for k, v in bal.items()}

    def loop(self):
        while True:
            try:
                self.poll()
            except Exception as e:  # keep serving the last good state
                print(f"poll failed: {e}", file=sys.stderr)
            time.sleep(2)

    def snapshot(self):
        with self.lock:
            return dict(self.state) if self.state else None


# ---------------------------------------------------------------- jobs
class Jobs:
    """One command at a time: they mine, wait for blocks and restart containers."""

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
        except L.Fail as e:
            job['log'].append(f"error: {node_message(str(e))}")
            status = 'error'
        except Exception as e:
            job['log'].append(f"error: {type(e).__name__}: {e}")
            status = 'error'
        out.flush()
        job['ended'] = time.time()
        job['status'] = status


def node_message(text):
    """A failed lightning-cli or elements-cli call: the node's own message, not the docker command."""
    m = re.search(r'"message":\s*"((?:[^"\\]|\\.)*)"', text)
    return json.loads(f'"{m.group(1)}"') if m else text


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
        raise BadRequest(f"{key}: pick one of {', '.join(NODES)}")
    return v


def int_arg(body, key):
    try:
        v = int(body.get(key))
    except (TypeError, ValueError):
        raise BadRequest(f"{key}: enter a whole number")
    if not 1 <= v <= 10**13:
        raise BadRequest(f"{key}: out of range")
    return v


def asset_arg(body):
    """(asset id or None, is L-BTC)."""
    a = body.get('asset')
    if a in (None, '', 'lbtc'):
        return None, True
    if not HEX64.match(a):
        raise BadRequest("asset: not an asset id")
    return a, False


def label(asset):
    return L.asset_label(asset)


def build_job(body):
    """(title, function) for a request; raises BadRequest."""
    action = body.get('action')
    if action == 'open':
        frm, to = node_arg(body, 'frm'), node_arg(body, 'to')
        if frm == to:
            raise BadRequest("pick two different nodes")
        units, (asset, lbtc) = int_arg(body, 'units'), asset_arg(body)
        args = ns(frm=frm, to=to, units=units, lbtc=lbtc, asset=asset, announce=bool(body.get('announce')),
                  fee_reserve=6000, change=2000)
        return (f"Open {label(asset)} channel {frm} to {to}, {units:,}", lambda: lq.cmd_open(args))
    if action == 'pay':
        frm, to = node_arg(body, 'frm'), node_arg(body, 'to')
        if frm == to:
            raise BadRequest("pick two different nodes")
        units, (asset, lbtc) = int_arg(body, 'units'), asset_arg(body)
        args = ns(frm=frm, to=to, units=units, lbtc=lbtc, asset=asset)
        return (f"Pay {units:,} {label(asset)} from {frm} to {to}", lambda: lq.cmd_pay(args))
    if action == 'close':
        frm, to = node_arg(body, 'frm'), node_arg(body, 'to')
        asset, lbtc = asset_arg(body)
        force = bool(body.get('force'))
        args = ns(frm=frm, to=to, lbtc=lbtc, asset=asset, force=force)
        return (f"{'Force close' if force else 'Close'} {label(asset)} channel {frm} and {to}",
                lambda: lq.cmd_close(args))
    if action == 'send':
        node = node_arg(body, 'node')
        address = body.get('address', '')
        if not ADDRESS.match(address):
            raise BadRequest("address: not a Liquid address")
        units, (asset, lbtc) = int_arg(body, 'units'), asset_arg(body)
        args = ns(node=node, address=address, units=units, lbtc=lbtc, asset=asset)
        return (f"Send {units:,} {label(asset)} from {node} on-chain", lambda: lq.cmd_send(args))
    if action == 'fund':
        node, sats = node_arg(body, 'node'), int_arg(body, 'sats')
        args = ns(node=node, sats=sats)
        return (f"Fund {node} with {sats:,} sat L-BTC", lambda: lq.cmd_fund(args))
    raise BadRequest("unknown action")


poller, jobs = Poller(), Jobs()
PORT = 8765


class Handler(BaseHTTPRequestHandler):
    server_version = 'lqui'

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
        ok = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}
        if self.headers.get('Host') not in ok:
            return False
        origin = self.headers.get('Origin')
        return origin is None or origin in {f'http://{h}' for h in ok}

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
                raise BadRequest("bad request")
            title, fn = build_job(body)
            return self.reply(200, {'id': jobs.start(title, fn)})
        except Busy:
            return self.reply(409, {'error': 'Another action is still running.'})
        except (BadRequest, ValueError) as e:
            return self.reply(400, {'error': str(e)})


def main():
    global PORT
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--net', choices=['testnet', 'regtest'], default='testnet')
    p.add_argument('--port', type=int, default=PORT)
    args = p.parse_args()
    PORT = args.port
    threading.Thread(target=poller.loop, daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f"{L.LN_NETWORK}: http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
