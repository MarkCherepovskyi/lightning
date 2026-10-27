"""Helpers for the Liquid testnet cases (nodes alice, bob, carol, dave).

Run the case scripts from this directory with LNPOC_NET=testnet (set here).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

ARGV = list(sys.argv[1:])
sys.argv = ['lnpoc.py']
os.environ['LNPOC_NET'] = 'testnet'
import importlib.util

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # contrib/liquid-poc
spec = importlib.util.spec_from_file_location('lnpoc', HERE + '/lnpoc.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

ln = m.ln
ecli = m.ecli
ASSET = m.load_state()['asset']
EXPLORER = 'https://blockstream.info/liquidtestnet'


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def dc(*args, check=True):
    cmd = ['docker', 'compose', '-f', HERE + '/docker-compose.testnet.yml'] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout


def esplora(path):
    for _ in range(5):
        try:
            with urllib.request.urlopen(f'https://blockstream.info/liquidtestnet/api/{path}', timeout=20) as r:
                return json.load(r)
        except Exception:
            time.sleep(3)
    return None


def height():
    return ecli('getblockcount', wallet=False)


def wait_blocks(n, why=''):
    target = height() + n
    log(f'waiting {n} blocks (to {target}) {why}')
    while height() < target:
        time.sleep(15)


def wait(what, fn, timeout=1800, every=10):
    t0 = time.time()
    while True:
        try:
            r = fn()
            if r:
                return r
        except Exception:
            pass
        if time.time() > t0 + timeout:
            raise TimeoutError(what)
        time.sleep(every)


def chan(node, peer, states=('CHANNELD_NORMAL',), funding_prefix=None):
    for c in ln(node, 'listpeerchannels', id=m.node_id(peer))['channels']:
        if c['state'] in states and (not funding_prefix or c['funding_txid'].startswith(funding_prefix)):
            return c
    return None


def outspend(txid, vout):
    r = esplora(f'tx/{txid}/outspend/{vout}')
    return r if r and r.get('spent') else None


def link(txid):
    return f'{EXPLORER}/tx/{txid}'


def sync_nodes(nodes=None):
    h = height()
    for n in nodes or m.NODES:
        try:
            wait(n, lambda: ln(n, 'getinfo')['blockheight'] >= h, timeout=300, every=5)
        except Exception:
            pass


def hold_fulfill(node):
    """Make `node` disconnect instead of sending update_fulfill_htlc."""
    dc('exec', '-T', node, 'sh', '-c', 'echo "-WIRE_UPDATE_FULFILL_HTLC" > /data/dev_disconnect')
    dc('restart', node)
    wait(node, lambda: ln(node, 'getinfo'), timeout=120, every=3)
    sync_nodes([node])


def htlcs_of(node, cid):
    for c in ln(node, 'listpeerchannels')['channels']:
        if c['channel_id'] == cid:
            return c.get('htlcs', [])
    return []


def delay_htlcs(node):
    """Make `node` wait 40s before processing an incoming HTLC (a window to
    take the other side offline while the HTLC is committed)."""
    dc('exec', '-T', node, 'sh', '-c', ': > /data/dev_disconnect')
    dc('restart', node)
    wait(node, lambda: ln(node, 'getinfo'), timeout=180, every=3)
    sync_nodes([node])
    dc('cp', os.path.dirname(os.path.abspath(__file__)) + '/delay.py', f'{node}:/data/delay.py')
    dc('exec', '-T', node, 'chmod', '+x', '/data/delay.py')
    ln(node, 'plugin', subcommand='start', plugin='/data/delay.py')
