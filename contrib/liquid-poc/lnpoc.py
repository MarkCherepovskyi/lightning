#!/usr/bin/env python3
"""Drive the Liquid asset-channel PoC network (docker-compose.yml) from the host.

    ./lnpoc.py start                        # up + setup + the web UI, all in one
    ./lnpoc.py setup                        # wallet, blocks, test asset, fund nodes, connect
    ./lnpoc.py open-lbtc alice bob 2000000  # ordinary L-BTC channel (sats)
    ./lnpoc.py open-asset alice bob 5000000 # asset channel (asset base units)
    ./lnpoc.py route alice carol 1000000000 # find a path (amount in msat / milli-units)
    ./lnpoc.py pay alice carol 1000000000   # invoice + pay along that path
    ./lnpoc.py channels                     # every channel, per asset
    ./lnpoc.py close alice bob              # cooperative close (+ mine)
    ./lnpoc.py force-close alice bob        # unilateral close, mine until both swept
    ./lnpoc.py open-asset alice bob 5000000 --announce   # gossiped on the asset's network
    ./lnpoc.py graph alice                  # what alice's gossip map holds
    ./lnpoc.py xpay alice carol 1000000000  # pay with xpay, routes found from gossip

`--lbtc` switches route/pay/close/invoice from the asset to L-BTC; `--asset ID`
picks another asset than the one `setup` issued.  Run `./lnpoc.py -h` for all
commands.  Only needs Python 3 and `docker compose`.

Cross-asset swaps (contrib/liquid-poc/CROSS_ASSET_SWAP.md): pay an invoice in
L-BTC, or in the asset `setup` issued, while only holding the other -- via a
swap-provider node that holds both.  `assetswap-plugin` is built into the
`cln` image and loaded on every node from startup (`up`/`start` already
cover it; see Dockerfile's `assetswap-builder` stage).

    ./lnpoc.py swap-provider bob lbtc <asset> 1000000       # bob quotes L-BTC->asset at 1:1 (+ fee)
    ./lnpoc.py swap-payer alice bob                         # alice uses bob as a provider
    ./lnpoc.py swap alice carol 1000000000 <asset>          # carol invoices the asset, alice pays it
    ./lnpoc.py assetinvoice carol 1000000000 <asset>        # just the invoice half of `swap`
    ./lnpoc.py payassetinvoice alice <assetinvoice-string>  # just the paying half of `swap`
    ./lnpoc.py swap-demo                                    # the whole thing, start to finish

`lbtc` is accepted in place of an asset id anywhere swap commands take one.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
# LNPOC_NET=testnet drives docker-compose.testnet.yml: the real Liquid testnet
# (blocks come every minute, nobody can mine them, coins come from the faucet).
TESTNET = os.environ.get('LNPOC_NET') == 'testnet'
if TESTNET:
    BASE_COMPOSE = os.path.join(HERE, 'docker-compose.testnet.yml')
    NODES_COMPOSE = os.path.join(HERE, '.lnpoc-testnet-nodes.yml')
    STATE_FILE = os.path.join(HERE, '.lnpoc-testnet-state.json')
    BASE_NODES = ['alice', 'bob', 'carol', 'dave']
    LN_NETWORK = 'liquid-testnet'
    BITCOIN_RPCPORT = 7039
    ECLI = ['elements-cli', '-chain=liquidtestnet', '-rpcport=7039',
            '-rpcuser=poc', '-rpcpassword=poc']
else:
    BASE_COMPOSE = os.path.join(HERE, 'docker-compose.yml')
    NODES_COMPOSE = os.path.join(HERE, '.lnpoc-nodes.yml')
    STATE_FILE = os.path.join(HERE, '.lnpoc-state.json')
    BASE_NODES = ['alice', 'bob', 'carol']
    LN_NETWORK = 'liquid-regtest'
    BITCOIN_RPCPORT = 18884
    ECLI = ['elements-cli', '-chain=liquid-regtest', '-rpcport=18884',
            '-rpcuser=poc', '-rpcpassword=poc']
LN_PORT = 20735
COIN = 10**8
WALLET = 'poc'
NODE_NAME_RE = re.compile(r'^[a-z][a-z0-9]{0,19}$')


def _read_extra_nodes():
    """Nodes added at runtime (`add-node` / the web UI), read straight from the
    state file: called at import time, before `load_state` (below) exists."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get('extra_nodes', [])
    except FileNotFoundError:
        return []


def all_nodes():
    extra = [n for n in _read_extra_nodes() if n not in BASE_NODES]
    return BASE_NODES + extra


