#!/usr/bin/env python3
"""Drive the Liquid asset-channel PoC network (docker-compose.yml) from the host.

    ./lnpoc.py setup                        # wallet, blocks, test asset, fund nodes, connect
    ./lnpoc.py open-lbtc alice bob 2000000  # ordinary L-BTC channel (sats)
    ./lnpoc.py open-asset alice bob 5000000 # asset channel (asset base units)
    ./lnpoc.py route alice carol 1000000000 # find a path (amount in msat / milli-units)
    ./lnpoc.py pay alice carol 1000000000   # invoice + pay along that path
    ./lnpoc.py channels                     # every channel, per asset
    ./lnpoc.py close alice bob              # cooperative close (+ mine)

`--lbtc` switches route/pay/close/invoice from the asset to L-BTC; `--asset ID`
picks another asset than the one `setup` issued.  Run `./lnpoc.py -h` for all
commands.  Only needs Python 3 and `docker compose`.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = ['docker', 'compose', '-f', os.path.join(HERE, 'docker-compose.yml')]
STATE_FILE = os.path.join(HERE, '.lnpoc-state.json')
NODES = ['alice', 'bob', 'carol']
LN_PORT = 20735
COIN = 10**8
WALLET = 'poc'
ECLI = ['elements-cli', '-chain=liquid-regtest', '-rpcport=18884',
        '-rpcuser=poc', '-rpcpassword=poc']


class Fail(Exception):
    pass


# ---------------------------------------------------------------- plumbing
def run(cmd, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise Fail(f"{' '.join(cmd[len(COMPOSE):])}\n{p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def parse(out):
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def cli_arg(v):
    return v if isinstance(v, str) else json.dumps(v)


def ecli(*args, wallet=True, check=True, **named):
    """elements-cli in the elementsd container (keyword args: -named)."""
    cmd = COMPOSE + ['exec', '-T', 'elementsd'] + ECLI
    if wallet:
        cmd.append(f'-rpcwallet={WALLET}')
    if named:
        cmd.append('-named')
    cmd += [cli_arg(a) for a in args]
    cmd += [f"{k}={cli_arg(v)}" for k, v in named.items()]
    return parse(run(cmd, check))


def ln(node, method, check=True, **params):
    """lightning-cli in a node's container."""
    cmd = COMPOSE + ['exec', '-T', node, 'lightning-cli',
                     '--network=liquid-regtest', '--lightning-dir=/data', '-k', method]
    for k, v in params.items():
        if v is None:
            continue
        cmd.append(f"{k}={cli_arg(v)}")
    return parse(run(cmd, check))


def wait_for(what, fn, timeout=120):
    start = time.time()
    while True:
        try:
            res = fn()
            if res:
                return res
        except Fail:
            pass
        if time.time() > start + timeout:
            raise Fail(f"timed out waiting for {what}")
        time.sleep(1)


def coins(sat):
    """Base units -> the decimal amounts elements-cli wants."""
    return str(Decimal(sat) / COIN)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def lbtc_asset():
    return ecli('dumpassetlabels', wallet=False)['bitcoin']


def default_asset(args):
    """The asset an operation is in: None means L-BTC."""
    if getattr(args, 'lbtc', False):
        return None
    if getattr(args, 'asset', None):
        return args.asset
    asset = load_state().get('asset')
    if not asset:
        raise Fail("no asset known: run `setup` first, or pass --asset / --lbtc")
    return asset


def asset_label(asset):
    if asset is None:
        return 'L-BTC'
    if asset == load_state().get('asset'):
        return f"ASSET({asset[:8]})"
    return f"asset({asset[:8]})"


_ids = {}


def node_id(node):
    if node not in _ids:
        _ids[node] = ln(node, 'getinfo')['id']
    return _ids[node]


def node_name(nid):
    for n in NODES:
        if node_id(n) == nid:
            return n
    return nid[:12]


def mine(n=1):
    addr = ecli('getnewaddress')
    ecli('generatetoaddress', n, addr)
    height = ecli('getblockcount', wallet=False)
    for node in NODES:
        wait_for(f"{node} to sync to block {height}",
                 lambda: ln(node, 'getinfo')['blockheight'] >= height)
    return height


def connect(a, b):
    """Connect a to b (no-op if already connected)."""
    ln(a, 'connect', id=node_id(b), host=b, port=LN_PORT)


def channels_of(node):
    return ln(node, 'listpeerchannels')['channels']


def channel_between(a, b, asset):
    """The normal channel between a and b in `asset` (None: L-BTC), from a's side."""
    found = [c for c in channels_of(a)
             if c['peer_id'] == node_id(b)
             and c['state'] == 'CHANNELD_NORMAL'
             and c.get('liquid_asset') == asset]
    if not found:
        raise Fail(f"no {asset_label(asset)} channel between {a} and {b}")
    return found[0]