def cln_service(name):
    """A docker-compose service block for one more Core Lightning node. It
    can't reuse the `x-cln` anchor in the base compose file -- YAML anchors
    don't cross the files `docker compose -f` merges -- so it repeats that
    block's settings for `name`."""
    dev_flags = ' --dev-fast-gossip --dev-bitcoind-poll=1' if not TESTNET else ' --dev-fast-gossip'
    return f"""  {name}:
    image: liquid-ln-poc/cln
    build:
      context: ../..
      dockerfile: contrib/liquid-poc/Dockerfile
      target: cln
    depends_on:
      - elementsd
    restart: unless-stopped
    stop_grace_period: 30s
    environment:
      NODE_ALIAS: {name}
    entrypoint:
      - sh
      - -c
      - >-
        exec lightningd
        --network={LN_NETWORK}
        --lightning-dir=/data
        --alias=$${{NODE_ALIAS}}
        --bind-addr=0.0.0.0:{LN_PORT}
        --bitcoin-rpcconnect=elementsd
        --bitcoin-rpcport={BITCOIN_RPCPORT}
        --bitcoin-rpcuser=poc
        --bitcoin-rpcpassword=poc
        --log-level=debug
        --developer{dev_flags}
    volumes:
      - {name}-data:/data
"""


def write_nodes_compose(names):
    """Regenerate the overlay compose file with one service per extra node.
    `COMPOSE` always loads it (-f), even when `names` is empty, so adding a
    node never needs to change the base compose file or restart anything
    that's already running."""
    services = ''.join(cln_service(n) for n in names)
    volumes = ''.join(f"  {n}-data:\n" for n in names)
    with open(NODES_COMPOSE, 'w') as f:
        f.write("# Generated by lnpoc.py add-node; edits here are overwritten.\n"
                f"services:\n{services or '  {}' + chr(10)}"
                f"volumes:\n{volumes or '  {}' + chr(10)}")


write_nodes_compose(_read_extra_nodes())
NODES = all_nodes()
COMPOSE = ['docker', 'compose', '-f', BASE_COMPOSE, '-f', NODES_COMPOSE]


class Fail(Exception):
    pass


# ---------------------------------------------------------------- plumbing
def run(cmd, check=True, input=None):
    p = subprocess.run(cmd, capture_output=True, text=True, input=input)
    if check and p.returncode != 0:
        raise Fail(f"{' '.join(cmd[len(COMPOSE):])}\n{p.stderr.strip() or p.stdout.strip()}")
    return p.stdout.strip()


def parse(out):
    # lightning-cli prints notifications as '# ...' lines before the result.
    body = '\n'.join(line for line in out.splitlines() if not line.startswith('#'))
    try:
        return json.loads(body)
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
                     f'--network={LN_NETWORK}', '--lightning-dir=/data', '-k', method]
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
    if asset in (None, 'lbtc'):
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


def mine(n=1, nodes=None):
    nodes = all_nodes() if nodes is None else nodes
    if TESTNET:
        # Nobody can mine here: wait for n blocks (about a minute each).
        height = ecli('getblockcount', wallet=False) + n
        wait_for(f"block {height}",
                 lambda: ecli('getblockcount', wallet=False) >= height,
                 timeout=n * 300)
        for node in nodes:
            wait_for(f"{node} to sync to block {height}",
                     lambda: ln(node, 'getinfo')['blockheight'] >= height,
                     timeout=300)
        return height
    addr = ecli('getnewaddress')
    ecli('generatetoaddress', n, addr)
    height = ecli('getblockcount', wallet=False)
    for node in nodes:
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


def follow_asset(asset):
    """Have every node follow the asset's gossip network (liquid-gossip-asset
    in its config), restarting the nodes that didn't yet."""
    line = f'liquid-gossip-asset={asset}'
    changed = []
    for node in NODES:
        conf = run(COMPOSE + ['exec', '-T', node, 'sh', '-c', 'cat /data/config 2>/dev/null; true'])
        if line not in conf.splitlines():
            run(COMPOSE + ['exec', '-T', node, 'sh', '-c', f'echo {line} >> /data/config'])
            changed.append(node)
    if changed:
        print(f"restarting {', '.join(changed)} to follow the asset's gossip ...")
        run(COMPOSE + ['restart'] + changed)
        for node in changed:
            wait_for(node, lambda: ln(node, 'getinfo'))


def add_config_lines(node, lines):
    """Append any of `lines` not already verbatim in node's /data/config,
    then restart it to pick them up.  Same append-if-missing approach as
    follow_asset(), generalized to more than one line at a time (used for
    assetswap-plugin's `assetswap-provider=` and pricetiers-plugin's
    `pricetiers-pair=` config, which must survive a restart -- see
    CROSS_ASSET_SWAP.md's "Restart idempotency" section for why; both
    plugins are always loaded from the image, so there's no `plugin=`
    line to add here)."""
    conf = run(COMPOSE + ['exec', '-T', node, 'sh', '-c', 'cat /data/config 2>/dev/null; true'])
    have = set(conf.splitlines())
    missing = [l for l in lines if l not in have]
    if not missing:
        return
    run(COMPOSE + ['exec', '-T', node, 'sh', '-c', 'cat >> /data/config'],
       input='\n'.join(missing) + '\n')
    print(f"restarting {node} to apply: {', '.join(missing)}")
    run(COMPOSE + ['restart', node])
    wait_for(node, lambda: ln(node, 'getinfo'))


# ---------------------------------------------------------------- commands
def cmd_up(args):
    subprocess.run(COMPOSE + ['up', '-d', '--build'], check=True)


def cmd_down(args):
    subprocess.run(['pkill', '-f', 'lqui.py'], check=False)
    subprocess.run(COMPOSE + ['down'] + (['-v'] if args.volumes else []), check=True)
    if args.volumes and os.path.exists(STATE_FILE):
        # The chain is gone, and the asset with it.
        os.remove(STATE_FILE)


def cmd_add_node(args):
    """Add one more Core Lightning node to the running stack: write its
    service into the overlay compose file, build and start it (reusing the
    `cln` image already built for alice/bob/carol), then optionally seed its
    wallet with L-BTC."""
    name = args.name
    if not NODE_NAME_RE.match(name):
        raise Fail("node name: lowercase letters/digits, starting with a letter, up to 20 characters")
    extra = _read_extra_nodes()
    if name in BASE_NODES or name == 'elementsd' or name in extra:
        raise Fail(f"{name}: a node by that name already exists")

    write_nodes_compose(extra + [name])
    print(f"building and starting {name} ...")
    run(COMPOSE + ['up', '-d', '--build', name])
    wait_for(name, lambda: ln(name, 'getinfo'))

    state = load_state()
    state['extra_nodes'] = extra + [name]
    save_state(state)
    print(f"{name} is up: {node_id(name)}")

    fund_sat = getattr(args, 'fund_sat', 0)
    if fund_sat:
        if WALLET not in ecli('listwallets', wallet=False):
            print(f"not funding {name}: elementsd's '{WALLET}' wallet isn't loaded (run `setup` first)")
        else:
            addr = ln(name, 'newaddr', addresstype='bech32')['bech32']
            ecli('sendtoaddress', addr, coins(fund_sat))
            mine(1, nodes=[name])
            print(f"funded {name} with {fund_sat} sat L-BTC")


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def start_ui(port):
    # node_id() caches ids for the life of the process, so a lqui.py left
    # running from before this up/setup would hand out node ids that no
    # longer match the nodes' (possibly regenerated) HSM seeds, and `connect`
    # fails with "wrong key". Always replace it with a fresh one.
    subprocess.run(['pkill', '-f', 'lqui.py'], check=False)
    wait_for(f"port {port} to free up", lambda: not _port_open(port), timeout=10)
    log_path = os.path.join(HERE, 'lqui.log')
    with open(log_path, 'a') as log:
        subprocess.Popen(
            [sys.executable, os.path.join(HERE, 'lqui.py'), '--net', 'regtest', '--port', str(port)],
            stdout=log, stderr=subprocess.STDOUT, cwd=HERE, start_new_session=True)
    wait_for('the web UI', lambda: _port_open(port))
    print(f"UI: http://127.0.0.1:{port}  (log: {log_path})")


def cmd_start(args):
    """up + setup + the web UI, in one call."""
    if TESTNET:
        raise Fail("start is for regtest; on testnet use `up`, `testnet-setup` and lqui.py by hand")
    cmd_up(args)
    cmd_setup(args)
    if not args.no_ui:
        start_ui(args.ui_port)


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
    follow_asset(state['asset'])

    for node in NODES:
        wait_for(node, lambda: ln(node, 'getinfo'))
        addr = ln(node, 'newaddr', addresstype='bech32')['bech32']
        ecli('sendtoaddress', addr, coins(args.fund_sat))
        print(f"{node:6} {node_id(node)}  funded with {args.fund_sat} sat L-BTC")
    mine(1)

    for a, b in zip(NODES, NODES[1:]):
        connect(a, b)
        print(f"connected {a} -> {b}")

    # bob is always set up as a swap provider between L-BTC and the asset
    # (both directions), and every other node as its payer -- so a swap
    # just works once the matching channels exist (see swap-demo, or open
    # them by hand), with no extra `swap-provider`/`swap-payer` step.
    provider = 'bob'
    for from_asset, to_asset in (('lbtc', state['asset']), (state['asset'], 'lbtc')):
        cmd_swap_provider(argparse.Namespace(node=provider, from_asset=from_asset, to_asset=to_asset,
                                             rate_ppm=1_000_000, fee_base_msat=500))
    for payer in NODES:
        if payer != provider:
            cmd_swap_payer(argparse.Namespace(node=payer, provider=provider))

    print(f"\nasset: {state['asset']}\nL-BTC: {lbtc_asset()}")