def explicit_utxo(asset, amount_sat):
    """Have elementsd's wallet pay itself an explicit (unblinded) output."""
    info = ecli('getaddressinfo', ecli('getnewaddress'))
    txid = ecli('sendtoaddress', address=info['unconfidential'],
                amount=coins(amount_sat), assetlabel=asset)
    for v in ecli('getrawtransaction', txid, True, wallet=False)['vout']:
        if v['scriptPubKey'].get('hex') == info['scriptPubKey'] and 'value' in v:
            return {'txid': txid, 'vout': v['n']}
    raise Fail("elementsd didn't create an explicit output")


def unconfidential_address():
    return ecli('getaddressinfo', ecli('getnewaddress'))['unconfidential']


# ---------------------------------------------------------------- commands
def cmd_up(args):
    subprocess.run(COMPOSE + ['up', '-d', '--build'], check=True)


def cmd_down(args):
    subprocess.run(COMPOSE + ['down'] + (['-v'] if args.volumes else []), check=True)
    if args.volumes and os.path.exists(STATE_FILE):
        # The chain is gone, and the asset with it.
        os.remove(STATE_FILE)


def cmd_setup(args):
    wait_for('elementsd', lambda: ecli('getblockchaininfo', wallet=False))
    if WALLET not in ecli('listwallets', wallet=False):
        ecli('loadwallet', WALLET, wallet=False, check=False)
    if WALLET not in ecli('listwallets', wallet=False):
        ecli('createwallet', WALLET, wallet=False)
    if ecli('getblockcount', wallet=False) < 101:
        print("mining 101 blocks ...")
        ecli('generatetoaddress', 101, ecli('getnewaddress'))

    state = load_state()
    if not state.get('asset'):
        issued = ecli('issueasset', args.asset_amount, 0, False)
        state['asset'] = issued['asset']
        save_state(state)
        print(f"issued {args.asset_amount} of asset {state['asset']}")
    ecli('generatetoaddress', 1, ecli('getnewaddress'))

    for node in NODES:
        wait_for(node, lambda: ln(node, 'getinfo'))
        addr = ln(node, 'newaddr')['bech32']
        ecli('sendtoaddress', addr, coins(args.fund_sat))
        print(f"{node:6} {node_id(node)}  funded with {args.fund_sat} sat L-BTC")
    mine(1)

    for a, b in zip(NODES, NODES[1:]):
        connect(a, b)
        print(f"connected {a} -> {b}")
    print(f"\nasset: {state['asset']}\nL-BTC: {lbtc_asset()}")


def cmd_mine(args):
    print(f"block height {mine(args.blocks)}")


def cmd_open_lbtc(args):
    connect(args.frm, args.to)
    res = ln(args.frm, 'fundchannel', id=node_id(args.to), amount=args.amount,
             announce=False)
    print(f"funding tx {res['txid']}")
    mine(6)
    wait_for('channel', lambda: channel_between(args.frm, args.to, None))
    print(f"L-BTC channel {args.frm} -> {args.to} ready: {args.amount} sat")


def cmd_open_asset(args):
    asset = args.asset or default_asset(args)
    lbtc = lbtc_asset()
    fee = 1000
    peer = node_id(args.to)
    connect(args.frm, args.to)

    # 1. Ask the opener for the channel's funding address (2-of-2).
    start = ln(args.frm, 'fundchannel_start', id=peer, amount=args.units,
               announce=False, asset=asset, fee_reserve=args.fee_reserve)
    addr = start['funding_address']

    # 2. elementsd builds the funding tx: the asset to the channel at output 0,
    #    the L-BTC fee reserve to the same address at output 1.  Explicit
    #    inputs, so nothing needs blinding.
    x_in = explicit_utxo(asset, args.units)
    l_in = explicit_utxo(lbtc, args.fee_reserve + fee + 100000)
    mine(1)
    psbt = ecli('createpsbt', [x_in, l_in],
                [{addr: coins(args.units), 'asset': asset},
                 {addr: coins(args.fee_reserve)},
                 {unconfidential_address(): coins(100000)},
                 {'fee': coins(fee)}])
    signed = ecli('walletprocesspsbt', psbt)
    if not signed.get('complete'):
        raise Fail(f"elementsd couldn't sign the funding tx: {signed}")

    # 3. Hand it to the opener, then broadcast.
    ln(args.frm, 'fundchannel_complete', id=peer, psbt=signed['psbt'])
    final = ecli('finalizepsbt', signed['psbt'])
    txid = ecli('sendrawtransaction', final['hex'], wallet=False)
    print(f"funding tx {txid}")
    mine(6)
    wait_for('channel', lambda: channel_between(args.frm, args.to, asset))
    print(f"{asset_label(asset)} channel {args.frm} -> {args.to} ready: "
          f"{args.units} units + {args.fee_reserve} sat L-BTC fee reserve")


def cmd_channels(args):
    print(f"{'node':6} {'peer':6} {'asset':16} {'state':18} {'ours':>14} {'total':>14}  scid / alias")
    for node in NODES:
        for c in channels_of(node):
            unit = 1000
            print(f"{node:6} {node_name(c['peer_id']):6} "
                  f"{asset_label(c.get('liquid_asset')):16} {c['state']:18} "
                  f"{c['to_us_msat'] // unit:>14} {c['total_msat'] // unit:>14}  "
                  f"{c.get('short_channel_id', '-')} / {c.get('alias', {}).get('local', '-')}")


def cmd_balances(args):
    for node in NODES:
        onchain = sum(o['amount_msat'] for o in ln(node, 'listfunds')['outputs']
                      if o['status'] == 'confirmed') // 1000
        per_asset = {}
        for c in channels_of(node):
            if c['state'] == 'CHANNELD_NORMAL':
                key = asset_label(c.get('liquid_asset'))
                per_asset[key] = per_asset.get(key, 0) + c['to_us_msat'] // 1000
        chans = ', '.join(f"{k}: {v}" for k, v in sorted(per_asset.items())) or '-'
        print(f"{node:6} on-chain L-BTC {onchain:>12} sat   in channels: {chans}")


def find_route(frm, to, amount_msat, asset, final_cltv=18):
    """Shortest path over normal channels in `asset` with enough spendable
    balance, then fees and CLTVs worked out backwards from the payee."""
    edges = {}
    for node in NODES:
        for c in channels_of(node):
            if c['state'] != 'CHANNELD_NORMAL' or c.get('liquid_asset') != asset:
                continue
            peer = node_name(c['peer_id'])
            edges.setdefault(node, []).append((peer, c))

    # Breadth-first search (amounts here are small next to the fees).
    paths = {frm: []}
    queue = [frm]
    while queue:
        node = queue.pop(0)
        for peer, c in edges.get(node, []):
            if peer in paths or c['spendable_msat'] < amount_msat:
                continue
            paths[peer] = paths[node] + [(node, peer, c)]
            queue.append(peer)
    if to not in paths:
        raise Fail(f"no {asset_label(asset)} path from {frm} to {to} for {amount_msat} msat")
    hops = paths[to]

    route = []
    amount, delay = amount_msat, final_cltv + 1
    for i in range(len(hops) - 1, -1, -1):
        sender, receiver, c = hops[i]
        # The payer uses its own channel id; forwarders know private
        # channels by their local alias.
        chan = c['short_channel_id'] if i == 0 else c.get('alias', {}).get('local', c['short_channel_id'])
        route.insert(0, {'id': node_id(receiver), 'channel': chan,
                         'amount_msat': amount, 'delay': delay})
        if i > 0:
            # `sender` forwards over c: it charges its fee and CLTV delta.
            amount += c['fee_base_msat'] + -(-amount * c['fee_proportional_millionths'] // 10**6)
            delay += c['updates']['local']['cltv_expiry_delta']
    return [frm] + [h[1] for h in hops], route


def cmd_route(args):
    asset = default_asset(args)
    path, route = find_route(args.frm, args.to, args.amount_msat, asset)
    print(f"{asset_label(asset)} path: {' -> '.join(path)}")
    print(json.dumps(route, indent=2))


def cmd_invoice(args):
    asset = default_asset(args)
    label = args.label or f"lnpoc-{int(time.time() * 1000)}"
    inv = ln(args.node, 'invoice', amount_msat=args.amount_msat, label=label,
             description=f"lnpoc {asset_label(asset)}", asset=asset)
    print(inv['bolt11'])
    return inv


def cmd_pay(args):
    asset = default_asset(args)
    label = f"lnpoc-{int(time.time() * 1000)}"
    inv = ln(args.to, 'invoice', amount_msat=args.amount_msat, label=label,
             description=f"lnpoc {asset_label(asset)}", asset=asset)
    final = ln(args.to, 'decode', string=inv['bolt11'])['min_final_cltv_expiry']
    path, route = find_route(args.frm, args.to, args.amount_msat, asset, final)
    print(f"paying {args.amount_msat} msat in {asset_label(asset)}: {' -> '.join(path)}")
    ln(args.frm, 'sendpay', route=route, payment_hash=inv['payment_hash'],
       payment_secret=inv['payment_secret'], amount_msat=args.amount_msat)
    res = ln(args.frm, 'waitsendpay', check=False, payment_hash=inv['payment_hash'])
    if isinstance(res, dict) and res.get('status') == 'complete':
        print(f"paid: preimage {res['payment_preimage']}")
    else:
        raise Fail(f"payment failed: {res}")


def cmd_close(args):
    asset = default_asset(args)
    c = channel_between(args.frm, args.to, asset)
    before = set(ecli('getrawmempool', wallet=False))
    ln(args.frm, 'close', id=c['channel_id'])
    # An opener who funded via fundchannel_complete leaves broadcasting the
    # close to the peer, so just watch the mempool.
    txid = wait_for('close tx', lambda: [t for t in ecli('getrawmempool', wallet=False)
                                         if t not in before])[0]
    mine(1)
    tx = ecli('getrawtransaction', txid, True, wallet=False)
    print(f"closed {asset_label(asset)} channel {args.frm} <-> {args.to}: tx {txid}")
    for v in tx['vout']:
        dest = v['scriptPubKey'].get('address') or v['scriptPubKey'].get('type')
        asset_out = None if v.get('asset') == lbtc_asset() else v.get('asset')
        print(f"  {asset_label(asset_out):16} {int(Decimal(str(v['value'])) * COIN):>12}  {dest}")


def cmd_demo(args):
    """Whole story in one go: two kinds of channels on each hop, a payment in
    each asset, then cooperative closes."""
    if not load_state().get('asset'):
        cmd_setup(argparse.Namespace(asset_amount=1000, fund_sat=10_000_000))
    for a, b in zip(NODES, NODES[1:]):
        cmd_open_lbtc(argparse.Namespace(frm=a, to=b, amount=5_000_000))
        cmd_open_asset(argparse.Namespace(frm=a, to=b, units=5_000_000,
                                          fee_reserve=100_000, asset=None, lbtc=False))
    cmd_channels(args)
    for lbtc in (False, True):
        cmd_pay(argparse.Namespace(frm='alice', to='carol', amount_msat=1_000_000_000,
                                   asset=None, lbtc=lbtc))
    cmd_balances(args)
    for a, b in zip(NODES, NODES[1:]):
        for lbtc in (False, True):
            cmd_close(argparse.Namespace(frm=a, to=b, asset=None, lbtc=lbtc))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    def add(name, fn, help, asset=False):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(fn=fn)
        if asset:
            g = sp.add_mutually_exclusive_group()
            g.add_argument('--asset', help='asset id (default: the one `setup` issued)')
            g.add_argument('--lbtc', action='store_true', help='use L-BTC instead')
        return sp

    add('up', cmd_up, 'build and start the containers')
    sp = add('down', cmd_down, 'stop the containers')
    sp.add_argument('-v', '--volumes', action='store_true', help='also delete all data')
    sp = add('setup', cmd_setup, 'wallet, blocks, test asset, fund nodes, connect them')
    sp.add_argument('--asset-amount', type=int, default=1000, help='asset to issue (whole units)')
    sp.add_argument('--fund-sat', type=int, default=10_000_000, help='L-BTC per node wallet')
    sp = add('mine', cmd_mine, 'mine blocks')
    sp.add_argument('blocks', type=int, nargs='?', default=1)
    sp = add('open-lbtc', cmd_open_lbtc, 'open an ordinary L-BTC channel')
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('amount', type=int, help='sats')
    sp = add('open-asset', cmd_open_asset, 'open an asset channel (asset + L-BTC fee reserve)')
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('units', type=int, help='asset base units')
    sp.add_argument('--fee-reserve', type=int, default=100_000, help='L-BTC sats')
    sp.add_argument('--asset', help='asset id (default: the one `setup` issued)')
    add('channels', cmd_channels, 'list every channel on every node')
    add('balances', cmd_balances, 'on-chain and per-asset channel balances')
    sp = add('route', cmd_route, 'find a path in one asset', asset=True)
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('amount_msat', type=int)
    sp = add('invoice', cmd_invoice, 'create an invoice', asset=True)
    sp.add_argument('node', choices=NODES)
    sp.add_argument('amount_msat', type=int)
    sp.add_argument('--label')
    sp = add('pay', cmd_pay, 'invoice on `to`, pay it from `frm` along a found path', asset=True)
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('amount_msat', type=int)
    sp = add('close', cmd_close, 'cooperatively close a channel and mine it', asset=True)
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    add('demo', cmd_demo, 'setup, open both kinds of channels, pay in both, close')

    args = p.parse_args()
    try:
        args.fn(args)
    except Fail as e:
        sys.exit(f"error: {e}")


if __name__ == '__main__':
    main()