def cmd_mine(args):
    print(f"block height {mine(args.blocks)}")


def cmd_open_lbtc(args):
    connect(args.frm, args.to)
    res = ln(args.frm, 'fundchannel', id=node_id(args.to), amount=args.amount,
             announce=args.announce)
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
               announce=args.announce, asset=asset, fee_reserve=args.fee_reserve)
    addr = start['funding_address']

    # 2. elementsd builds the funding tx: the asset to the channel at output 0,
    #    the L-BTC fee reserve to the same address at output 1.  Explicit
    #    inputs, so nothing needs blinding.
    x_in = explicit_utxo(asset, args.units)
    change = getattr(args, 'change', 100000)
    l_in = explicit_utxo(lbtc, args.fee_reserve + fee + change)
    mine(1)
    psbt = ecli('createpsbt', [x_in, l_in],
                [{addr: coins(args.units), 'asset': asset},
                 {addr: coins(args.fee_reserve)},
                 {unconfidential_address(): coins(change)},
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
    for node in all_nodes():
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


def cmd_graph(args):
    """What a node's gossip map (listchannels) holds: one line per direction."""
    chans = sorted(ln(args.node, 'listchannels')['channels'],
                   key=lambda c: (c['short_channel_id'], c['direction']))
    print(f"{'scid':10} {'from':6} {'to':6} {'asset':16} {'public':6} {'capacity':>12}  fee base/ppm")
    for c in chans:
        print(f"{c['short_channel_id']:10} {node_name(c['source']):6} "
              f"{node_name(c['destination']):6} {asset_label(c.get('liquid_asset')):16} "
              f"{str(c['public']):6} {c['amount_msat'] // 1000:>12}  "
              f"{c['base_fee_millisatoshi']}/{c['fee_per_millionth']}")


def cmd_xpay(args):
    """Invoice on `to`, paid by xpay on `frm`: routes come from gossip, so
    the channels must be announced (open-* --announce)."""
    asset = default_asset(args)
    label = f"lnpoc-{int(time.time() * 1000)}"
    inv = ln(args.to, 'invoice', amount_msat=args.amount_msat, label=label,
             description=f"lnpoc {asset_label(asset)}", asset=asset)
    res = ln(args.frm, 'xpay', invstring=inv['bolt11'], asset=asset)
    print(f"xpay {args.frm} -> {args.to}: {res['amount_msat']} msat in {asset_label(asset)}, "
          f"sent {res['amount_sent_msat']} (fees included), {res['successful_parts']} part(s)")


# ---------------------------------------------------------- swap commands
# CROSS_ASSET_SWAP.md: pay an invoice in an asset you don't hold, by
# routing through a swap-provider node that holds channels in both.
# assetswap-plugin and pricetiers-plugin are both built into the `cln`
# image (Dockerfile's rust-plugins-builder stage) and loaded on every node
# from startup, so there's no separate build/load step here -- only their
# per-pair/per-provider config.
def cmd_swap_provider(args):
    """Configure node as a swap provider for from_asset -> to_asset, at
    rate_ppm (msat of from_asset per 1_000_000 msat worth of to_asset) plus
    a flat fee_base_msat.  The pricing itself is pricetiers-plugin's
    config, not assetswap-plugin's -- see CROSS_ASSET_SWAP.md.  Persistent
    (survives a restart): see add_config_lines()'s docstring for why that
    matters here."""
    from_asset, to_asset = args.from_asset, args.to_asset
    add_config_lines(args.node, [
        f'pricetiers-pair={from_asset}:{to_asset}:{args.rate_ppm}:{args.fee_base_msat}',
    ])
    print(f"{args.node}: swap-provider {asset_label(from_asset)} -> {asset_label(to_asset)} "
          f"at {args.rate_ppm} ppm (+{args.fee_base_msat} msat base fee)")


def cmd_swap_payer(args):
    """Configure node to use provider as its swap provider.  Persistent,
    same as swap-provider (not strictly required on the payer side, but
    harmless and keeps the two commands symmetric)."""
    provider_id = node_id(args.provider)
    add_config_lines(args.node, [f'assetswap-provider={provider_id}'])
    print(f"{args.node}: will use {args.provider} ({provider_id}) as a swap provider")


def cmd_assetinvoice(args):
    """Create an invoice on node requiring asset, wrapped with the asset
    into one self-contained string (assetinvoice RPC)."""
    asset = args.asset
    label = args.label or f"lnpoc-swap-{int(time.time() * 1000)}"
    res = ln(args.node, 'assetinvoice', amount_msat=args.amount_msat, label=label,
             description=f"lnpoc swap {asset_label(asset)}", asset=asset)
    print(res['assetinvoice'])
    return res


def cmd_payassetinvoice(args):
    """Pay an assetinvoice string from node, swapping via a configured
    provider if node doesn't hold the required asset directly."""
    res = ln(args.node, 'payassetinvoice', assetinvoice=args.assetinvoice, tier=args.tier)
    print(f"paid {res['amount_msat']} msat, preimage {res['payment_preimage']}")
    return res


def cmd_swap(args):
    """Invoice on `to` requiring asset, paid by `frm` via payassetinvoice --
    the swap half of pay/xpay's pattern: creates the invoice and pays it in
    one call, swapping through a configured provider if needed."""
    asset = args.asset
    label = args.label or f"lnpoc-swap-{int(time.time() * 1000)}"
    inv = ln(args.to, 'assetinvoice', amount_msat=args.amount_msat, label=label,
             description=f"lnpoc swap {asset_label(asset)}", asset=asset)
    print(f"invoiced {args.amount_msat} msat of {asset_label(asset)} on {args.to}, paying from {args.frm} ...")
    res = ln(args.frm, 'payassetinvoice', assetinvoice=inv['assetinvoice'], tier=args.tier)
    print(f"swapped: {args.frm} -> {args.to}, {res['amount_msat']} msat sent, "
          f"preimage {res['payment_preimage']}")


def cmd_swap_demo(args):
    """Whole story in one go: open alice-bob in L-BTC and bob-carol in the
    asset `setup` issued, make bob a swap provider bridging the two, and
    have alice pay a carol invoice in that asset while only holding L-BTC."""
    if not load_state().get('asset'):
        cmd_setup(argparse.Namespace(asset_amount=1000, fund_sat=10_000_000))
    asset = load_state()['asset']

    try:
        channel_between('alice', 'bob', None)
    except Fail:
        cmd_open_lbtc(argparse.Namespace(frm='alice', to='bob', amount=args.units, announce=False))
    try:
        channel_between('bob', 'carol', asset)
    except Fail:
        cmd_open_asset(argparse.Namespace(frm='bob', to='carol', units=args.units,
                                          fee_reserve=100_000, change=100_000,
                                          asset=asset, lbtc=False, announce=False))

    cmd_swap_provider(argparse.Namespace(node='bob', from_asset='lbtc', to_asset=asset,
                                         rate_ppm=args.rate_ppm, fee_base_msat=500))
    cmd_swap_payer(argparse.Namespace(node='alice', provider='bob'))

    # Any of the restarts above (add_config_lines) drop peer connections;
    # the quote protocol needs alice and bob directly connected.
    connect('alice', 'bob')

    print(f"\nswapping {args.pay_units} units of L-BTC -> asset({asset[:8]}) ...")
    cmd_swap(argparse.Namespace(frm='alice', to='carol', amount_msat=args.pay_units * 1000,
                                asset=asset, tier='medium', label=None))
    cmd_balances(args)


def cmd_close(args):
    asset = default_asset(args)
    c = channel_between(args.frm, args.to, asset)
    before = set(ecli('getrawmempool', wallet=False))
    res = ln(args.frm, 'close', id=c['channel_id'])
    if TESTNET:
        # The mempool is shared with the world: use the txid close gave us
        # and wait until it's mined.
        txid = res.get('txid') or res['txids'][0]
        wait_for('close tx', lambda: ecli('getrawtransaction', txid, True,
                                          wallet=False, check=False).get('confirmations')
                 if isinstance(ecli('getrawtransaction', txid, True, wallet=False, check=False), dict)
                 else None, timeout=900)
    else:
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


def print_outputs(tx, lbtc):
    for v in tx['vout']:
        dest = v['scriptPubKey'].get('address') or v['scriptPubKey'].get('type')
        asset_out = None if v.get('asset') == lbtc else v.get('asset')
        print(f"  {asset_label(asset_out):16} {int(Decimal(str(v['value'])) * COIN):>12}  {dest}")


def mempool_spend_of(txid):
    for t in ecli('getrawmempool', wallet=False):
        tx = ecli('getrawtransaction', t, True, wallet=False)
        if any(vin.get('txid') == txid for vin in tx['vin']):
            return tx
    return None


def cmd_force_close(args):
    """Unilateral close by `frm` while `to` is offline, then both sides sweep
    their outputs: `to` right away (its to_remote), `frm` after its CSV delay
    (to_local).  In an asset channel, assets go where a mutual close would
    pay them (`--to-elements`: an elementsd address for `frm`); fees are
    L-BTC, from the opener's fee reserve change or the sweeper's wallet."""
    asset = default_asset(args)
    lbtc = lbtc_asset()
    c = channel_between(args.frm, args.to, asset)
    others = [n for n in all_nodes() if n != args.to]
    params = {'id': c['channel_id'], 'unilateraltimeout': 1}
    if args.to_elements:
        params['destination'] = unconfidential_address()

    print(f"stopping {args.to}, so {args.frm}'s close goes unilateral ...")
    run(COMPOSE + ['stop', args.to])
    ln(args.frm, 'close', **params)
    commit = wait_for('commitment tx', lambda: mempool_spend_of(c['funding_txid']))
    mine(1, others)
    spends = ', '.join('%s:%d' % (i['txid'][:8], i['vout']) for i in commit['vin'])
    print(f"{args.frm}'s commitment tx {commit['txid']} (spends {spends}):")
    print_outputs(commit, lbtc)

    print(f"starting {args.to}: it sweeps its to_remote now ...")
    run(COMPOSE + ['start', args.to])
    wait_for(args.to, lambda: ln(args.to, 'getinfo'))
    sweep = wait_for(f"{args.to}'s sweep", lambda: mempool_spend_of(commit['txid']))
    mine(1)
    print(f"{args.to}'s sweep {sweep['txid']}:")
    print_outputs(sweep, lbtc)

    delay = c['their_to_self_delay']
    print(f"mining {delay} blocks for {args.frm}'s to_local CSV delay ...")
    mine(delay - 1)
    sweep = wait_for(f"{args.frm}'s sweep", lambda: mempool_spend_of(commit['txid']))
    mine(1)
    print(f"{args.frm}'s sweep {sweep['txid']}:")
    print_outputs(sweep, lbtc)


def cmd_demo(args):
    """Whole story in one go: two kinds of channels on each hop, a payment in
    each asset, then cooperative closes."""
    if not load_state().get('asset'):
        cmd_setup(argparse.Namespace(asset_amount=1000, fund_sat=10_000_000))
    for a, b in zip(NODES, NODES[1:]):
        cmd_open_lbtc(argparse.Namespace(frm=a, to=b, amount=5_000_000, announce=False))
        cmd_open_asset(argparse.Namespace(frm=a, to=b, units=5_000_000, fee_reserve=100_000,
                                          asset=None, lbtc=False, announce=False))
    cmd_channels(args)
    for lbtc in (False, True):
        cmd_pay(argparse.Namespace(frm='alice', to='carol', amount_msat=1_000_000_000,
                                   asset=None, lbtc=lbtc))
    cmd_balances(args)
    for a, b in zip(NODES, NODES[1:]):
        for lbtc in (False, True):
            cmd_close(argparse.Namespace(frm=a, to=b, asset=None, lbtc=lbtc))


def cmd_demo_gossip(args):
    """Like demo, but every channel is announced: alice learns the paths
    from gossip (one network per asset) and pays with xpay."""
    if not load_state().get('asset'):
        cmd_setup(argparse.Namespace(asset_amount=1000, fund_sat=10_000_000))
    for a, b in zip(NODES, NODES[1:]):
        cmd_open_lbtc(argparse.Namespace(frm=a, to=b, amount=5_000_000, announce=True))
        cmd_open_asset(argparse.Namespace(frm=a, to=b, units=5_000_000, fee_reserve=100_000,
                                          asset=None, lbtc=False, announce=True))
    # Both directions of all four channels, as alice's gossip has them.
    wait_for("alice's gossip", lambda: sum(c['public'] for c in
                                           ln('alice', 'listchannels')['channels']) >= 8,
             timeout=300)
    cmd_graph(argparse.Namespace(node='alice'))
    for lbtc in (False, True):
        cmd_xpay(argparse.Namespace(frm='alice', to='carol', amount_msat=1_000_000_000,
                                    asset=None, lbtc=lbtc))
    cmd_balances(args)
    for a, b in zip(NODES, NODES[1:]):
        for lbtc in (False, True):
            cmd_close(argparse.Namespace(frm=a, to=b, asset=None, lbtc=lbtc))


# ------------------------------------------------------ Liquid testnet only
FAUCET = 'https://liquidtestnet.com/faucet'


def faucet(address, action):
    """Ask the public testnet faucet (action: lbtc, test or amp) for coins."""
    import re
    import urllib.parse
    import urllib.request
    url = FAUCET + '?' + urllib.parse.urlencode({'address': address, 'action': action})
    with urllib.request.urlopen(url, timeout=60) as r:
        text = re.sub(r'<[^>]*>', ' ', r.read().decode())
    return ' '.join(text.split())


def wallet_balances():
    return ecli('getbalance')


def cmd_testnet_setup(args):
    """Wallet, faucet coins, our own asset, fund the nodes, connect A-B-C-D."""
    if not TESTNET:
        raise Fail("run this with LNPOC_NET=testnet")
    wait_for('elementsd', lambda: ecli('getblockchaininfo', wallet=False))
    info = ecli('getblockchaininfo', wallet=False)
    if info.get('initialblockdownload') or info['blocks'] < info['headers'] - 2:
        raise Fail(f"elementsd is still syncing: block {info['blocks']} of {info['headers']}")
    if WALLET not in ecli('listwallets', wallet=False):
        ecli('loadwallet', WALLET, wallet=False, check=False)
    if WALLET not in ecli('listwallets', wallet=False):
        ecli('createwallet', WALLET, wallet=False)

    state = load_state()
    if not state.get('address'):
        state['address'] = unconfidential_address()
        save_state(state)
    print(f"elementsd wallet address (unconfidential): {state['address']}")

    lbtc = lbtc_asset()
    if not args.no_faucet and not wallet_balances().get('bitcoin'):
        print("faucet:", faucet(state['address'], 'lbtc')[-300:])
        if args.faucet_asset:
            print("faucet (test asset):", faucet(state['address'], 'test')[-300:])
    wait_for('faucet L-BTC (1 confirmation)',
             lambda: ecli('getbalance', minconf=1).get('bitcoin'), timeout=900)
    print("wallet balances:", json.dumps(wallet_balances()))

    if not state.get('asset'):
        issued = ecli('issueasset', args.asset_amount, 0, False)
        state['asset'] = issued['asset']
        save_state(state)
        print(f"issued {args.asset_amount} of our own asset {state['asset']}")
        mine(1, nodes=[])
    follow_asset(state['asset'])

    for node in NODES:
        wait_for(node, lambda: ln(node, 'getinfo'))
        addr = ln(node, 'newaddr', addresstype='bech32')['bech32']
        ecli('sendtoaddress', addr, coins(args.fund_sat))
        print(f"{node:6} {node_id(node)}  funded with {args.fund_sat} sat L-BTC")
    mine(1)
    for a, b in zip(NODES, NODES[1:]):
        connect(a, b)
        print(f"connected {a} -> {b}")
    print(f"\nasset: {state['asset']}\nL-BTC: {lbtc}")


def cmd_testnet_demo(args):
    """A -> B -> C -> D with our own asset: three asset channels, one payment
    from alice to dave across all of them."""
    if not TESTNET:
        raise Fail("run this with LNPOC_NET=testnet")
    asset = load_state().get('asset')
    if not asset:
        raise Fail("run testnet-setup first")
    for a, b in zip(NODES, NODES[1:]):
        try:
            channel_between(a, b, asset)
            print(f"{a} -> {b}: channel exists")
            continue
        except Fail:
            pass
        cmd_open_asset(argparse.Namespace(frm=a, to=b, units=args.units,
                                          fee_reserve=args.fee_reserve, change=args.change,
                                          asset=asset, lbtc=False, announce=False))
    cmd_channels(args)
    cmd_pay(argparse.Namespace(frm='alice', to='dave', amount_msat=args.pay_units * 1000,
                               asset=asset, lbtc=False))
    cmd_channels(args)


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
    sp = add('add-node', cmd_add_node, 'build and start one more Core Lightning node')
    sp.add_argument('name', help='lowercase letters/digits, e.g. "dave2"')
    sp.add_argument('--fund-sat', type=int, default=10_000_000, help='L-BTC to seed its wallet with (0: skip)')
    sp = add('setup', cmd_setup, 'wallet, blocks, test asset, fund nodes, connect them')
    sp.add_argument('--asset-amount', type=int, default=1000, help='asset to issue (whole units)')
    sp.add_argument('--fund-sat', type=int, default=10_000_000, help='L-BTC per node wallet')
    sp = add('start', cmd_start, 'up + setup + the web UI, all in one (regtest only)')
    sp.add_argument('--asset-amount', type=int, default=1000, help='asset to issue (whole units)')
    sp.add_argument('--fund-sat', type=int, default=10_000_000, help='L-BTC per node wallet')
    sp.add_argument('--ui-port', type=int, default=8765)
    sp.add_argument('--no-ui', action='store_true', help="don't start lqui.py")
    sp = add('mine', cmd_mine, 'mine blocks')
    sp.add_argument('blocks', type=int, nargs='?', default=1)
    sp = add('open-lbtc', cmd_open_lbtc, 'open an ordinary L-BTC channel')
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('amount', type=int, help='sats')
    sp.add_argument('--announce', action='store_true', help='announce it (gossip)')
    sp = add('open-asset', cmd_open_asset, 'open an asset channel (asset + L-BTC fee reserve)')
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('units', type=int, help='asset base units')
    sp.add_argument('--fee-reserve', type=int, default=100_000, help='L-BTC sats')
    sp.add_argument('--change', type=int, default=100_000,
                    help='L-BTC sats of the funding input that go back to elementsd')
    sp.add_argument('--asset', help='asset id (default: the one `setup` issued)')
    sp.add_argument('--announce', action='store_true',
                    help="announce it, on the asset's own gossip network")
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
    sp = add('force-close', cmd_force_close,
             'unilaterally close a channel (peer offline), mine until both sides swept',
             asset=True)
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('--to-elements', action='store_true',
                    help="send frm's swept asset to an elementsd address")
    sp = add('graph', cmd_graph, "a node's gossip map (listchannels)")
    sp.add_argument('node', choices=NODES)
    sp = add('xpay', cmd_xpay, 'invoice on `to`, pay it from `frm` with xpay (needs announced channels)',
             asset=True)
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('amount_msat', type=int)
    sp = add('swap-provider', cmd_swap_provider,
             'configure node as a swap provider for from_asset -> to_asset')
    sp.add_argument('node', choices=NODES)
    sp.add_argument('from_asset', help="asset id, or 'lbtc'")
    sp.add_argument('to_asset', help="asset id, or 'lbtc'")
    sp.add_argument('rate_ppm', type=int,
                    help='msat of from_asset required per 1_000_000 msat worth of to_asset')
    sp.add_argument('--fee-base-msat', type=int, default=500)
    sp = add('swap-payer', cmd_swap_payer, 'configure node to use provider as its swap provider')
    sp.add_argument('node', choices=NODES)
    sp.add_argument('provider', choices=NODES)
    sp = add('assetinvoice', cmd_assetinvoice,
             'create an invoice requiring asset, wrapped for payassetinvoice/swap')
    sp.add_argument('node', choices=NODES)
    sp.add_argument('amount_msat', type=int)
    sp.add_argument('asset', help="asset id, or 'lbtc'")
    sp.add_argument('--label')
    sp = add('payassetinvoice', cmd_payassetinvoice,
             'pay an assetinvoice string, swapping via a configured provider if needed')
    sp.add_argument('node', choices=NODES)
    sp.add_argument('assetinvoice')
    sp.add_argument('--tier', choices=['low', 'medium', 'high'], default='medium')
    sp = add('swap', cmd_swap,
             'invoice on `to` requiring asset, pay it from `frm` (swapping via its provider if needed)')
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('amount_msat', type=int)
    sp.add_argument('asset', help="asset id, or 'lbtc'")
    sp.add_argument('--tier', choices=['low', 'medium', 'high'], default='medium')
    sp.add_argument('--label')
    sp = add('testnet-setup', cmd_testnet_setup,
             'Liquid testnet: wallet, faucet coins, our own asset, fund nodes, connect')
    sp.add_argument('--asset-amount', type=int, default=100, help='asset to issue (whole units)')
    sp.add_argument('--fund-sat', type=int, default=30_000, help='L-BTC per node wallet')
    sp.add_argument('--no-faucet', action='store_true', help="don't ask the faucet")
    sp.add_argument('--faucet-asset', action='store_true',
                    help='also ask the faucet for its test asset')
    sp = add('testnet-demo', cmd_testnet_demo, 'Liquid testnet: A-B-C-D asset channels, pay A -> D')
    sp.add_argument('--units', type=int, default=1_000_000, help='asset base units per channel')
    sp.add_argument('--fee-reserve', type=int, default=20_000, help='L-BTC sats per channel')
    sp.add_argument('--change', type=int, default=10_000, help='L-BTC sats back to elementsd')
    sp.add_argument('--pay-units', type=int, default=100_000, help='asset base units to pay')
    add('demo', cmd_demo, 'setup, open both kinds of channels, pay in both, close')
    add('demo-gossip', cmd_demo_gossip, 'like demo, with announced channels and xpay')
    sp = add('swap-demo', cmd_swap_demo,
             'L-BTC <-> asset swap end to end: open channels, set up a provider, swap, show balances')
    sp.add_argument('--units', type=int, default=5_000_000,
                    help='L-BTC channel size (sats) and asset channel size (asset base units)')
    sp.add_argument('--rate-ppm', type=int, default=1_000_000, help="bob's quoted rate (1:1 default)")
    sp.add_argument('--pay-units', type=int, default=1_000_000, help='asset base units to swap into')

    args = p.parse_args()
    try:
        args.fn(args)
    except Fail as e:
        sys.exit(f"error: {e}")


if __name__ == '__main__':
    main()
