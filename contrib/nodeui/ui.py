#!/usr/bin/env python3
"""nodeui: one web page for Core Lightning nodes on Bitcoin, Liquid (the
assetchan plugin) and EVM (the evmbackend plugin) networks.

    contrib/nodeui/ui.py --node alice=~/.lightning --node bob=/path/to/lightning-dir
    contrib/nodeui/ui.py --config nodes.json
    contrib/nodeui/ui.py --network liquid-regtest --elements-rpc http://u:p@127.0.0.1:18884
                                       # nodes.sh's A..D, as contrib/assetchan/ui.py did
    contrib/nodeui/ui.py ... --port 8765 --bitcoin-rpc http://u:p@127.0.0.1:18443

Then open http://127.0.0.1:8765 (`?demo` shows sample data).  For each node
the server works out once, and again whenever it comes back after being
down, what it is: `evm` if it has `evm-status`, `liquid` if it has
`assetchan-*` methods or runs on liquid*, else `bitcoin`.  Every node and
channel carries a network ID (btc:regtest, liquid:testnet/<asset>,
evm:<chain id>:<contract>/ETH); opens and payments only go between nodes
with the same one, and the server refuses anything else.

Actions run one at a time per network ID and side by side across networks;
a second one on a busy network waits its turn.  Their output shows in the
Activity panel.  On regtest the page mines the blocks an action waits for
(bitcoind or elementsd over RPC; anvil by moving time on, chain id 31337
only); elsewhere an open or deposit ends once sent or mined, and the page
shows the confirmations.
Python 3 and pyln-client only.  It listens on 127.0.0.1 only and spends
what the nodes hold: don't expose it.
"""
import argparse
import base64
import collections
import decimal
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pyln.client import LightningRpc, RpcError

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(HERE, 'ui')
HEX64 = re.compile(r'^[0-9a-f]{64}$')
NODE_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')
ETH_ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
BECH32 = re.compile(r'^([a-z]{2,4})1[02-9ac-hj-np-z]{6,100}$')
BASE58 = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{25,110}$')
LIQUID_ADDRESS = re.compile(r'^[A-Za-z0-9]{20,150}$')
BOLT11 = re.compile(r'^ln[a-z0-9]{1,30}1[02-9ac-hj-np-z]{50,2000}$')
TOKEN_HEADER = 'X-NodeUI-Token'

SAT_WEI = 10**10            # 1 sat = 1e10 wei (SPEC 4)
ETH_DECIMALS = 6            # ETH shows this many decimals at most (the tooltip has all 18)
ANVIL_CHAIN_ID = 31337

# Block explorers the page links to (none on regtest, liquid-regtest or anvil).
EVM_EXPLORERS = {1: 'https://eth.blockscout.com', 11155111: 'https://eth-sepolia.blockscout.com',
                 8453: 'https://base.blockscout.com', 84532: 'https://base-sepolia.blockscout.com',
                 10: 'https://optimism.blockscout.com', 11155420: 'https://optimism-sepolia.blockscout.com'}
CHAIN_EXPLORERS = {'bitcoin': 'https://mempool.space', 'testnet': 'https://mempool.space/testnet',
                   'testnet4': 'https://mempool.space/testnet4', 'signet': 'https://mempool.space/signet',
                   'liquid': 'https://blockstream.info/liquid',
                   'liquid-testnet': 'https://blockstream.info/liquidtestnet'}
ADDRESS = re.compile(r'^[0-9A-Za-z]{20,100}$')
WALLET_LINKS = 4            # most wallet addresses a node card lists
TXINFO_RETRY = 60           # s between evm-txinfo tries for a tx not yet indexed
PAY_RETRY_FOR = 600         # s xpay keeps trying where nobody mines (block heights differ between nodes)
# Infura credits per call, as contrib/evmchan/smoke_base_sepolia.py prices them.
CREDITS = {'eth_getLogs': 255, 'eth_estimateGas': 300, 'eth_chainId': 5}
CREDITS_DEFAULT = 80
FREE_TIER_PER_DAY = 3_000_000
MAX_AMOUNT = 10**15

# network -> (bolt11/bech32 prefix, genesis block hash)
BTC_NETWORKS = {
    'bitcoin': ('bc', '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'),
    'testnet': ('tb', '000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943'),
    'testnet4': ('tb', '00000000da84f2bafbbc53dee25a72ae507ff4914b867c565be350b0da8bf043'),
    'signet': ('tbs', '00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6'),
    'regtest': ('bcrt', '0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206'),
}
# network -> (bolt11 prefix, short name)
LIQUID_NETWORKS = {'liquid': ('ex', 'mainnet'), 'liquid-testnet': ('tex', 'testnet'),
                   'liquid-regtest': ('ert', 'regtest')}
ALL_NETWORKS = list(BTC_NETWORKS) + list(LIQUID_NETWORKS)
BTC_ADDRESS_HRP = {'bitcoin': 'bc', 'testnet': 'tb', 'testnet4': 'tb', 'signet': 'tb', 'regtest': 'bcrt'}
DEFAULT_BTC_RPC_PORT = {'bitcoin': 8332, 'testnet': 18332, 'testnet4': 48332, 'signet': 38332,
                        'regtest': 18443, 'liquid-regtest': 18884}

GONE_STATES = ('ONCHAIN', 'CLOSED', 'FUNDING_SPEND_SEEN')
CLOSED_STATES = ('ONCHAIN', 'CLOSED')


class Fail(Exception):
    """An action failed: the message is shown as is (scrubbed)."""


class BadRequest(Exception):
    pass


class Busy(Exception):
    pass


# ---------------------------------------------------------------- secrets
class Scrubber:
    """Removes secrets (RPC URLs, passwords, API keys) and anything that
    looks like a URL from text that leaves the server."""
    URL = re.compile(r'[a-z][a-z0-9+.-]*://[^\s\'"<>]*', re.I)
    USERINFO = re.compile(r'[^\s:/@\'"]+:[^\s/@\'"]+@[\w.-]+')
    API_KEY = re.compile(r'(/v[0-9]+/)[A-Za-z0-9_-]{16,}')      # infura.io/v3/<key> and the like

    def __init__(self):
        self.secrets = set()
        self.lock = threading.Lock()

    # Words the state is full of: a password equal to one (say, rpcpassword=regtest)
    # would turn every 'btc:regtest' into 'btc:<secret>'. In a URL it is still
    # caught by URL and USERINFO.
    NOT_SECRETS = {'bitcoin', 'mainnet', 'testnet', 'testnet4', 'signet', 'regtest', 'liquid',
                   'liquid-testnet', 'liquid-regtest'}

    def add(self, *values):
        with self.lock:
            for v in values:
                if isinstance(v, str) and len(v) >= 4 and v.lower() not in self.NOT_SECRETS:
                    self.secrets.update({v, urllib.parse.unquote(v), urllib.parse.quote(v, safe='')})

    def text(self, s):
        s = str(s)
        with self.lock:
            known = sorted(self.secrets, key=len, reverse=True)
        for k in known:
            s = s.replace(k, '<secret>')
        s = self.URL.sub('<url>', s)
        s = self.API_KEY.sub(r'\1<secret>', s)
        return self.USERINFO.sub('<secret>', s)

    def deep(self, obj):
        if isinstance(obj, str):
            return self.text(obj) if obj else obj
        if isinstance(obj, dict):
            return {k: self.deep(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.deep(v) for v in obj]
        return obj


# ---------------------------------------------------------------- helpers
def config_value(cfg, name, default=None):
    """A lightningd option's value from listconfigs' configs."""
    opt = (cfg or {}).get(name, {})
    for k in ('value_int', 'value_msat', 'value_str', 'value_bool', 'values_str', 'values_int'):
        if k in opt:
            return opt[k]
    return default


def rpc_password(cfg):
    """bitcoin-rpcpassword: listconfigs conceals it as "...", but says where
    it was set: read it back from that config file (as assetchan.py does)."""
    opt = (cfg or {}).get('bitcoin-rpcpassword', {})
    if opt.get('value_str') not in (None, '...'):
        return opt['value_str']
    path, _, linenum = opt.get('source', '').rpartition(':')
    if not path or not linenum.isdigit() or not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            line = f.read().splitlines()[int(linenum) - 1]
    except (OSError, IndexError):
        return None
    return line.split('=', 1)[1].strip() if '=' in line else None


def msat(v):
    """An _msat field (int, Millisatoshi or '123msat') in msat."""
    if v is None:
        return 0
    if isinstance(v, str):
        v = v[:-4] if v.endswith('msat') else v
    return int(v)


def n(x):
    return '{:,}'.format(int(x))


def eth_exact(wei):
    """Every one of the 18 decimals that is there: 0.015997549833998572."""
    s = format(decimal.Decimal(int(wei)).scaleb(-18).normalize(), 'f')
    return s if s not in ('-0', '') else '0'


def eth_text(wei):
    """At most ETH_DECIMALS decimals, trimmed, '≈' in front when that rounded."""
    exact = decimal.Decimal(int(wei)).scaleb(-18)
    q = exact.quantize(decimal.Decimal(1).scaleb(-ETH_DECIMALS), rounding=decimal.ROUND_HALF_UP)
    s = format(q.normalize(), 'f')
    return ('≈' if q != exact else '') + (s if s != '-0' else '0')


def fmt_eth(wei):
    """ETH as ETH and sat: 1 sat = 1e10 wei."""
    if wei is None:
        return None
    wei = int(wei)
    sat, rest = divmod(wei, SAT_WEI)
    return '{} ETH · {}{} sat'.format(eth_text(wei), '~' if rest else '', n(sat))


def fmt_eth_exact(wei):
    """The tooltip of an ETH amount: exact, and in wei."""
    if wei is None:
        return None
    return '{} ETH = {} wei'.format(eth_exact(wei), n(wei))


def fmt_units(units, units_per_sat, label=None):
    """A token amount in base units and sat (units_per_sat units = 1 sat);
    once, when a unit is a sat."""
    if units is None:
        return None
    units, ups = int(units), max(1, int(units_per_sat or 1))
    what = '{} units'.format(label) if label else 'units'
    if ups == 1:
        return '{} {}'.format(n(units), what)
    sat, rest = divmod(units, ups)
    return '{} {} · {}{} sat'.format(n(units), what, '~' if rest else '', n(sat))


def eth_to_wei(v):
    try:
        d = decimal.Decimal(str(v))
        if not d.is_finite() or d < 0:
            return None
        return int(d * 10**18)
    except (decimal.InvalidOperation, ValueError):
        return None


def credits(calls):
    return sum(int(c) * CREDITS.get(m, CREDITS_DEFAULT) for m, c in (calls or {}).items())


def detect_kind(methods, network):
    if 'evm-status' in methods:
        return 'evm'
    if any(m.startswith('assetchan-') for m in methods) or (network or '').startswith('liquid'):
        return 'liquid'
    return 'bitcoin'


def node_net_id(kind, network, chain_id=None, contract=None):
    if kind == 'evm':
        return 'evm:{}:{}'.format('?' if chain_id is None else chain_id, contract or '?')
    if kind == 'liquid':
        return 'liquid:' + LIQUID_NETWORKS.get(network, (None, network))[1]
    return 'btc:' + network


def short_address(a):
    return a if len(a) <= 12 else '{}…{}'.format(a[:6], a[-4:])


def short_net_id(net):
    """evm:84532:0xB820…940E/ETH for evm:84532:0xB8200000…0000940E/ETH."""
    if not net or not net.startswith('evm:'):
        return net
    base, sep, suffix = net.partition('/')
    parts = base.split(':', 2)
    if len(parts) == 3:
        parts[2] = short_address(parts[2])
    return ':'.join(parts) + sep + suffix


def net_title(kind, network, net, chain_id=None, contract=None, token=None):
    """The tooltip of a network badge."""
    if kind == 'bitcoin':
        g = BTC_NETWORKS.get(network, (None, None))[1]
        return 'Bitcoin {}{}'.format(network, ', chain {}…{}'.format(g[:8], g[-8:]) if g else '')
    if kind == 'liquid':
        return 'Liquid ({})'.format(network)
    return 'EVM chain {}, contract {}{}'.format(chain_id, contract, ', token {}'.format(token) if token else ', ETH')


# ---------------------------------------------------------------- JSON-RPC to bitcoind, elementsd, anvil
class JsonRpc:
    """http://user:password@host:port[/wallet/w]; never says the URL."""

    def __init__(self, url, wallet='', name='rpc', scrub=None, version='1.0', timeout=60):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ('http', 'https'):
            raise ValueError('not an http URL')
        self.url = '{}://{}{}'.format(u.scheme, u.netloc.rpartition('@')[2], u.path).rstrip('/')
        if wallet:
            self.url += '/wallet/' + urllib.parse.quote(wallet, safe='')
        self.headers = {'Content-Type': 'application/json'}
        if u.username is not None:
            auth = '{}:{}'.format(urllib.parse.unquote(u.username or ''), urllib.parse.unquote(u.password or ''))
            self.headers['Authorization'] = 'Basic ' + base64.b64encode(auth.encode()).decode()
        self.name, self.version, self.timeout = name, version, timeout
        self.scrub = scrub or Scrubber()
        self.scrub.add(url, self.url, u.password)
        self.next_id = 0
        self.lock = threading.Lock()

    def call(self, method, *params):
        with self.lock:
            self.next_id += 1
            req = {'jsonrpc': self.version, 'id': self.next_id, 'method': method, 'params': list(params)}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, json.dumps(req).encode(), self.headers),
                                        timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()     # an RPC error is an HTTP 500 with the usual body
            if not body:
                raise Fail('{} {}: HTTP {}'.format(self.name, method, e.code))
        except OSError as e:
            reason = getattr(e, 'reason', None) or e
            raise Fail(self.scrub.text('{} {}: not reachable ({})'.format(
                self.name, method, getattr(reason, 'strerror', None) or type(reason).__name__)))
        try:
            resp = json.loads(body.decode(), parse_float=decimal.Decimal)
        except ValueError:
            raise Fail('{} {}: unexpected reply'.format(self.name, method))
        err = resp.get('error')
        if err:
            msg = err.get('message', err) if isinstance(err, dict) else err
            raise Fail(self.scrub.text('{} {}: {}'.format(self.name, method, msg)))
        return resp.get('result')


class Miner:
    """Mines on one network ID: n blocks (bitcoind, elementsd) or n virtual
    blocks (anvil)."""
    what = 'blocks'

    def mine(self, n):
        raise NotImplementedError


class CoreMiner(Miner):
    """bitcoind or elementsd on regtest: to its wallet, or anyone-can-spend."""

    def __init__(self, rpc):
        self.rpc = rpc
        self.checked = False

    def check(self):
        """Refuses to mine or fund unless the chain is a regtest."""
        if not self.checked:
            chain = (self.rpc.call('getblockchaininfo') or {}).get('chain', '')
            if 'regtest' not in chain:
                raise Fail('refusing to mine: {} is on {}, not regtest'.format(self.rpc.name, chain))
            self.checked = True

    def mine(self, n):
        self.check()
        try:
            addr = self.rpc.call('getnewaddress')
        except Fail:
            addr = None
        if addr:
            self.rpc.call('generatetoaddress', n, addr)
        else:
            self.rpc.call('generatetodescriptor', n, 'raw(51)')


ANVIL_LOCK = threading.Lock()   # one for every anvil: two contracts on one anvil are two network IDs


class AnvilMiner(Miner):
    """anvil (chain id 31337 only): each block moves time on by INTERVAL, so
    it is one more virtual block; one extra block so `safe` covers them."""

    def __init__(self, rpc, interval):
        self.rpc, self.interval = rpc, max(1, int(interval or 1))
        self.lock = ANVIL_LOCK
        self.checked = False

    def mine1(self):
        for _ in range(10):
            ts = int(self.rpc.call('eth_getBlockByNumber', 'latest', False)['timestamp'], 16) + self.interval
            try:
                self.rpc.call('evm_setNextBlockTimestamp', hex(ts))
                self.rpc.call('evm_mine')
                return
            except Fail:
                continue        # a transaction got mined in between: try again
        raise Fail('anvil: cannot mine')

    def mine(self, n):
        with self.lock:
            if not self.checked:
                if int(self.rpc.call('eth_chainId'), 16) != ANVIL_CHAIN_ID:
                    raise Fail('refusing to mine: the chain is not anvil (31337)')
                self.checked = True
            for _ in range(n + 1):
                self.mine1()


# ---------------------------------------------------------------- nodes
class Node:
    """One lightningd: how to reach it and what it was detected as."""

    def __init__(self, name, ldir, network=None, factory=None):
        if not NODE_NAME.match(name or ''):
            raise ValueError('node names are 1-32 of A-Z a-z 0-9 _ . -')
        self.name, self.dir, self.network_hint = name, os.path.expanduser(ldir or ''), network
        self.factory = factory or LightningRpc
        self.detected = False
        self.kind = self.network = self.net = None
        self.methods = frozenset()
        self.cfg, self.info, self.evm = {}, {}, {}
        self.allowed = []
        self.status_cached = True       # evm-status takes cached=true (not on the ERC-20 branch)

    def rpc_path(self):
        if self.network_hint:
            return os.path.join(self.dir, self.network_hint, 'lightning-rpc')
        for p in [os.path.join(self.dir, 'lightning-rpc')] + \
                [os.path.join(self.dir, net, 'lightning-rpc') for net in ALL_NETWORKS]:
            if os.path.exists(p):
                return p
        return os.path.join(self.dir, 'bitcoin', 'lightning-rpc')

    def call(self, method, **params):
        params = {k: v for k, v in params.items() if v is not None}
        try:
            return self.factory(self.rpc_path()).call(method, params)
        except RpcError as e:
            err = e.error
            msg = err.get('message', err) if isinstance(err, dict) else err
            raise Fail('{}: {}: {}'.format(self.name, method, msg))
        except OSError as e:
            raise Fail('{}: not reachable ({})'.format(self.name, e.strerror or type(e).__name__))
        except Fail:
            raise
        except Exception as e:      # noqa: B902 - a broken reply: say what, not the payload
            raise Fail('{}: {}: {}'.format(self.name, method, type(e).__name__))

    def config(self, name, default=None):
        return config_value(self.call('listconfigs')['configs'], name, default)

    def detect(self):
        """Works out the kind and network ID; raises Fail if the node is down.
        An EVM node whose evm-status fails stays undetected (tried again)."""
        helps = self.call('help').get('help', [])
        methods = frozenset((h.get('command') or '').split(' ')[0] for h in helps)
        info = self.call('getinfo')
        cfg = self.call('listconfigs').get('configs', {})
        network = info.get('network', '')
        kind = detect_kind(methods, network)
        self.methods, self.info, self.cfg, self.kind, self.network = methods, info, cfg, kind, network
        self.status_cached = True
        complete = True
        if kind == 'evm':
            try:
                st = self.call('evm-status')
                self.evm = {'chain_id': st.get('chain_id'), 'contract': st.get('contract'),
                            'token': st.get('token'), 'interval': st.get('interval'),
                            'units_per_sat': st.get('units_per_sat'), 'eth_address': st.get('eth_address')}
            except Fail:
                self.evm = {'chain_id': None, 'contract': config_value(cfg, 'evm-contract')}
                complete = False
            self.net = node_net_id(kind, network, self.evm.get('chain_id'), self.evm.get('contract'))
        else:
            self.net = node_net_id(kind, network)
        self.allowed = [a.lower() for a in (config_value(cfg, 'assetchan-asset', []) or [])] \
            if kind == 'liquid' else []
        self.detected = complete
        return complete


# ---------------------------------------------------------------- the app
class App:
    """Nodes, the poller that keeps the state, the miners and the jobs."""
    POLL = 3            # seconds between polls while someone looks
    IDLE = 90           # stop polling this long after the last view
    GOSSIP_EVERY = 30
    NODE_TIMEOUT = 25
    REFRESH_EVERY = 30  # a node's Refresh button: one fresh evm-status read per 30 s at most
    DEPOSIT_KEEP = 6 * 3600     # how long a deposit's progress shows, at most

    def __init__(self, nodes, labels=None, bitcoin_rpc=None, elements_rpc=None, elements_wallet=None,
                 evm_status_every=300, env=None):
        self.nodes = collections.OrderedDict((nd.name, nd) for nd in nodes)
        self.labels = {k.lower(): v for k, v in (labels or {}).items()}
        self.scrub = Scrubber()
        env = os.environ if env is None else env
        self.env = env
        self.scrub.add(env.get('INFURA_API_KEY'), bitcoin_rpc, elements_rpc)
        self.bitcoin_rpc, self.elements_rpc = bitcoin_rpc, elements_rpc
        self.elements_wallet = elements_wallet
        self.evm_status_every = evm_status_every
        self.miners = {}            # network ID -> Miner, or None
        self.cache = {}             # (node, what) -> (time, value)
        self.chans = {}             # node -> {channel_id: listpeerchannels entry}
        self.up = {}                # node -> answered the last poll
        self.state = None
        self.lock = threading.Lock()
        self.viewed = time.time()
        self.wake = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=max(4, 2 * len(self.nodes)))
        self.inflight = {}          # node -> its snapshot future
        self.cleared_at = 0.0       # refresh_soon: gossip and elementsd read before this are stale
        self.flock = threading.Lock()
        self.fresh = set()          # nodes whose next snapshot reads evm-status afresh
        self.fresh_at = {}          # node -> when its last fresh read was asked for
        self.deposits = {}          # node -> [deposits mined on the EVM chain, not yet confirmed in CLN]
        self.txinfo = {}            # (network ID, funding txid) -> evm-txinfo
        self.txinfo_tried = {}      # (network ID, funding txid) -> when evm-txinfo last failed
        self.jobs = Jobs(self)

    # ------------------------------------------------ labels
    def asset_label(self, asset):
        if asset in (None, '', 'lbtc', 'bitcoin'):
            return 'L-BTC'
        return self.labels.get(asset.lower(), asset[:8])

    def token_label(self, token):
        if not token:
            return 'ETH'
        return self.labels.get(token.lower(), token[:8])

    def channel_net(self, node, asset=None):
        if node.kind == 'liquid':
            return '{}/{}'.format(node.net, self.asset_label(asset))
        if node.kind == 'evm':
            return '{}/{}'.format(node.net, self.token_label(node.evm.get('token')))
        return node.net

    # ------------------------------------------------ miners
    def register(self, node):
        """After detection: secrets from its options, and its network's miner."""
        cfg = node.cfg
        for k in ('evm-rpc', 'assetchan-elements-rpc', 'bitcoin-rpcpassword'):
            self.scrub.add(config_value(cfg, k))
        self.scrub.add(rpc_password(cfg))
        if node.net in self.miners and self.miners[node.net] is not None:
            return
        try:
            self.miners[node.net] = self.make_miner(node)
        except (Fail, ValueError) as e:
            print('{}: no mining: {}'.format(node.name, self.scrub.text(e)), file=sys.stderr)
            self.miners[node.net] = None

    def make_miner(self, node):
        cfg = node.cfg
        if node.kind == 'bitcoin' and node.network == 'regtest':
            url = self.bitcoin_rpc or self.core_rpc_url(cfg, 'regtest')
            if url:
                return CoreMiner(JsonRpc(url, name='bitcoind', scrub=self.scrub))
            cookie = self.cookie_url(cfg)
            return CoreMiner(JsonRpc(cookie, name='bitcoind', scrub=self.scrub)) if cookie else None
        if node.kind == 'liquid' and node.network == 'liquid-regtest':
            url = self.elements_rpc or config_value(cfg, 'assetchan-elements-rpc') \
                or self.core_rpc_url(cfg, 'liquid-regtest')
            if not url:
                return None
            wallet = self.elements_wallet if self.elements_wallet is not None \
                else (config_value(cfg, 'assetchan-elements-wallet') or '')
            return CoreMiner(JsonRpc(url, wallet, name='elementsd', scrub=self.scrub))
        if node.kind == 'evm' and node.evm.get('chain_id') == ANVIL_CHAIN_ID:
            url = config_value(cfg, 'evm-rpc') or ''
            if '${INFURA_API_KEY}' in url:
                key = self.env.get('INFURA_API_KEY')
                if not key:
                    return None
                url = url.replace('${INFURA_API_KEY}', key)
            self.scrub.add(url)
            return AnvilMiner(JsonRpc(url, name='anvil', scrub=self.scrub, version='2.0'),
                              node.evm.get('interval'))
        return None

    @staticmethod
    def core_rpc_url(cfg, network):
        user, password = config_value(cfg, 'bitcoin-rpcuser'), rpc_password(cfg)
        if not (user and password):
            return None
        host = config_value(cfg, 'bitcoin-rpcconnect') or '127.0.0.1'
        port = config_value(cfg, 'bitcoin-rpcport') or DEFAULT_BTC_RPC_PORT.get(network)
        return 'http://{}:{}@{}:{}'.format(urllib.parse.quote(str(user), safe=''),
                                           urllib.parse.quote(str(password), safe=''), host, port)

    def cookie_url(self, cfg):
        datadir = config_value(cfg, 'bitcoin-datadir') or os.path.expanduser('~/.bitcoin')
        try:
            with open(os.path.join(datadir, 'regtest', '.cookie')) as f:
                user, _, password = f.read().strip().partition(':')
        except OSError:
            return None
        self.scrub.add(password)
        host = config_value(cfg, 'bitcoin-rpcconnect') or '127.0.0.1'
        port = config_value(cfg, 'bitcoin-rpcport') or 18443
        return 'http://{}:{}@{}:{}'.format(urllib.parse.quote(user, safe=''),
                                           urllib.parse.quote(password, safe=''), host, port)

    # ------------------------------------------------ polling
    def cached(self, node, what, every, fn, force=False):
        """A value younger than `every` s, read after the last refresh_soon."""
        key = (node.name, what)
        hit = self.cache.get(key)
        if hit and not force and time.time() - hit[0] < every and hit[0] >= self.cleared_at:
            return hit[1]
        at = time.time()            # before the read: one in flight across refresh_soon is stale
        value = fn()
        self.cache[key] = (at, value)
        return value

    def evm_every(self, node):
        return self.POLL if node.evm.get('chain_id') == ANVIL_CHAIN_ID else self.evm_status_every

    def read_evm_status(self, node, fresh=False):
        """(evm-status, when it was read): from the cache while it is younger
        than evm_every. Off anvil a poll asks for cached=true (no fresh
        balance reads, SPEC 5.5), and falls back to a plain call if the
        plugin takes no such parameter; fresh (Refresh, a job's end) asks
        for a plain one."""
        key = (node.name, 'evm')
        hit = self.cache.get(key)
        if hit and not fresh and time.time() - hit[0] < self.evm_every(node):
            return hit[1], hit[0]
        st = None
        if not fresh and node.status_cached and node.evm.get('chain_id') != ANVIL_CHAIN_ID:
            try:
                st = node.call('evm-status', cached=True)
            except Fail:
                st = node.call('evm-status')        # raises if the node itself is the problem
                node.status_cached = False          # an evm-status without `cached` (the ERC-20 branch)
        if st is None:
            st = node.call('evm-status')
        at = time.time()
        self.cache[key] = (at, st)
        return st, at

    def refresh_node(self, name):
        """The Refresh button: the node's next poll reads evm-status afresh,
        now. Returns 0, or the seconds to wait (one per REFRESH_EVERY)."""
        now = time.time()
        with self.flock:
            wait = self._wait_locked(name, now)
            if wait:
                return wait
            self.fresh_at[name] = now
            self.fresh.add(name)
        self.wake.set()
        return 0

    def _wait_locked(self, name, now):
        """Seconds before the next Refresh of `name` (0: now); the caller holds flock."""
        return max(0, math.ceil(self.REFRESH_EVERY - (now - self.fresh_at.get(name, 0))))

    def refresh_wait(self, name, now=None):
        with self.flock:
            return self._wait_locked(name, time.time() if now is None else now)

    def track_deposit(self, node, txid, height, sats):
        with self.flock:
            self.deposits.setdefault(node, []).append({'txid': txid, 'height': height, 'sat': sats,
                                                       'at': time.time()})

    def deposits_of(self, node, outputs, now=None):
        """The node's tracked deposits CLN hasn't confirmed yet (each with
        whether its wallet lists it); drops the confirmed and the old ones."""
        now = time.time() if now is None else now
        status = {o.get('txid'): o.get('status') for o in outputs}
        with self.flock:
            keep = [d for d in self.deposits.get(node, [])
                    if status.get(d['txid']) != 'confirmed' and now - d['at'] < self.DEPOSIT_KEEP]
            if keep:
                self.deposits[node] = keep
            else:
                self.deposits.pop(node, None)
            return [dict(d, in_wallet=status.get(d['txid']) == 'unconfirmed') for d in keep]

    def snapshot(self, node):
        """What the page shows of one node; {'error': ...} when it is down."""
        try:
            if not node.detected:
                node.detect()
                self.register(node)
            info = node.call('getinfo')
            if (info.get('id'), info.get('network')) != (node.info.get('id'), node.info.get('network')):
                node.detect()           # restarted as another node, or on another network
                self.register(node)
            s = {'info': info, 'funds': node.call('listfunds'),
                 'peers': node.call('listpeers')['peers'],
                 'chans': node.call('listpeerchannels')['channels']}
        except Fail as e:
            node.detected = False       # detect again when it is back
            return {'error': self.scrub.text(e)}
        if node.kind == 'liquid':
            try:
                s['elements'] = self.cached(node, 'elements', self.POLL,
                                            lambda: node.call('assetchan-funds', channels=False)['elements'])
            except Fail as e:
                s['elements'], s['elements_error'] = {}, self.scrub.text(e)
            if 'assetchan-sweeps' in node.methods:
                try:
                    s['sweeps'] = node.call('assetchan-sweeps')['sweeps']
                except Fail:
                    s['sweeps'] = []
        if node.kind == 'evm':
            with self.flock:
                fresh = node.name in self.fresh
                self.fresh.discard(node.name)
            try:
                s['evm'], s['evm_read_at'] = self.read_evm_status(node, fresh)
                if (s['evm'].get('chain_id'), s['evm'].get('contract')) != \
                        (node.evm.get('chain_id'), node.evm.get('contract')):
                    node.detect()       # moved to another chain or contract
                    self.register(node)
            except Fail as e:
                if fresh:
                    with self.flock:
                        self.fresh.add(node.name)       # try the fresh read again next poll
                s['evm_error'] = self.scrub.text(e)
                hit = self.cache.get((node.name, 'evm'))
                s['evm'], s['evm_read_at'] = (hit[1], hit[0]) if hit else (None, None)
        if node.kind == 'evm' and 'evm-txinfo' in node.methods:
            self.look_up_funding(node, s['chans'])
        try:
            if node.kind == 'liquid' and 'assetchan-listchannels' in node.methods:
                s['gossip'] = self.cached(node, 'gossip', self.GOSSIP_EVERY,
                                          lambda: node.call('assetchan-listchannels')['channels'])
            else:
                s['gossip'] = self.cached(node, 'gossip', self.GOSSIP_EVERY,
                                          lambda: node.call('listchannels')['channels'])
        except Fail:
            s['gossip'] = []
        return s

    def look_up_funding(self, node, chans):
        """evm-txinfo of each channel's funding tx, once it is indexed (one
        eth_getLogs each, remembered); a miss is tried again after a minute."""
        now = time.time()
        for c in chans:
            txid = c.get('funding_txid')
            key = (node.net, txid)
            with self.flock:
                if not txid or key in self.txinfo or now - self.txinfo_tried.get(key, 0) < TXINFO_RETRY:
                    continue
                self.txinfo_tried[key] = now
            try:
                info = node.call('evm-txinfo', txid=txid)
            except Fail:
                continue
            if info.get('evm_tx'):
                with self.flock:
                    self.txinfo[key] = info
                    self.txinfo_tried.pop(key, None)

    def poll_once(self):
        """Reads every node at once. A node that hangs keeps its one
        snapshot in flight (pyln has no timeout) and shows as not answering;
        it never gets a second one, so it can't take every worker."""
        for name, nd in self.nodes.items():
            f = self.inflight.get(name)
            if f is None or f.done():
                self.inflight[name] = self.pool.submit(self.snapshot, nd)
        wait(list(self.inflight.values()), timeout=self.NODE_TIMEOUT)
        snaps = {}
        for name, f in self.inflight.items():
            if not f.done():
                snaps[name] = {'error': '{}: not answering'.format(name)}
                continue
            try:
                snaps[name] = f.result()
            except Exception as e:      # noqa: B902 - one node must not break the page
                snaps[name] = {'error': '{}: {}'.format(name, type(e).__name__)}
        state = self.scrub.deep(build_state(self, snaps))
        # Our own table's explorer URLs, added after the scrub (which would blank them).
        state['explorers'] = {nd.net: explorer_of(nd) for nd in self.nodes.values() if nd.net and explorer_of(nd)}
        with self.lock:
            self.state = state
        return state

    def run_poller(self):
        while True:
            if self.state is None or time.time() - self.viewed < self.IDLE or self.jobs.running():
                try:
                    self.poll_once()
                except RuntimeError:        # the interpreter is exiting
                    return
                except Exception as e:      # noqa: B902
                    print('poll failed: {}'.format(self.scrub.text(e)), file=sys.stderr)
            self.wake.wait(self.POLL)
            self.wake.clear()

    def refresh_soon(self, nodes=()):
        """After a job: poll now, gossip and elementsd afresh, and evm-status
        afresh for the job's EVM nodes only (each read costs credits)."""
        now = time.time()
        self.cleared_at = now
        for key in list(self.cache):
            if key[1] != 'evm':
                self.cache.pop(key, None)
        with self.flock:
            for name in nodes:
                nd = self.nodes.get(name)
                if nd is not None and nd.kind == 'evm':
                    self.fresh.add(name)
                    self.fresh_at[name] = now
        self.wake.set()

    def current_state(self):
        self.viewed = time.time()
        with self.lock:
            st = dict(self.state) if self.state else None
        if st is not None:
            st['jobs'], st['history'] = self.jobs.current()
        return st


# ---------------------------------------------------------------- the state
def state_name(s):
    if s == 'CHANNELD_NORMAL':
        return 'open'
    if s == 'ONCHAIN':
        return 'onchain'
    if s == 'CLOSED':
        return 'closed'
    if s.startswith(('CHANNELD_AWAITING', 'OPENINGD', 'DUALOPEND')):
        return 'opening'
    return 'closing'


def explorer_of(nd):
    """The block explorer of a node's chain, or None."""
    if nd.kind == 'evm':
        return EVM_EXPLORERS.get(nd.evm.get('chain_id'))
    return CHAIN_EXPLORERS.get(nd.network)


def explorer_path(base, what, value):
    """/address/{value} or /tx/{value} on the explorer `base`, for a value
    that looks like one (an address, or a 32-byte hex hash), else None. Only
    the path goes into the state, which is scrubbed of anything like a URL;
    the page joins it to the network's entry in state['explorers']."""
    if not base or not isinstance(value, str):
        return None
    v = value[2:] if value.startswith('0x') else value
    ok = ADDRESS.match(value) if what == 'address' else re.fullmatch(r'[0-9a-fA-F]{64}', v)
    return '/{}/{}'.format(what, value) if ok else None


def node_links(nd, outs):
    """What to look a node up by in an explorer: on EVM its ETH address, the
    contract and the token; elsewhere the wallet addresses holding funds,
    largest first."""
    base = explorer_of(nd)
    if nd.kind == 'evm':
        out = []
        for label, value in (('ETH address', nd.evm.get('eth_address')), ('Contract', nd.evm.get('contract')),
                             ('Token', nd.evm.get('token'))):
            if value:
                out.append({'label': label, 'value': value, 'path': explorer_path(base, 'address', value)})
        return out, 0
    held = collections.Counter()
    for o in outs:
        if o.get('address'):
            held[o['address']] += msat(o.get('amount_msat')) // 1000
    top = held.most_common()
    return [{'label': 'Wallet', 'value': a, 'sat': v, 'path': explorer_path(base, 'address', a)}
            for a, v in top[:WALLET_LINKS]], max(0, len(top) - WALLET_LINKS)


def funding_link(app, nd, c):
    """The channel's funding tx and where to see it: the tx itself on Bitcoin
    and Liquid, the EVM tx that applied it on an EVM chain (evm-txinfo)."""
    txid = c.get('funding_txid')
    if not txid:
        return None
    out = {'txid': txid, 'outnum': c.get('funding_outnum'), 'path': None, 'title': 'funding tx ' + txid}
    base = explorer_of(nd)
    if nd.kind != 'evm':
        out['path'] = explorer_path(base, 'tx', txid)
        return out
    info = app.txinfo.get((nd.net, txid))
    if info and info.get('evm_tx'):
        out['path'] = explorer_path(base, 'tx', info['evm_tx'])
        out['evm_tx'] = info['evm_tx']
        out['title'] = 'funding tx {} (virtual height {}), applied by EVM tx {} in block {}'.format(
            txid, info.get('height'), info['evm_tx'], info.get('evm_block'))
    else:
        out['title'] += ' (not yet applied at the confirmation tag)'
    return out


def parse_evm_status(st, cfg, now=None, label=None):
    """evm-status as the page shows it: ETH and sat (exact in *_title, the
    tooltips), token units and sat, alarms and credit use. Token fields
    (ERC-20 mode) are optional; label names the token."""
    now = time.time() if now is None else now
    st = st or {}
    token = st.get('token')
    ups = int(st.get('units_per_sat') or SAT_WEI)
    gas = st.get('balance_wei')
    min_wei = eth_to_wei(config_value(cfg, 'evm-min-balance', '0.01'))
    if token:
        wd = st.get('withdrawable_units')
        wd_text, wd_sat = fmt_units(wd, ups, label), None if wd is None else int(wd) // ups
        wd_title = None if wd is None else '{} base units'.format(n(wd))
    else:
        wd = st.get('withdrawable_wei')
        wd_text, wd_sat = fmt_eth(wd), None if wd is None else int(wd) // SAT_WEI
        wd_title = fmt_eth_exact(wd)
    tb = st.get('token_balance_units')
    out = {
        'contract': st.get('contract'), 'chain_id': st.get('chain_id'), 'interval': st.get('interval'),
        'confirm': st.get('confirm'), 'eth_address': st.get('eth_address'),
        'token': token, 'units_per_sat': ups if token else None,
        'gas_wei': gas, 'gas_text': fmt_eth(gas), 'gas_title': fmt_eth_exact(gas),
        'min_balance_wei': min_wei, 'min_balance_text': fmt_eth(min_wei) if min_wei is not None else None,
        'low_gas': gas is not None and min_wei is not None and int(gas) < min_wei,
        'token_balance_units': tb, 'token_balance_text': fmt_units(tb, ups, label) if token else None,
        'withdrawable': wd, 'withdrawable_sat': wd_sat, 'withdrawable_text': wd_text,
        'withdrawable_title': wd_title, 'cached': bool(st.get('cached')),
        'balance_age_s': st.get('balance_age_s'), 'withdrawable_age_s': st.get('withdrawable_age_s'),
        'tip': st.get('tip'), 'current_height': st.get('current_height'), 'lag': st.get('lag'),
        'tag_age_s': st.get('tag_age_s'), 'tag_unchanged_s': st.get('tag_unchanged_s'),
        'checkpoint': st.get('checkpoint'), 'pending_sends': st.get('pending_sends') or [],
        'alarms': st.get('alarms') or {}, 'sync_error': st.get('sync_error'),
        'options_checked': st.get('options_checked'), 'option_problems': st.get('option_problems') or [],
        'opens_refused': st.get('opens_refused'),
        'errors': [st[k] for k in ('balance_error', 'withdrawable_error', 'token_balance_error') if st.get(k)],
        'anvil': st.get('chain_id') == ANVIL_CHAIN_ID,
    }
    calls, since = st.get('rpc_calls'), st.get('rpc_calls_since')
    if calls is not None and since is not None:
        secs = max(60.0, now - float(since))
        per_day = round(credits(calls) / secs * 86400)
        out.update({'rpc_calls_total': sum(int(c) for c in calls.values()), 'credits': credits(calls),
                    'credits_per_day': per_day,
                    'credits_pct': round(100.0 * per_day / FREE_TIER_PER_DAY, 1),
                    'credits_hours': round((now - float(since)) / 3600, 2)})
    return out


def emergency_reserve(node):
    """CLN's min-emergency-msat, in sat (default 25,000)."""
    try:
        return int(msat(config_value(node.cfg, 'min-emergency-msat', 25_000_000))) // 1000
    except (TypeError, ValueError):
        return 25_000


def reserve_text(reserve):
    return ('CLN keeps {} sat (min-emergency-msat) as an emergency reserve while it still knows anchor '
            'channels, until about 100 blocks after a close'.format(n(reserve)))


def funding_progress(app, nd, info, c, ids):
    """{'depth', 'need'} of an opening channel: the funding tx's depth (from
    its short channel id's block) against funding-confirms (the larger of
    both sides' when the peer is ours)."""
    need = int(config_value(nd.cfg, 'funding-confirms', 1) or 1)
    peer = app.nodes.get(ids.get(c.get('peer_id')))
    if peer is not None:
        need = max(need, int(config_value(peer.cfg, 'funding-confirms', 1) or 1))
    scid = c.get('short_channel_id') or ''
    block = scid.split('x')[0]
    depth = max(0, int(info.get('blockheight', 0)) - int(block) + 1) if block.isdigit() else 0
    return {'depth': depth, 'need': need}


def build_state(app, snaps):
    """The JSON the page renders, from each node's snapshot."""
    ids = {}            # node id -> page name
    for name, s in snaps.items():
        if 'info' in s:
            ids[s['info']['id']] = name
    nodes, chans, maps, assets = [], {}, {}, {}

    for name, nd in app.nodes.items():
        s = snaps[name]
        app.up[name] = 'info' in s
        base = {'name': name, 'kind': nd.kind, 'network': nd.network, 'net': nd.net,
                'net_short': short_net_id(nd.net), 'detected': nd.detected}
        if 'info' not in s:
            nodes.append(dict(base, ok=False, error=s.get('error'), id=nd.info.get('id', ''), peers=[]))
            continue
        info = s['info']
        outs = s['funds'].get('outputs', [])
        conf = sum(msat(o.get('amount_msat')) // 1000 for o in outs
                   if o.get('status') == 'confirmed' and not o.get('reserved'))
        unconf = sum(msat(o.get('amount_msat')) // 1000 for o in outs if o.get('status') == 'unconfirmed')
        wallet = {'confirmed': conf, 'unconfirmed': unconf, 'unit': 'L-BTC sat' if nd.kind == 'liquid' else 'sat'}
        reserve = emergency_reserve(nd)
        if s['chans'] and 0 < conf + unconf <= reserve:
            wallet['reserve'] = reserve
            wallet['reserve_hint'] = reserve_text(reserve)
        links, more = node_links(nd, outs)
        entry = dict(base, ok=True, id=info['id'], alias=info.get('alias', ''), links=links, links_more=more,
                     blockheight=info.get('blockheight', 0),
                     syncing=bool(info.get('warning_bitcoind_sync') or info.get('warning_lightningd_sync')),
                     wallet=wallet,
                     confirms=config_value(nd.cfg, 'funding-confirms', 1),
                     mining=app.miners.get(nd.net) is not None,
                     peers=[{'id': p['id'], 'name': ids.get(p['id'], p['id'][:10]),
                             'connected': bool(p.get('connected'))} for p in s['peers']])
        swap = swap_info(app, nd, ids)
        if swap:
            entry['swap'] = swap
        if nd.kind == 'liquid':
            elements = s.get('elements') or {}
            entry['elements'] = {app.asset_label(k) if not HEX64.match(k) else k: int(v)
                                 for k, v in elements.items()}
            entry['assets'] = {a: int(u) for a, u in elements.items() if HEX64.match(a)}
            entry['allowed_assets'] = list(nd.allowed)
            entry['fee_reserve'] = config_value(nd.cfg, 'assetchan-default-fee-reserve', 2000)
            if s.get('elements_error'):
                entry['elements_error'] = s['elements_error']
            for a in list(nd.allowed) + list(entry['assets']):
                assets.setdefault(a, app.asset_label(a))
            entry['sweeps'] = [dict(w, asset_label=app.asset_label(w.get('asset')))
                               for w in s.get('sweeps', [])]
        if nd.kind == 'evm':
            ev = s.get('evm')
            entry['evm'] = parse_evm_status(ev, nd.cfg, label=app.token_label(ev.get('token'))
                                            if ev.get('token') else None) if ev else None
            if s.get('evm_error'):
                entry['evm_error'] = s['evm_error']
            if entry['evm']:
                if entry['evm']['token']:
                    entry['evm']['token_label'] = app.token_label(entry['evm']['token'])
                if s.get('evm_read_at'):
                    entry['evm']['read_age_s'] = round(max(0.0, time.time() - s['evm_read_at']))
                entry['evm']['refresh_wait_s'] = app.refresh_wait(name)
                entry['evm']['poll_every_s'] = app.evm_every(nd)
            entry['deposits'] = [{'sat': d['sat'], 'txid': d['txid'], 'height': d['height'],
                                  'in_wallet': d['in_wallet']} for d in app.deposits_of(name, outs)]
        nodes.append(entry)

        # Every channel shows up on both of our nodes: keep the opener's view.
        for c in s['chans']:
            key = c.get('channel_id') or '{}:{}'.format(c['peer_id'], c.get('short_channel_id'))
            if key in chans and c.get('opener') != 'local':
                continue
            asset = c.get('asset') if nd.kind == 'liquid' else None
            if asset:
                assets.setdefault(asset, app.asset_label(asset))
            ours, total = msat(c.get('to_us_msat')) // 1000, msat(c.get('total_msat')) // 1000
            status = c.get('status') or []
            confirm = None
            if state_name(c.get('state', '')) == 'opening':
                confirm = funding_progress(app, nd, info, c, ids)
            chans[key] = {
                'id': key, 'net': app.channel_net(nd, asset), 'node_net': nd.net, 'kind': nd.kind,
                'a': name, 'b': ids.get(c['peer_id'], c['peer_id'][:10]), 'b_id': c['peer_id'],
                'b_ours': c['peer_id'] in ids, 'asset': asset, 'asset_label': app.asset_label(asset)
                if nd.kind == 'liquid' else (app.token_label(nd.evm.get('token')) if nd.kind == 'evm' else 'BTC'),
                'unit': 'units' if asset else 'sat',
                'state': state_name(c.get('state', '')), 'raw_state': c.get('state', ''),
                'status': status[-1] if status else '', 'a_units': ours, 'b_units': total - ours,
                'scid': c.get('short_channel_id'), 'opener': c.get('opener'),
                'private': bool(c.get('private')), 'peer_connected': bool(c.get('peer_connected')),
                'confirm': confirm, 'funding': funding_link(app, nd, c),
            }
        app.chans[name] = {c.get('channel_id'): c for c in s['chans'] if c.get('channel_id')}

    order = {'open': 0, 'opening': 1, 'closing': 2, 'onchain': 3, 'closed': 4}
    channels = sorted(chans.values(), key=lambda c: (order.get(c['state'], 9), c['net'], c['a'], c['b']))

    # Networks, and one map per network: gossip plus our own channels.
    networks = collections.OrderedDict()
    for nd in app.nodes.values():
        if not nd.net:
            continue
        e = networks.setdefault(nd.net, {
            'id': nd.net, 'short': short_net_id(nd.net), 'kind': nd.kind, 'network': nd.network,
            'title': net_title(nd.kind, nd.network, nd.net, nd.evm.get('chain_id'), nd.evm.get('contract'),
                               nd.evm.get('token')),
            'mining': app.miners.get(nd.net) is not None,
            'mine_what': 'virtual blocks (INTERVAL {} s each)'.format(nd.evm.get('interval'))
            if nd.kind == 'evm' else 'blocks',
            'nodes': [], 'block': 0, 'syncing': False})
        e['nodes'].append(nd.name)
        s = snaps[nd.name]
        if 'info' in s:
            e['block'] = max(e['block'], s['info'].get('blockheight', 0))
            e['syncing'] = e['syncing'] or bool(s['info'].get('warning_bitcoind_sync')
                                                or s['info'].get('warning_lightningd_sync'))
    for net in networks:
        maps[net] = build_map(app, net, snaps, ids, channels)
    return {'networks': list(networks.values()), 'nodes': nodes, 'channels': channels,
            'maps': maps, 'assets': assets, 'now': time.time()}


MAP_FOREIGN = 30        # most nodes that are not ours on one map


def build_map(app, net, snaps, ids, channels):
    names = {}          # node id -> label
    for name, nd in app.nodes.items():
        if nd.net == net and nd.info.get('id'):
            names[nd.info['id']] = name
    edges = {}
    for name, nd in app.nodes.items():
        s = snaps[name]
        if nd.net != net or 'gossip' not in s:
            continue
        for g in s['gossip']:
            scid = g.get('short_channel_id')
            if not scid or scid in edges:
                continue
            a, b = g.get('source'), g.get('destination')
            asset = g.get('asset') if nd.kind == 'liquid' else None
            edges[scid] = {'a': a, 'b': b, 'cap': msat(g.get('amount_msat')) // 1000, 'local': False,
                           'state': 'open', 'label': app.asset_label(None if asset == 'lbtc' else asset)
                           if nd.kind == 'liquid' else ''}
    for c in channels:
        if c['node_net'] != net or c['state'] == 'closed':
            continue
        a = app.nodes[c['a']].info.get('id')
        key = c['scid'] or c['id']
        edges[key] = {'a': a, 'b': c['b_id'], 'cap': c['a_units'] + c['b_units'], 'local': True,
                      'state': c['state'], 'label': c['asset_label'] if c['kind'] == 'liquid' else ''}
    # Keep the edges touching our nodes first, and at most MAP_FOREIGN others.
    ours = set(names)
    foreign = []
    kept = []
    for e in sorted(edges.values(), key=lambda e: not (e['a'] in ours or e['b'] in ours)):
        new = [x for x in (e['a'], e['b']) if x not in ours and x not in foreign]
        if len(foreign) + len(new) > MAP_FOREIGN:
            continue
        foreign += new
        kept.append(e)
    peers = {}
    for name in names.values():
        s = snaps[name]
        for p in s.get('peers', []):
            if p.get('connected'):
                peers.setdefault(name, []).append(p['id'])
    nodes = [{'key': k, 'name': v, 'ours': True} for k, v in names.items()] + \
            [{'key': k, 'name': k[:8], 'ours': False} for k in foreign]
    links = [{'a': a_id, 'b': p} for a_id, name in names.items() for p in peers.get(name, [])
             if p in names and a_id < p]
    return {'nodes': nodes, 'edges': kept, 'peers': links}


# ---------------------------------------------------------------- jobs
class Ctx:
    """What a job's command gets: say() to log, mine and wait on its network."""

    def __init__(self, app, net, say, result, cancelled=None):
        self.app, self.net, self.say, self.result = app, net, say, result
        self.cancelled = cancelled or threading.Event()

    @property
    def miner(self):
        return self.app.miners.get(self.net) if self.net else None

    def mine(self, blocks):
        if self.cancelled.is_set():
            raise Fail('cancelled')
        if self.miner is not None and blocks > 0:
            self.miner.mine(blocks)

    def wait_for(self, what, fn, mine_between=False, timeout=None, soft=False):
        """Polls fn() until it is true; mines a block per try when asked
        and possible. soft: on timeout say so and return None."""
        mining = self.miner is not None
        if timeout is None:
            timeout = 120 if mining else 30 * 60
        end = time.time() + timeout
        while True:
            if self.cancelled.is_set():
                raise Fail('cancelled while waiting for {}'.format(what))
            got = fn()
            if got:
                return got
            if time.time() > end:
                if soft:
                    self.say('still waiting for {}; mine more or check back later'.format(what))
                    return None
                raise Fail('timed out waiting for {}'.format(what))
            if mine_between:
                self.mine(1)
            self.cancelled.wait(1 if mining else 10)


class Jobs:
    """Jobs run one at a time per network ID, and on different networks side
    by side: they mine and wait for blocks. A job on a network (or naming a
    node) that has an earlier job waits for it, in order. A node has one
    network ID, so jobs on two networks never touch the same node; the node
    check covers a node that came back on another network meanwhile."""
    KEEP = 8            # finished jobs kept, with their logs
    MAX_WAITING = 3     # waiting jobs per network

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.active = []            # running and waiting jobs, oldest first
        self.history = collections.deque(maxlen=self.KEEP)     # finished, newest first
        self.counter = 0

    @staticmethod
    def clash(job, net, nodes):
        return job['net'] == net or bool(set(job['nodes']) & set(nodes))

    def running(self):
        """True while a job runs or waits."""
        with self.lock:
            return bool(self.active)

    @staticmethod
    def _view(job):
        return {'id': job['id'], 'title': job['title'], 'net': job['net'], 'nodes': list(job['nodes']),
                'status': job['status'], 'queued': job['queued'], 'started': job['started'],
                'ended': job['ended'], 'log': list(job['log']), 'result': dict(job['result']),
                'cancelling': job['cancel'].is_set()}

    def current(self):
        """(active jobs, oldest first, each waiting one with what it waits
        for; finished jobs, newest first)."""
        with self.lock:
            active = []
            for i, job in enumerate(self.active):
                v = self._view(job)
                if job['status'] == 'waiting':
                    v['waiting_for'] = [k['title'] for k in self.active[:i]
                                        if self.clash(k, job['net'], job['nodes'])]
                active.append(v)
            return active, [self._view(j) for j in self.history]

    def get(self, job_id):
        with self.lock:
            job = next((j for j in list(self.active) + list(self.history) if j['id'] == job_id), None)
            return self._view(job) if job else None

    def start(self, title, net, fn, nodes=(), wait=False):
        """Runs fn(ctx) now, or once the earlier jobs on its network (or
        nodes) end. Raises Busy if MAX_WAITING already wait there. wait:
        returns when it has ended."""
        nodes = tuple(nodes)
        with self.lock:
            ahead = [j for j in self.active if self.clash(j, net, nodes)]
            if sum(1 for j in ahead if j['status'] == 'waiting') >= self.MAX_WAITING:
                raise Busy()
            self.counter += 1
            job = {'id': self.counter, 'title': title, 'net': net, 'nodes': nodes, 'status': 'waiting',
                   'queued': time.time(), 'started': None, 'ended': None, 'log': [], 'result': {},
                   'fn': fn, 'cancel': threading.Event(), 'done': threading.Event()}
            self.active.append(job)
            if not ahead:
                self._launch(job)
        if wait:
            job['done'].wait()
        return job['id']

    def _launch(self, job):
        """The caller holds the lock. A thread that can't start ends the job
        (so its network isn't left blocked)."""
        job['status'], job['started'] = 'running', time.time()
        try:
            threading.Thread(target=self.run, args=(job,), daemon=True, name='job-{}'.format(job['id'])).start()
        except RuntimeError as e:
            job['log'].append('error: cannot start ({})'.format(e))
            job['ended'], job['status'], job['fn'] = time.time(), 'error', None
            self.active.remove(job)
            self.history.appendleft(job)
            job['done'].set()

    def _dispatch(self):
        """Starts every waiting job that no earlier job clashes with; the
        caller holds the lock."""
        for job in list(self.active):
            if job not in self.active or job['status'] != 'waiting':
                continue
            earlier = self.active[:self.active.index(job)]
            if not any(self.clash(k, job['net'], job['nodes']) for k in earlier):
                self._launch(job)

    def _finish(self, job, status):
        """The caller holds the lock."""
        job['ended'], job['status'] = time.time(), status
        job['fn'] = None
        self.active.remove(job)
        self.history.appendleft(job)
        self._dispatch()

    def cancel(self, job_id):
        """A waiting job is dropped; a running one stops at its next wait (an
        RPC in progress still finishes: lightningd can't be interrupted)."""
        with self.lock:
            job = next((j for j in self.active if j['id'] == job_id), None)
            if job is None:
                return False
            if job['status'] == 'waiting':
                job['log'].append('cancelled before it started')
                self._finish(job, 'cancelled')
                job['done'].set()
                return True
            if not job['cancel'].is_set():
                job['cancel'].set()
                job['log'].append('cancelling at the next wait…')
            return True

    def run(self, job):
        scrub = self.app.scrub.text

        def say(line):
            with self.lock:
                job['log'].extend(scrub(x) for x in str(line).split('\n'))
        status = 'error'
        try:
            for name in job['nodes']:
                nd = self.app.nodes.get(name)
                if nd is not None and nd.net != job['net']:
                    raise Fail('{} is on {} now, not {}: not run'.format(name, nd.net, job['net']))
            job['fn'](Ctx(self.app, job['net'], say, job['result'], job['cancel']))
            status = 'done'
        except Fail as e:
            say('error: {}'.format(e))
        except Exception as e:          # noqa: B902 - shown in the Activity panel, scrubbed
            say('error: {}: {}'.format(type(e).__name__, e))
        finally:                        # whatever happened, the network is free again
            if status == 'error' and job['cancel'].is_set():
                status = 'cancelled'
            with self.lock:
                self._finish(job, status)
            job['done'].set()
            self.app.refresh_soon(job['nodes'])


# ---------------------------------------------------------------- commands
def channel_of(node, channel_id):
    return next((c for c in node.call('listpeerchannels')['channels'] if c.get('channel_id') == channel_id), None)


def chan_state(node, channel_id):
    return (channel_of(node, channel_id) or {}).get('state')


def confirmed_outputs(node):
    return {(o['txid'], o['output']) for o in node.call('listfunds')['outputs'] if o.get('status') == 'confirmed'}


def peer_node(app, peer_id):
    return next((nd for nd in app.nodes.values() if nd.info.get('id') == peer_id), None)


def unit_name(asset):
    return 'units' if asset else 'sat'


def cmd_open(ctx, frm, to, amount, asset, announce):
    app = ctx.app
    a, b = app.nodes[frm], app.nodes[to]
    info = b.call('getinfo')
    binding = next((x for x in info.get('binding', []) if x.get('type') in ('ipv4', 'ipv6')), None)
    if binding is None:
        raise Fail('{} has no address to connect to'.format(to))
    a.call('connect', id=info['id'], host=binding['address'], port=binding['port'])
    if asset is None:
        res = a.call('fundchannel', id=info['id'], amount=amount, announce=announce)
        ctx.say('funding tx {}'.format(res['txid']))
    else:
        res = a.call('assetchan-open', id=info['id'], amount=amount, asset=asset, announce=announce)
        ctx.say('funding tx {}:{}, {} sat L-BTC fee reserve next to the asset'.format(
            res['txid'], res['outnum'], res['fee_reserve']))
    cid = res['channel_id']
    ctx.result['channel_id'] = cid
    if announce:
        ctx.say('announced: once it has 6 confirmations both nodes gossip it, and every node on {} can route '
                'payments through it'.format(app.channel_net(a, asset)))
    else:
        ctx.say('private: only {} and {} know it; another payer can use it only as the last hop, from an '
                'invoice\'s route hint, so multi-hop routes need announced channels'.format(frm, to))
    if ctx.miner is None:
        # Nobody mines here: the job ends; the Channels table shows the depth.
        need = int(a.config('funding-confirms', 1))
        try:
            need = max(need, int(b.config('funding-confirms', 1)))
        except Fail:
            pass
        ctx.say('{} channel {} -> {} funded with {} {}: it opens after {} confirmation{}; '
                'the Channels table shows the depth'.format(app.channel_net(a, asset), frm, to, n(amount),
                                                            unit_name(asset), need, '' if need == 1 else 's'))
        return
    ctx.mine(int(a.config('funding-confirms', 1)))
    ctx.wait_for('the channel to open', lambda: chan_state(a, cid) == 'CHANNELD_NORMAL'
                 and chan_state(b, cid) == 'CHANNELD_NORMAL', mine_between=True)
    c = channel_of(a, cid) or {}
    ctx.say('{} channel {} -> {} ready: {} {} ({})'.format(
        app.channel_net(a, asset), frm, to, n(amount), unit_name(asset), c.get('short_channel_id')))


NO_ROUTE = re.compile(r'no connection between source and destination|unknown destination|no path|'
                      r'no route|could not find a route', re.I)


def explain_no_route(ctx, node):
    """After a routing failure: what the payer knows of the network."""
    try:
        chans = node.call('listchannels')['channels']
        mine = [c for c in node.call('listpeerchannels')['channels'] if c.get('state') == 'CHANNELD_NORMAL']
    except Fail:
        return
    known = {c.get('short_channel_id') for c in chans}
    ctx.say('{} knows {} channel{} through gossip and has {} open channel{} of its own ({} private). A route can '
            'only use channels the payer knows: announced ones (gossiped about 6 confirmations after they open), '
            'its own, and the last hop from the invoice\'s route hint. Open the channels along the way with '
            '"Announce to the network" ticked and wait for gossip (the network map draws gossiped channels).'
            .format(node.name, len(known), '' if len(known) == 1 else 's', len(mine), '' if len(mine) == 1 else 's',
                    sum(1 for c in mine if c.get('private'))))


def settled_forwards(node):
    try:
        return [f for f in node.call('listforwards', status='settled')['forwards']]
    except Fail:
        return []


def pay_route(app, payer, before):
    """The page nodes a payment went through, in order: the settled forwards
    that appeared on the payer's network since `before` ({node: count}),
    chained by channel (a job holds the network, so they are this payment's)."""
    hops = []
    for nd in app.nodes.values():
        if nd.net == payer.net and nd.name != payer.name and app.up.get(nd.name):
            new = settled_forwards(nd)[before.get(nd.name, 0):]
            hops += [(nd.name, f.get('in_channel'), f.get('out_channel'), msat(f.get('fee_msat'))) for f in new]
    ours = {c.get('short_channel_id') for c in payer.call('listpeerchannels')['channels']}
    route, chan = [], None
    while hops:
        nxt = next((h for h in hops if (h[1] in ours if chan is None else h[1] == chan)), None)
        if nxt is None:
            break
        hops.remove(nxt)
        route.append(nxt)
        chan = nxt[2]
    return route


def pay_bolt11(ctx, node, bolt11, asset):
    # Where nobody mines, nodes' block heights differ by a block or more (an
    # EVM node is up to an evm-poll behind): a payee ahead of us rejects our
    # CLTV, and xpay waits for our height to catch up, but only for retry_for.
    retry = None if ctx.miner is not None else PAY_RETRY_FOR
    try:
        if node.kind == 'liquid':
            res = node.call('assetchan-pay', bolt11=bolt11, asset=asset or 'lbtc')
        elif 'xpay' in node.methods:
            res = node.call('xpay', invstring=bolt11, retry_for=retry)
        else:
            res = node.call('pay', bolt11=bolt11, retry_for=retry)
    except Fail as e:
        if NO_ROUTE.search(str(e)):
            ctx.say('error: {}'.format(e))
            explain_no_route(ctx, node)
        raise
    fee = (msat(res.get('amount_sent_msat')) - msat(res.get('amount_msat'))) // 1000
    ctx.say('paid: preimage {}, {} part(s), fee {} {}'.format(
        res.get('payment_preimage'), res.get('successful_parts', res.get('parts', 1)), n(fee),
        unit_name(asset)))


def private_hints(node, amount):
    """The channels to name in exposeprivatechannels, or None for CLN's own
    choice. When all of the payee's open channels are private, CLN leaves out
    every hint whose peer looks like a dead end (a peer with no announced
    channels), so a payer without gossip can't find the payee at all. Then
    every private channel that can receive the amount is named, which CLN
    always hints."""
    chans = [c for c in node.call('listpeerchannels').get('channels', [])
             if c.get('state') == 'CHANNELD_NORMAL' and c.get('short_channel_id') and c.get('peer_connected')]

    def can_take(c):
        return receivable_msat(c) >= amount * 1000
    # An announced channel that can take the amount: CLN's own choice works.
    # Otherwise (none announced, or all of them full on our side, as a
    # channel we just opened is) CLN would hint no private channel either.
    if any(not c.get('private') and can_take(c) for c in chans):
        return None
    return [c['short_channel_id'] for c in chans if c.get('private') and can_take(c)] or None


def receivable_msat(c):
    """What a channel can still receive (CLN's receivable_msat, or the peer's side)."""
    if c.get('receivable_msat') is not None:
        return msat(c['receivable_msat'])
    return msat(c.get('total_msat')) - msat(c.get('to_us_msat'))


def receivable_sat(node):
    """The most the node can receive in one channel, in sat."""
    return max([receivable_msat(c) // 1000 for c in node.call('listpeerchannels').get('channels', [])
                if c.get('state') == 'CHANNELD_NORMAL' and c.get('peer_connected')] or [0])


def make_invoice(ctx, node, amount, asset, description):
    """An invoice on node; says which private channels it hints, and CLN's
    warnings (warning_capacity, warning_deadends, ...)."""
    label = 'nodeui-{}'.format(time.time_ns())
    if node.kind == 'liquid':
        inv = node.call('assetchan-invoice', amount=amount, asset=asset or 'lbtc', label=label,
                        description=description)
    else:
        hints = private_hints(node, amount)
        inv = node.call('invoice', amount_msat=amount * 1000, label=label, description=description,
                        exposeprivatechannels=hints)
        if hints:
            ctx.say('{} can receive this only on private channels: the invoice hints {}'.format(
                node.name, ', '.join(hints)))
        elif node.kind != 'liquid':
            most = receivable_sat(node)
            if most < amount:
                ctx.say('{} can receive at most {} sat in one channel: a channel\'s funds start on the side that '
                        'opened it, so pay into {} first, or have a peer open a channel to it'.format(
                            node.name, n(most), node.name))
    for k in sorted(inv):
        if k.startswith('warning_'):
            ctx.say('CLN {}: {}'.format(k, inv[k]))
    return inv


def cmd_pay(ctx, frm, to, amount, asset):
    a, b = ctx.app.nodes[frm], ctx.app.nodes[to]
    inv = make_invoice(ctx, b, amount, asset, 'nodeui payment {} -> {}'.format(frm, to))
    ctx.say('invoice on {} for {} {}: {}…'.format(to, n(amount), unit_name(asset), inv['bolt11'][:40]))
    before = {nd.name: len(settled_forwards(nd)) for nd in ctx.app.nodes.values()
              if nd.net == a.net and nd.name != frm and ctx.app.up.get(nd.name)}
    pay_bolt11(ctx, a, inv['bolt11'], asset)
    route = pay_route(ctx.app, a, before)
    names = [frm] + [h[0] for h in route] + [to]
    ctx.say('route: {} ({} hop{}{})'.format(
        ' -> '.join(names), len(names) - 1, '' if len(names) == 2 else 's',
        ', forwarded by ' + ', '.join('{} over {} -> {} (fee {} msat)'.format(h[0], h[1], h[2], n(h[3]))
                                      for h in route) if route else ', direct'))


def cmd_pay_invoice(ctx, frm, bolt11, asset):
    pay_bolt11(ctx, ctx.app.nodes[frm], bolt11, asset)


def cmd_invoice(ctx, node, amount, asset, description):
    nd = ctx.app.nodes[node]
    inv = make_invoice(ctx, nd, amount, asset, description)
    net = ctx.app.channel_net(nd, asset)
    ctx.say('invoice on {} for {} {} ({}):'.format(node, n(amount), unit_name(asset), net))
    ctx.say(inv['bolt11'])
    ctx.result['bolt11'] = inv['bolt11']


# ---------------------------------------------------------------- swaps
# assetswap-plugin (plugins/assetswap-plugin): a payer pays a payee's
# invoice in another asset, or on another chain, through a swap provider.
SWAP_TIERS = ('low', 'medium', 'high')
SWAP_MARGINS = {'low': 2010, 'medium': 2030, 'high': 2060}    # pricetiers-plugin's, per mille


def native_asset(nd):
    """What assetswap-plugin calls the coin a node's channels are in."""
    return {'evm': 'eth', 'liquid': 'lbtc'}.get(nd.kind, 'btc')


def swap_info(app, nd, ids):
    """What a node can do in a swap (from its plugins, as detected): pay
    (payassetinvoice; through which provider), receive (assetinvoice), and
    as a provider which pairs it quotes and which page node pays leg 2 in
    each remote asset."""
    if 'assetinvoice' not in nd.methods:
        return None
    info = {'receive': True, 'pay': 'payassetinvoice' in nd.methods, 'native': native_asset(nd)}
    providers = config_value(nd.cfg, 'assetswap-provider', []) or []
    if providers:
        info['provider'] = ids.get(providers[0], providers[0][:10])
    pairs = []
    for v in config_value(nd.cfg, 'pricetiers-pair', []) or []:
        parts = v.split(':')
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            pairs.append({'from': parts[0], 'to': parts[1], 'rate_ppm': int(parts[2]), 'fee_base_msat': int(parts[3])})
    if pairs:
        info['pairs'] = pairs
    remotes = {}
    for v in config_value(nd.cfg, 'assetswap-remote', []) or []:
        asset, _, path = v.partition(':')
        other = next((o for o in app.nodes.values()
                      if os.path.realpath(o.rpc_path()) == os.path.realpath(path)), None)
        remotes[asset] = other.name if other else '(not on this page)'
    if remotes:
        info['remotes'] = remotes
    return info


def swap_asset_arg(body, key, nd):
    """An asset as assetswap-plugin names it: the node's native coin
    ('lbtc', 'eth', 'btc') or, on Liquid, an asset id the node allows."""
    a = body.get(key)
    if a in (None, ''):
        return native_asset(nd)
    if not isinstance(a, str):
        raise BadRequest('{}: not an asset'.format(key))
    a = a.lower()
    if a == native_asset(nd):
        return a
    if nd.kind == 'liquid' and HEX64.match(a) and a in nd.allowed:
        return a
    raise BadRequest('{}: {} takes {} or one of its assets'.format(key, nd.name, native_asset(nd)))


def swap_label(app, asset):
    return {'lbtc': 'L-BTC', 'eth': 'ETH', 'btc': 'BTC'}.get(asset) or app.asset_label(asset)


def swap_amount(app, asset, x):
    """'100 sat of ETH', '203 units of <asset>'."""
    return '{} {} of {}'.format(n(x), 'sat' if asset in ('lbtc', 'eth', 'btc') else 'units', swap_label(app, asset))


# assetswap-plugin's CLTV rules (plugins/assetswap-plugin: pay.rs LEG1_CLTV_MARGIN,
# config.rs defaults), for explaining a swap's route.
LEG1_CLTV_MARGIN = 60
CHAIN_NAMES = {'liquid': 'Liquid', 'evm': 'EVM', 'bitcoin': 'Bitcoin'}


def channel_coin(nd, c):
    """The coin a channel of nd is in, as assetswap-plugin names it."""
    return (c.get('asset') or native_asset(nd)).lower()


def swap_candidates(app, nd, asset, amount_msat, toward=None):
    """nd's channels, each with whether it could carry amount_msat in asset
    and, if not, why (what the route finder's asset layer and balances rule
    out). `toward`: the page node the payment is going to, if known."""
    out = []
    for c in nd.call('listpeerchannels').get('channels', []):
        peer = peer_node(app, c.get('peer_id'))
        coin, spend = channel_coin(nd, c), msat(c.get('spendable_msat')) // 1000
        if state_name(c.get('state', '')) != 'open':
            why = 'not open ({})'.format(state_name(c.get('state', '')))
        elif not c.get('peer_connected'):
            why = 'peer offline'
        elif coin != asset:
            why = 'in {}, not {}: the route finder keeps the payment off it'.format(
                swap_label(app, coin), swap_label(app, asset))
        elif spend * 1000 < amount_msat:
            why = 'only {} spendable, {} needed'.format(n(spend), n(amount_msat // 1000))
        else:
            why = None
        out.append({'peer': peer.name if peer else (c.get('peer_id') or '')[:10], 'scid': c.get('short_channel_id'),
                    'coin': swap_label(app, coin), 'spendable': spend, 'ok': why is None, 'why': why,
                    'toward': bool(toward and peer is not None and peer.name == toward)})
    return out


def forwards_before(app, net):
    return {nd.name: len(settled_forwards(nd)) for nd in app.nodes.values()
            if nd.net == net and app.up.get(nd.name)}


def leg_path(app, payer, dest, before, candidates):
    """A payment's hops, payer to dest, from the forwards it left on the
    page's nodes (their fees too); a direct one went over the channel that
    could carry it."""
    route = pay_route(app, payer, before)            # [(node, in channel, out channel, fee)]
    names = [payer.name] + [r[0] for r in route] + [dest]
    if route:
        scids = [route[0][1]] + [r[2] for r in route]
    else:
        scids = [next((c['scid'] for c in candidates if c['ok'] and c['peer'] == dest), None)]
    return {'path': names, 'hops': [{'from': names[i], 'to': names[i + 1], 'scid': scids[i]}
                                    for i in range(len(names) - 1)],
            'forwarders': [{'node': r[0], 'fee_msat': r[3]} for r in route]}


def cmd_swap(ctx, frm, to, to_asset, amount, from_asset, tier):
    app = ctx.app
    payer, payee = app.nodes[frm], app.nodes[to]
    from_l, to_l = swap_label(app, from_asset), swap_label(app, to_asset)
    tr = ctx.result['swap'] = {
        'kind': 'swap', 'payer': frm, 'payee': to, 'from_asset': from_asset, 'to_asset': to_asset,
        'from_label': from_l, 'to_label': to_l, 'from_unit': 'sat' if from_asset in ('lbtc', 'eth', 'btc') else 'units',
        'to_unit': 'sat' if to_asset in ('lbtc', 'eth', 'btc') else 'units', 'amount': amount, 'tier': tier,
        'ok': False, 'why': []}
    why = tr['why']
    try:
        _swap(ctx, app, tr, why, payer, payee, frm, to, to_asset, amount, from_asset, tier, from_l)
    except Fail as e:
        tr['error'] = app.scrub.text(e)
        raise


def _swap(ctx, app, tr, why, payer, payee, frm, to, to_asset, amount, from_asset, tier, from_l):
    to_l = tr['to_label']
    ctx.say('1. {} makes an invoice for {} (assetinvoice)'.format(to, swap_amount(app, to_asset, amount)))
    w = payee.call('assetinvoice', amount_msat=amount * 1000, label='nodeui-swap-{}'.format(time.time_ns()),
                   description='nodeui swap {} -> {}'.format(frm, to), asset=to_asset)['assetinvoice']
    wrapper = json.loads(base64.urlsafe_b64decode(w + '=' * (-len(w) % 4)))
    h = tr['hash'] = wrapper['payment_hash']
    ctx.say('   bolt11 {}…, payment hash {}'.format(wrapper['bolt11'][:30], h))
    try:
        final_cltv = payee.call('decode', string=wrapper['bolt11']).get('min_final_cltv_expiry')
    except Fail:
        final_cltv = None

    # The same coin on the same network: payassetinvoice pays it directly
    # (xpay), no swap and no provider.
    if from_asset == to_asset and payer.net == payee.net:
        tr['kind'] = 'direct'
        why.append('{} and {} are on the same network and {} wants {}, which {} holds: a plain payment, '
                   'no swap and no provider.'.format(frm, to, to, swap_label(app, to_asset), frm))
        cands = swap_candidates(app, payer, from_asset, amount * 1000)
        before = forwards_before(app, payer.net)
        ctx.say('2. {} pays it directly in {}: same coin, same network, no swap (payassetinvoice -> xpay)'.format(
            frm, from_l))
        res = payer.call('payassetinvoice', assetinvoice=w, from_asset=from_asset, tier=tier)
        if not res.get('payment_preimage'):
            raise Fail('{}: payassetinvoice: no preimage in its reply'.format(frm))
        sent = msat(res.get('amount_sent_msat', res.get('amount_msat'))) // 1000
        ctx.say('   paid: {} sent, preimage {}'.format(swap_amount(app, from_asset, sent), res['payment_preimage']))
        tr['leg1'] = dict(leg_path(app, payer, to, before, cands), candidates=cands, chain=payer.kind,
                          label=from_l, amount=sent)
        tr['paid'] = sent
        inv = payee.call('listinvoices', payment_hash=h)['invoices']
        ctx.say('3. {}\'s invoice: {}'.format(to, inv[0]['status'] if inv else '?'))
        tr['ok'] = bool(inv and inv[0]['status'] == 'paid')
        return

    prov_id = (config_value(payer.cfg, 'assetswap-provider', []) or [None])[0]
    prov = peer_node(app, prov_id) if prov_id else None
    if prov is None:
        raise Fail('{} has no swap provider on this page (assetswap-provider)'.format(frm))
    tr['provider'] = prov.name
    why.append('{} is set up to swap through {} (its assetswap-provider), so leg 1 goes to {} in {}.'.format(
        frm, prov.name, prov.name, from_l))
    remote = None
    for v in config_value(prov.cfg, 'assetswap-remote', []) or []:
        asset, _, path = v.partition(':')
        if asset == to_asset:
            remote = next((o for o in app.nodes.values()
                           if os.path.realpath(o.rpc_path()) == os.path.realpath(path)), None)
    leg2 = remote or prov
    tr['leg2_node'] = leg2.name
    if remote is not None:
        tr['bridge'] = {'from': prov.name, 'to': remote.name}
        why.append('{} can\'t pay {} itself ({} is on {}): its assetswap-remote for {} is {}, the same LSP\'s '
                   'node there, which pays leg 2 over {}\'s RPC socket.'.format(
                       prov.name, to_l, to, CHAIN_NAMES.get(payee.kind, payee.kind), to_asset if len(to_asset) < 20 else to_l, remote.name,
                       remote.name))
    else:
        why.append('{} pays leg 2 itself: it has channels in {}.'.format(prov.name, to_l))

    from_msat = None
    try:
        q = payer.call('pricetiers', to_asset=to_asset, to_amount_msat=amount * 1000)
        tiers = ', '.join('{} {}'.format(t['name'], n(t['from_amount_msat'] // 1000)) for t in q['tiers'])
        tr['tiers'] = [{'name': t['name'], 'amount': t['from_amount_msat'] // 1000} for t in q['tiers']]
        from_msat = next((t['from_amount_msat'] for t in q['tiers'] if t['name'] == tier), None)
        ctx.say('2. price in {} from {}\'s own pricetiers (the provider decides when leg 1 lands): {}'.format(
            swap_label(app, q['from_asset']), frm, tiers))
        why.append('Price: {} {} for {} {} at the {} tier ({}); {} accepts leg 1 if it pays at least its own '
                   'low tier.'.format(n((from_msat or 0) // 1000), tr['from_unit'], n(amount), tr['to_unit'], tier,
                                      tiers, prov.name))
    except Fail as e:
        ctx.say('2. no local price ({}): payassetinvoice will need one'.format(app.scrub.text(e)))

    if final_cltv is not None:
        safety = config_value(prov.cfg, 'assetswap-cltv-safety-blocks', 18)
        budget = config_value(prov.cfg, 'assetswap-min-hop-budget', 34)
        tr['cltv'] = {'final': final_cltv, 'leg1': final_cltv + LEG1_CLTV_MARGIN, 'safety': safety,
                      'budget': budget, 'need': final_cltv + safety + budget,
                      'leg2_max': final_cltv + LEG1_CLTV_MARGIN - safety}
        why.append('Timelocks: {} asks for {} blocks at the end; {} gives leg 1 {} (+{}); {} needs at least {} '
                   '({} + {} safety + {} hop budget) and lets leg 2 take at most {}, so leg 2 times out {} blocks '
                   'before leg 1.'.format(to, final_cltv, frm, final_cltv + LEG1_CLTV_MARGIN, LEG1_CLTV_MARGIN,
                                          prov.name, final_cltv + safety + budget, final_cltv, safety, budget,
                                          final_cltv + LEG1_CLTV_MARGIN - safety, safety)
                   + (' Caveat: leg 1 counts {} blocks and leg 2 {} blocks; the plugin compares them as raw counts, '
                      'safe only if a block on the {} chain is no longer than one on the {} chain.'.format(
                          CHAIN_NAMES[payer.kind], CHAIN_NAMES[leg2.kind], CHAIN_NAMES[leg2.kind],
                          CHAIN_NAMES[payer.kind]) if payer.kind != leg2.kind else ''))

    c1 = swap_candidates(app, payer, from_asset, from_msat or amount * 1000, prov.name)
    c2 = swap_candidates(app, leg2, to_asset, amount * 1000, to)
    for who, cands, dest, coin in ((frm, c1, prov.name, from_l), (leg2.name, c2, to, tr['to_label'])):
        direct = [c for c in cands if c['ok'] and c['toward']]
        via = sorted({c['peer'] for c in cands if c['ok'] and not c['toward']})
        out = sum(1 for c in cands if not c['ok'])
        parts = ['{} channel{} straight to {}'.format(len(direct), '' if len(direct) == 1 else 's', dest)]
        if via:
            parts.append('only via {} (if {} on)'.format(', '.join(via), 'it routes' if len(via) == 1 else 'they route'))
        if out:
            parts.append('{} ruled out'.format(out))
        why.append('{}\'s channels able to carry {} {}: {}.'.format(who, coin, 'to ' + dest, '; '.join(parts)))
    before1, before2 = forwards_before(app, payer.net), forwards_before(app, leg2.net)

    ctx.say('3. {} pays it in {} via provider {} (payassetinvoice, tier {})'.format(frm, from_l, prov.name, tier))
    res = payer.call('payassetinvoice', assetinvoice=w, from_asset=from_asset, tier=tier)
    # (waitsendpay's reply for a swap; xpay's, without a status, for a direct payment)
    if not res.get('payment_preimage') or res.get('status', 'complete') != 'complete':
        raise Fail('{}: payassetinvoice: {}'.format(frm, res.get('status') or 'no preimage in its reply'))
    sent = msat(res.get('amount_sent_msat', res.get('amount_msat'))) // 1000
    tr['paid'] = sent
    ctx.say('   leg 1: {} -> {}: {} sent, preimage {}'.format(frm, prov.name, swap_amount(app, from_asset, sent),
                                                           res.get('payment_preimage')))
    tr['leg1'] = dict(leg_path(app, payer, prov.name, before1, c1), candidates=c1, chain=payer.kind,
                      label=from_l, amount=sent)
    paid = None
    try:
        pays = leg2.call('listsendpays', payment_hash=h)['payments']
        done = [p for p in pays if p.get('status') == 'complete']
        if done:
            paid = sum(msat(p.get('amount_msat')) for p in done) // 1000
            ctx.say('   leg 2: {} -> {}: {} paid'.format(leg2.name, to, swap_amount(app, to_asset, paid)))
    except Fail as e:
        ctx.say('   leg 2: {}'.format(app.scrub.text(e)))
    tr['leg2'] = dict(leg_path(app, leg2, to, before2, c2), candidates=c2, chain=leg2.kind,
                      label=swap_label(app, to_asset), amount=paid)
    inv = payee.call('listinvoices', payment_hash=h)['invoices']
    ctx.say('4. {}\'s invoice: {}'.format(to, inv[0]['status'] if inv else '?'))
    tr['ok'] = bool(inv and inv[0]['status'] == 'paid')


def elements_assets(node):
    try:
        return {k: int(v) for k, v in node.call('assetchan-funds', channels=False)['elements'].items()}
    except Fail:
        return {}


def wallet_sat(node):
    """Confirmed sat in the node's wallet."""
    try:
        return sum(msat(o.get('amount_msat')) // 1000 for o in node.call('listfunds')['outputs']
                   if o.get('status') == 'confirmed' and not o.get('reserved'))
    except Fail:
        return 0


def say_evm_tx(ctx, nd, txid, what):
    """On an EVM chain: the EVM tx that applied a virtual-chain tx (the
    Activity log links 0x hashes to the explorer)."""
    if nd.kind != 'evm' or not txid or 'evm-txinfo' not in nd.methods:
        return
    try:
        info = nd.call('evm-txinfo', txid=txid)
    except Fail:
        ctx.say('{} {} is not at the confirmation tag yet: its channel row links it later'.format(what, txid))
        return
    ctx.say('{} {} was applied by EVM tx {} (block {}, virtual height {})'.format(
        what, txid, info.get('evm_tx'), n(info.get('evm_block') or 0), n(info.get('height') or 0)))


def cmd_close(ctx, node, channel_id, force):
    app = ctx.app
    a = app.nodes[node]
    c = channel_of(a, channel_id)
    if c is None:
        raise Fail('{} has no such channel'.format(node))
    peer = peer_node(app, c['peer_id'])
    before = elements_assets(a) if a.kind == 'liquid' else {}
    share = msat(c.get('to_us_msat')) // 1000
    wallet0 = wallet_sat(a)
    ctx.say('{}\'s share of the channel: {} {}'.format(node, n(share), 'units' if c.get('asset') else 'sat'))
    if not force:
        if a.kind == 'liquid':
            res = a.call('assetchan-close', id=channel_id)
        else:
            res = a.call('close', id=channel_id)
        if res.get('type') != 'mutual':
            ctx.say('close is {}'.format(res.get('type')))
        ctx.say('closing tx {}'.format(res.get('txid') or (res.get('txids') or [None])[0]))
        ctx.mine(1)
        ctx.wait_for('the close to confirm', lambda: chan_state(a, channel_id) in GONE_STATES + (None,),
                     mine_between=True)
        txid = res.get('txid') or (res.get('txids') or [None])[0]
        if a.kind != 'liquid' and ctx.miner is None and share:
            got = ctx.wait_for('{}\'s share to reach its wallet'.format(node),
                               lambda: wallet_sat(a) > wallet0, timeout=20 * 60, soft=True)
            if got:
                ctx.say('{}\'s wallet: {} -> {} sat (+{}; the opener pays the closing fee)'.format(
                    node, n(wallet0), n(wallet_sat(a)), n(wallet_sat(a) - wallet0)))
        else:
            ctx.say('closed; the funds are back in the wallets once onchaind is done')
        say_evm_tx(ctx, a, txid, 'closing tx')
        if a.kind == 'evm':
            ctx.say('next, to get ETH: Exit moves wallet sat into the contract as withdrawable, Withdraw pays it '
                    'to {}\'s ETH address (or Cash out does both); the node card shows each balance'.format(node))
    else:
        outs = confirmed_outputs(a)
        try:
            a.call('dev-fail', id=c['peer_id'])
            ctx.say('{} failed the channel on purpose (dev-fail)'.format(node))
        except Fail:
            # Without --developer: drop the peer and give up on negotiating at once.
            a.call('disconnect', id=c['peer_id'], force=True)
            a.call('close', id=channel_id, unilateraltimeout=1)
            ctx.say('{} disconnected and closed unilaterally'.format(node))
        ctx.mine(1)
        ctx.wait_for('the commitment to confirm',
                     lambda: chan_state(a, channel_id) in ('ONCHAIN', 'CLOSED', None), mine_between=True)
        ctx.say('commitment tx confirmed; each side sweeps its outputs as the timelocks pass')
        if ctx.miner is None:
            delay = int(peer.config('watchtime-blocks', 6)) if peer is not None and app.up.get(peer.name) else None
            interval = a.evm.get('interval') if a.kind == 'evm' else None
            ctx.say('{}\'s own share ({} sat) is locked for the to_self_delay the peer set{}, then onchaind sweeps '
                    'it to the wallet; the peer\'s share is in its wallet as soon as the commitment confirms. The '
                    'channel\'s state column shows onchaind\'s countdown.'.format(
                        node, n(share), ' ({} blocks{})'.format(n(delay), ', about {:.1f} h'.format(
                            delay * interval / 3600) if interval else '') if delay else ''))
        if ctx.miner is not None:
            delay = int(a.config('watchtime-blocks', 6))
            if peer is not None and app.up.get(peer.name):
                try:
                    delay = max(delay, int(peer.config('watchtime-blocks', 6)))
                except Fail:
                    pass
            ctx.say('mining {} blocks for the to_self_delay'.format(delay + 1))
            for _ in range(0, delay + 1, 5):
                ctx.mine(5)
                time.sleep(0.5)
            got = ctx.wait_for('our output to be swept to the wallet', lambda: confirmed_outputs(a) - outs,
                               mine_between=True, timeout=60, soft=True)
            if got:
                ctx.say('{} swept its output back to the wallet'.format(node))
        status = (channel_of(a, channel_id) or {}).get('status') or []
        if status:
            ctx.say('onchaind: {}'.format(status[-1]))
    if a.kind == 'liquid':
        after = elements_assets(a)
        for k in sorted(set(before) | set(after)):
            if k == 'bitcoin' and ctx.miner is not None:
                continue        # the blocks we mined pay elementsd's wallet too
            if after.get(k, 0) != before.get(k, 0):
                delta = after.get(k, 0) - before.get(k, 0)
                ctx.say("{}'s elementsd wallet: {} {:+,}".format(node, app.asset_label(k), delta))


def cmd_send(ctx, node, address, amount, asset):
    nd = ctx.app.nodes[node]
    if nd.kind == 'liquid':
        res = nd.call('assetchan-send', addr=address, asset=asset or 'bitcoin', amount=amount)
        ctx.say("sent {} {} from {}'s elementsd wallet to {}: txid {}".format(
            n(amount), ctx.app.asset_label(asset), node, address, res['txid']))
    else:
        res = nd.call('withdraw', destination=address, satoshi=amount)
        ctx.say('sent {} sat from {} to {}: txid {}'.format(
            amount if amount == 'all' else n(amount), node, address, res['txid']))
        if amount == 'all':
            say_reserve(ctx, nd)
    ctx.mine(1)


def cmd_newaddr(ctx, node, addrtype):
    nd = ctx.app.nodes[node]
    if addrtype == 'elements':
        addr = nd.call('assetchan-newaddr')['address']
        ctx.say("new address of {}'s elementsd wallet: {}".format(node, addr))
    else:
        addr = nd.call('newaddr', addresstype=addrtype)[addrtype]
        ctx.say('new {} address of {}: {}'.format(addrtype, node, addr))
    ctx.result['address'] = addr


def cmd_fund(ctx, node, sats):
    nd = ctx.app.nodes[node]
    addr = nd.call('newaddr', addresstype='bech32')['bech32']
    if nd.kind == 'liquid':
        res = nd.call('assetchan-send', addr=addr, asset='bitcoin', amount=sats)
        txid = res['txid']
        ctx.say('sent {} sat L-BTC to {} ({}): txid {}'.format(n(sats), node, addr, txid))
    else:
        if not isinstance(ctx.miner, CoreMiner):
            raise Fail('funding from bitcoind is for regtest only')
        ctx.miner.check()
        txid = ctx.miner.rpc.call('sendtoaddress', addr, '{}.{:08d}'.format(*divmod(sats, 10**8)))
        ctx.say("sent {} sat from bitcoind's wallet to {} ({}): txid {}".format(n(sats), node, addr, txid))
    ctx.mine(1)
    ctx.wait_for('the wallet to see it', lambda: any(o['txid'] == txid and o.get('status') == 'confirmed'
                                                     for o in nd.call('listfunds')['outputs']), mine_between=True)


def token_text(app, nd, units):
    return fmt_units(units, nd.evm.get('units_per_sat') or 1, app.token_label(nd.evm.get('token')))


def evm_amount_text(app, nd, res):
    if 'amount_units' in res:
        return token_text(app, nd, res['amount_units'])
    if 'amount_wei' in res:
        return fmt_eth(res['amount_wei'])
    return '{} sat'.format(n(res.get('amount_sat', 0)))


def evm_status_of(nd, cached=True):
    """evm-status for a job's own polling: cached=true (no fresh balance
    reads; withdrawable at most once per evm-poll or after a change), or
    plain where the plugin takes no `cached`."""
    if cached and nd.status_cached:
        try:
            return nd.call('evm-status', cached=True)
        except Fail:
            pass
    return nd.call('evm-status')


def cmd_deposit(ctx, node, sats):
    nd = ctx.app.nodes[node]
    addr = nd.call('newaddr', addresstype='p2tr')['p2tr']
    res = nd.call('evm-deposit', amount=sats, address=addr)
    what = '{} sat'.format(n(sats)) + (' ({})'.format(token_text(ctx.app, nd, res['amount_units']))
                                       if 'amount_units' in res else '')
    ctx.say('deposited {} to {} ({}): EVM tx {}, {}'.format(what, node, addr, res.get('evm_tx'), res.get('status')))
    txid = res.get('txid')
    if not txid:
        if res.get('status') == 'pending':
            ctx.say('not mined yet: the EVM status panel lists it under Pending sends')
        return
    ctx.say('bitcoin-side txid {}'.format(txid))
    ctx.app.track_deposit(node, txid, res.get('height'), sats)
    if ctx.miner is None:
        # Mined on the EVM chain: the job ends; the node card shows CLN catching up.
        tag = config_value(nd.cfg, 'evm-confirm', 'safe')
        ctx.say('mined; {} sees it once the {} block covers it and its tip reaches height {} '
                '(the node card shows the progress)'.format(node, tag, n(res.get('height') or 0)))
        return
    ctx.mine(1)
    ctx.wait_for('the wallet to see the deposit',
                 lambda: any(o['txid'] == txid and o.get('status') == 'confirmed'
                             for o in nd.call('listfunds')['outputs']), mine_between=True)
    ctx.say('{} sees it in its wallet'.format(node))


def withdrawable_of(st):
    return int(st.get('withdrawable_units', st.get('withdrawable_wei', 0)) or 0)


def say_reserve(ctx, nd):
    """After a withdraw of "all": what stayed in the wallet, and why."""
    try:
        outs = nd.call('listfunds')['outputs']
    except Fail:
        return
    left = sum(msat(o.get('amount_msat')) // 1000 for o in outs
               if o.get('status') in ('confirmed', 'unconfirmed') and not o.get('reserved'))
    if left <= 0:
        return
    reserve = emergency_reserve(nd)
    ctx.say('{} sat stayed in {}\'s wallet{}'.format(
        n(left), nd.name, ': ' + reserve_text(reserve) if left <= reserve else ''))


def cmd_exit(ctx, node, amount):
    app = ctx.app
    nd = app.nodes[node]
    st = evm_status_of(nd, cached=False)
    addr = nd.call('evm-exitaddr', ethaddress=st['eth_address'])['address']
    before = withdrawable_of(st)
    res = nd.call('withdraw', destination=addr, satoshi=amount)
    ctx.say('withdraw {} sat to the exit address {} of {}: txid {}'.format(
        amount if amount == 'all' else n(amount), addr, st['eth_address'], res['txid']))
    if amount == 'all':
        say_reserve(ctx, nd)
    # Our own submit's receipt marks withdrawable changed: cached reads see it.
    got = ctx.wait_for('the contract to credit the exit',
                       lambda: withdrawable_of(evm_status_of(nd)) > before, mine_between=True)
    if got:
        after = withdrawable_of(evm_status_of(nd))
        if st.get('token'):
            ctx.say('withdrawable now {} (+{})'.format(token_text(app, nd, after), token_text(app, nd, after - before)))
        else:
            ctx.say('withdrawable now {} (+{})'.format(fmt_eth(after), fmt_eth(after - before)))


def cmd_withdraw(ctx, node):
    nd = ctx.app.nodes[node]
    st = evm_status_of(nd, cached=False)
    gas0 = st.get('balance_wei')
    res = nd.call('evm-withdraw')
    ctx.say('withdrew {} to {}: EVM tx {}, {}'.format(
        evm_amount_text(ctx.app, nd, res), res.get('address'), res.get('evm_tx'), res.get('status')))
    after = evm_status_of(nd, cached=False)
    if gas0 is not None and after.get('balance_wei') is not None and not st.get('token'):
        got, paid = int(res.get('amount_wei') or 0), int(after['balance_wei']) - int(gas0)
        ctx.say('ETH balance of {}: {} -> {} ({}{}; the EVM tx\'s gas: {})'.format(
            res.get('address'), fmt_eth(gas0), fmt_eth(after['balance_wei']), '+' if paid >= 0 else '',
            fmt_eth(paid), fmt_eth(got - paid)))
    elif st.get('token'):
        ctx.say('token balance of {}: {}'.format(res.get('address'), after.get('token_balance_units')))
    ctx.say('check it on the explorer: the ETH address on the node card links there, and the EVM tx above')


def cmd_cashout(ctx, node):
    """Exit everything, wait until the contract credits it, withdraw."""
    cmd_exit(ctx, node, 'all')
    cmd_withdraw(ctx, node)


def cmd_mine(ctx, blocks):
    ctx.mine(blocks)
    ctx.say('mined {} {} on {}'.format(blocks, 'virtual blocks' if isinstance(ctx.miner, AnvilMiner) else 'blocks',
                                       ctx.net))


# ---------------------------------------------------------------- requests
def node_arg(app, body, key, kinds=None):
    name = body.get(key)
    if not isinstance(name, str) or name not in app.nodes:
        raise BadRequest('{}: pick one of {}'.format(key, ', '.join(app.nodes)))
    nd = app.nodes[name]
    if not app.up.get(name) or not nd.net:
        raise BadRequest('{} is down'.format(name))
    if not nd.detected or '?' in nd.net:
        raise BadRequest('{} is not fully detected yet ({}): try again shortly'.format(name, nd.net))
    if kinds and nd.kind not in kinds:
        raise BadRequest('{} is a {} node: this is for {} nodes'.format(name, nd.kind, ' or '.join(kinds)))
    return nd


def amount_arg(body, key='amount', allow_all=False):
    v = body.get(key)
    if allow_all and (v == 'all' or body.get('all') is True):
        return 'all'
    if isinstance(v, bool) or v is None:
        raise BadRequest('{}: enter a whole number'.format(key))
    if isinstance(v, str):
        v = v.strip().replace(',', '').replace('_', '')
        if not re.fullmatch(r'[0-9]{1,16}', v):
            raise BadRequest('{}: enter a whole number'.format(key))
    elif isinstance(v, float):
        if not v.is_integer():
            raise BadRequest('{}: enter a whole number'.format(key))
    elif not isinstance(v, int):
        raise BadRequest('{}: enter a whole number'.format(key))
    v = int(v)
    if not 1 <= v <= MAX_AMOUNT:
        raise BadRequest('{}: out of range'.format(key))
    return v


def same_net(a, b):
    if a.name == b.name:
        raise BadRequest('pick two different nodes')
    if a.net != b.net:
        raise BadRequest('{} is on {} and {} on {}: both must be on the same network'.format(
            a.name, a.net, b.name, b.net))


def asset_arg(body, *nodes, allowed=True):
    """The Liquid asset id, or None for L-BTC; checked against what each
    node allows (assetchan-asset), unless not `allowed`."""
    a = body.get('asset')
    if a in (None, '', 'lbtc', 'L-BTC', 'bitcoin'):
        return None
    if not all(nd.kind == 'liquid' for nd in nodes):
        raise BadRequest('asset: only Liquid nodes have assets')
    if not isinstance(a, str) or not HEX64.match(a.lower()):
        raise BadRequest('asset: not an asset id')
    a = a.lower()
    for nd in nodes:
        if allowed and a not in nd.allowed:
            raise BadRequest('asset: {} does not allow asset {}'.format(nd.name, a[:8]))
    return a


def bolt11_arg(body, node):
    s = body.get('bolt11')
    if not isinstance(s, str):
        raise BadRequest('bolt11: paste an invoice')
    s = s.strip().lower()
    if s.startswith('lightning:'):
        s = s[len('lightning:'):]
    if len(s) > 2100 or not BOLT11.match(s):
        raise BadRequest('bolt11: not a bolt11 invoice')
    hrp = s[2:s.rfind('1')]
    m = re.fullmatch(r'([a-z]+)([0-9]+[munp]?)?', hrp)
    want = (LIQUID_NETWORKS.get(node.network) or BTC_NETWORKS.get(node.network) or (None,))[0]
    if not m or m.group(1) != want:
        raise BadRequest('bolt11: an invoice for another network (ln{}…; {} takes ln{}…)'.format(
            m.group(1) if m else hrp, node.name, want))
    return s


def check_payee(app, node, bolt11):
    """Refuses an invoice of one of our nodes on another network ID (EVM and
    Bitcoin regtest invoices look the same)."""
    try:
        payee = node.call('decode', string=bolt11).get('payee')
    except Fail as e:
        raise BadRequest('bolt11: {}'.format(app.scrub.text(e)))
    other = peer_node(app, payee)
    if other is not None and other.net != node.net:
        raise BadRequest('bolt11: that is an invoice of {} on {}, not {}'.format(other.name, other.net, node.net))


def address_arg(body, node):
    a = body.get('address')
    if not isinstance(a, str) or not a.strip():
        raise BadRequest('address: enter an address')
    a = a.strip()
    if node.kind == 'liquid':
        if not LIQUID_ADDRESS.match(a):
            raise BadRequest('address: not a Liquid address')
        return a
    hrp = BTC_ADDRESS_HRP.get(node.network)
    m = BECH32.match(a.lower())
    if m and (a == a.lower() or a == a.upper()):
        if m.group(1) != hrp:
            raise BadRequest('address: a {}1… address, not for {} ({}1…)'.format(m.group(1), node.network, hrp))
        return a
    if BASE58.match(a) and len(a) <= 35:
        return a
    raise BadRequest('address: not a {} address'.format(node.network))


def text_arg(body, key, default, limit=100):
    v = body.get(key)
    if v in (None, ''):
        return default
    if not isinstance(v, str) or len(v) > limit or not v.isprintable():
        raise BadRequest('{}: at most {} printable characters'.format(key, limit))
    return v


def channel_arg(app, body, node):
    cid = body.get('chan')
    if not isinstance(cid, str) or not HEX64.match(cid):
        raise BadRequest('chan: not a channel id')
    c = app.chans.get(node.name, {}).get(cid)
    if c is None:
        raise BadRequest('chan: {} has no such channel'.format(node.name))
    if c.get('state') in CLOSED_STATES:
        raise BadRequest('chan: that channel is already closed')
    return cid, c


def build_job(app, body):
    """(title, network ID, function of ctx, the page nodes it touches) for a
    request; raises BadRequest."""
    if not isinstance(body, dict):
        raise BadRequest('bad request')
    action = body.get('action')
    if action == 'open':
        a, b = node_arg(app, body, 'frm'), node_arg(app, body, 'to')
        same_net(a, b)
        amount, asset, announce = amount_arg(body), asset_arg(body, a, b), bool(body.get('announce'))
        net = app.channel_net(a, asset)
        return ('Open {} channel {} to {}, {} {}'.format(net, a.name, b.name, n(amount), unit_name(asset)),
                a.net, lambda ctx: cmd_open(ctx, a.name, b.name, amount, asset, announce), (a.name, b.name))
    if action == 'pay':
        a = node_arg(app, body, 'frm')
        asset = asset_arg(body, a)
        if body.get('bolt11'):
            bolt11 = bolt11_arg(body, a)
            check_payee(app, a, bolt11)
            return ('Pay an invoice from {} ({})'.format(a.name, app.channel_net(a, asset)), a.net,
                    lambda ctx: cmd_pay_invoice(ctx, a.name, bolt11, asset), (a.name,))
        b = node_arg(app, body, 'to')
        same_net(a, b)
        asset = asset_arg(body, a, b)
        amount = amount_arg(body)
        return ('Pay {} {} from {} to {} ({})'.format(n(amount), unit_name(asset), a.name, b.name,
                                                      app.channel_net(a, asset)),
                a.net, lambda ctx: cmd_pay(ctx, a.name, b.name, amount, asset), (a.name, b.name))
    if action == 'swap':
        a, b = node_arg(app, body, 'frm'), node_arg(app, body, 'to')
        if a.name == b.name:
            raise BadRequest('pick two different nodes')
        if 'payassetinvoice' not in a.methods:
            raise BadRequest('{} has no assetswap-plugin with a provider to pay through'.format(a.name))
        if 'assetinvoice' not in b.methods:
            raise BadRequest('{} has no assetswap-plugin to make the invoice'.format(b.name))
        from_asset, to_asset = swap_asset_arg(body, 'from_asset', a), swap_asset_arg(body, 'to_asset', b)
        amount = amount_arg(body)
        tier = body.get('tier') or 'medium'
        if tier not in SWAP_TIERS:
            raise BadRequest('tier: one of {}'.format(', '.join(SWAP_TIERS)))
        return ('Swap: {} pays {} to {} in {}'.format(a.name, swap_amount(app, to_asset, amount), b.name,
                                                     swap_label(app, from_asset)),
                # Only the payer: a job's nodes must be on its network, and the
                # payee may be on another chain (it only makes an invoice).
                a.net, lambda ctx: cmd_swap(ctx, a.name, b.name, to_asset, amount, from_asset, tier),
                (a.name,))
    if action == 'invoice':
        nd = node_arg(app, body, 'node')
        amount, asset = amount_arg(body), asset_arg(body, nd)
        desc = text_arg(body, 'description', 'nodeui invoice')
        return ('Invoice on {} for {} {}'.format(nd.name, n(amount), unit_name(asset)), nd.net,
                lambda ctx: cmd_invoice(ctx, nd.name, amount, asset, desc), (nd.name,))
    if action == 'close':
        nd = node_arg(app, body, 'node')
        cid, c = channel_arg(app, body, nd)
        force = bool(body.get('force'))
        peer = peer_node(app, c.get('peer_id'))
        return ('{} {} channel {} and {}'.format('Force close' if force else 'Close',
                                                 app.channel_net(nd, c.get('asset')), nd.name,
                                                 peer.name if peer else c.get('peer_id', '')[:10]),
                nd.net, lambda ctx: cmd_close(ctx, nd.name, cid, force),
                (nd.name,) + ((peer.name,) if peer else ()))
    if action == 'send':
        nd = node_arg(app, body, 'node')
        address = address_arg(body, nd)
        # (elementsd's wallet may send any asset it holds)
        asset = asset_arg(body, nd, allowed=False)
        amount = amount_arg(body, allow_all=nd.kind != 'liquid')
        return ('Send {} {} on-chain from {}'.format(amount if amount == 'all' else n(amount),
                                                     app.asset_label(asset) if nd.kind == 'liquid' else 'sat',
                                                     nd.name),
                nd.net, lambda ctx: cmd_send(ctx, nd.name, address, amount, asset), (nd.name,))
    if action == 'newaddr':
        nd = node_arg(app, body, 'node')
        kind = body.get('type') or 'bech32'
        if kind not in ('bech32', 'p2tr') + (('elements',) if nd.kind == 'liquid' else ()):
            raise BadRequest('type: bech32, p2tr{}'.format(' or elements' if nd.kind == 'liquid' else ''))
        return ('New {} address on {}'.format(kind, nd.name), nd.net,
                lambda ctx: cmd_newaddr(ctx, nd.name, kind), (nd.name,))
    if action == 'fund':
        nd = node_arg(app, body, 'node', ('liquid', 'bitcoin'))
        if nd.kind == 'bitcoin' and not isinstance(app.miners.get(nd.net), CoreMiner):
            raise BadRequest('funding from bitcoind is only on regtest with its RPC known')
        sats = amount_arg(body)
        src = 'L-BTC from elementsd' if nd.kind == 'liquid' else 'from bitcoind'
        return ('Fund {} with {} sat {}'.format(nd.name, n(sats), src), nd.net,
                lambda ctx: cmd_fund(ctx, nd.name, sats), (nd.name,))
    if action == 'deposit':
        nd = node_arg(app, body, 'node', ('evm',))
        sats = amount_arg(body)
        return ('Deposit {} sat to {} ({})'.format(n(sats), nd.name, app.channel_net(nd)), nd.net,
                lambda ctx: cmd_deposit(ctx, nd.name, sats), (nd.name,))
    if action == 'exit':
        nd = node_arg(app, body, 'node', ('evm',))
        amount = amount_arg(body, allow_all=True)
        return ('Exit {} sat from {} to its ETH address'.format(amount if amount == 'all' else n(amount), nd.name),
                nd.net, lambda ctx: cmd_exit(ctx, nd.name, amount), (nd.name,))
    if action == 'cashout':
        nd = node_arg(app, body, 'node', ('evm',))
        return ('Cash out {}: exit its whole wallet and withdraw to its ETH address'.format(nd.name), nd.net,
                lambda ctx: cmd_cashout(ctx, nd.name), (nd.name,))
    if action == 'withdraw':
        nd = node_arg(app, body, 'node', ('evm',))
        return ('Withdraw the withdrawable of {} from the contract'.format(nd.name), nd.net,
                lambda ctx: cmd_withdraw(ctx, nd.name), (nd.name,))
    if action == 'mine':
        net = body.get('net')
        if not isinstance(net, str) or app.miners.get(net) is None:
            raise BadRequest('net: nobody mines on that network')
        blocks = amount_arg(body, 'blocks')
        if blocks > 500:
            raise BadRequest('blocks: at most 500')
        return ('Mine {} on {}'.format(blocks, short_net_id(net)), net, lambda ctx: cmd_mine(ctx, blocks), ())
    raise BadRequest('unknown action')


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = 'nodeui'
    sys_version = ''
    app = None          # set by serve()
    token = ''

    def log_message(self, fmt, *a):
        pass

    def reply(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if ctype.startswith('text/html'):
            self.send_header('Content-Security-Policy',
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' "
                             "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
                             "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                             "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def local_only(self):
        """Refuse other Host names (DNS rebinding) and other origins."""
        port = self.server.server_address[1]
        ok = {'127.0.0.1:{}'.format(port), 'localhost:{}'.format(port)}
        if self.headers.get('Host') not in ok:
            return False
        origin = self.headers.get('Origin')
        return origin is None or origin in {'http://' + h for h in ok}

    def token_ok(self):
        got = self.headers.get(TOKEN_HEADER, '')
        return got.isascii() and secrets.compare_digest(got, self.token)

    def do_GET(self):
        if not self.local_only():
            return self.reply(403, {'error': 'forbidden'})
        path = self.path.split('?')[0]
        if path == '/api/state':
            if not self.token_ok():
                return self.reply(403, {'error': 'forbidden: reload the page'})
            state = self.app.current_state()
            if not state:
                self.app.wake.set()
                return self.reply(503, {'error': 'reading the nodes, try again'})
            return self.reply(200, state)
        if path == '/':
            with open(os.path.join(UI_DIR, 'index.html'), 'rb') as f:
                page = f.read().replace(b'__NODEUI_TOKEN__', self.token.encode())
            return self.reply(200, page, 'text/html; charset=utf-8')
        if path == '/demo.json':
            with open(os.path.join(UI_DIR, 'demo.json'), 'rb') as f:
                return self.reply(200, f.read())
        self.reply(404, {'error': 'not found'})

    def body(self):
        length = int(self.headers.get('Content-Length', 0))
        if not 0 < length <= 10_000:
            raise BadRequest('bad request')
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise BadRequest('bad request')
        return body

    def do_POST(self):
        if not self.local_only() or not self.token_ok() \
                or self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            return self.reply(403, {'error': 'forbidden'})
        if self.path not in ('/api/job', '/api/cancel', '/api/refresh'):
            return self.reply(404, {'error': 'not found'})
        try:
            body = self.body()
            if self.path == '/api/cancel':
                job_id = body.get('id')
                if isinstance(job_id, bool) or not isinstance(job_id, int):
                    raise BadRequest('id: the job to cancel')
                return self.reply(200, {'cancelled': self.app.jobs.cancel(job_id)})
            if self.path == '/api/refresh':
                name = body.get('node')
                if not isinstance(name, str) or name not in self.app.nodes or self.app.nodes[name].kind != 'evm':
                    raise BadRequest('node: pick an EVM node')
                wait = self.app.refresh_node(name)
                return self.reply(200, {'refreshed': not wait, 'wait': wait})
            title, net, fn, nodes = build_job(self.app, body)
            job_id = self.app.jobs.start(title, net, fn, nodes)
            job = self.app.jobs.get(job_id) or {}
            return self.reply(200, {'id': job_id, 'status': job.get('status')})
        except Busy:
            return self.reply(409, {'error': 'Too many actions are waiting on this network (or node): '
                                             'wait for one to end, or cancel one.'})
        except (BadRequest, ValueError) as e:
            return self.reply(400, {'error': self.app.scrub.text(e)})
        except Exception as e:          # noqa: B902 - e.g. JSON nested too deep
            return self.reply(400, {'error': 'bad request ({})'.format(type(e).__name__)})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """No traceback on stderr: it could hold a request's data."""
        print('request failed: {}'.format(type(sys.exc_info()[1]).__name__), file=sys.stderr)


def serve(app, port):
    handler = type('BoundHandler', (Handler,), {'app': app, 'token': secrets.token_urlsafe(24)})
    srv = Server(('127.0.0.1', port), handler)
    srv.daemon_threads = True
    threading.Thread(target=app.run_poller, daemon=True, name='poller').start()
    return srv


# ---------------------------------------------------------------- main
def load_config(path):
    """nodes.json: {"nodes": [{"name", "dir", "network"?}] or {name: dir},
    "labels": {id: label}, "bitcoin-rpc", "elements-rpc", "elements-wallet"}."""
    with open(os.path.expanduser(path)) as f:
        cfg = json.load(f)
    nodes = cfg.get('nodes', [])
    if isinstance(nodes, dict):
        nodes = [{'name': k, 'dir': v} for k, v in nodes.items()]
    return cfg, [(x['name'], x['dir'], x.get('network')) for x in nodes]


def parse_label(spec):
    k, _, v = spec.partition('=')
    if not k or not v or len(v) > 16 or not re.fullmatch(r'[\w.$-]+', v):
        raise ValueError('--label takes ID=NAME (NAME: up to 16 of A-Z a-z 0-9 _ . $ -)')
    return k.lower(), v


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--node', action='append', default=[], metavar='NAME=LIGHTNING-DIR',
                   help='a node to show (repeatable); default: A..D under nodes.sh\'s directory')
    p.add_argument('--config', metavar='NODES.JSON', help='nodes (and labels, RPCs) from a JSON file')
    p.add_argument('--network', default=None,
                   help='the network subdirectory of the lightning dirs (default: found by looking), '
                        'and nodes.sh\'s network for the default nodes ($ASSETCHAN_NETWORK, liquid-testnet)')
    p.add_argument('--bitcoin-rpc', default=os.environ.get('BITCOIN_RPC'),
                   help='bitcoind http://user:password@host:port, to mine on regtest '
                        '(default: the nodes\' bitcoin-rpc* options, or the cookie)')
    p.add_argument('--elements-rpc', default=os.environ.get('ELEMENTS_RPC'),
                   help='elementsd http://user:password@host:port, to mine on liquid-regtest '
                        '(default: the nodes\' assetchan-elements-rpc)')
    p.add_argument('--elements-wallet', default=os.environ.get('ELEMENTS_WALLET'))
    p.add_argument('--label', action='append', default=[], metavar='ID=NAME',
                   help='a name for a Liquid asset id or an ERC-20 token address')
    p.add_argument('--evm-status-every', type=float, default=300,
                   help='seconds between evm-status reads off anvil, with cached=true (no fresh balance '
                        'reads); each node\'s Refresh button reads it afresh, at most every 30 s')
    p.add_argument('--port', type=int, default=8765)
    args = p.parse_args(argv)

    specs, labels = [], {}
    bitcoin_rpc, elements_rpc, elements_wallet = args.bitcoin_rpc, args.elements_rpc, args.elements_wallet
    if args.config:
        cfg, specs = load_config(args.config)
        labels.update({k.lower(): v for k, v in cfg.get('labels', {}).items()})
        bitcoin_rpc = bitcoin_rpc or cfg.get('bitcoin-rpc')
        elements_rpc = elements_rpc or cfg.get('elements-rpc')
        elements_wallet = elements_wallet if elements_wallet is not None else cfg.get('elements-wallet')
    for spec in args.node:
        name, _, d = spec.partition('=')
        if not name or not d:
            p.error('--node takes NAME=LIGHTNING-DIR')
        specs.append((name, d, None))
    try:
        labels.update(dict(parse_label(x) for x in args.label))
    except ValueError as e:
        p.error(str(e))
    if not specs:
        network = args.network or os.environ.get('ASSETCHAN_NETWORK', 'liquid-testnet')
        base = os.environ.get('ASSETCHAN_DIR') or os.path.expanduser(
            '~/.assetchan-' + network.replace('liquid-', ''))
        specs = [(x, os.path.join(base, x), network) for x in ('A', 'B', 'C', 'D')
                 if os.path.isdir(os.path.join(base, x))]
        if not specs:
            p.error('no nodes under {}: pass --node NAME=LIGHTNING-DIR'.format(base))
        if elements_rpc and elements_wallet is None:
            elements_wallet = 'poc'         # as contrib/assetchan/ui.py did
    nodes, seen = [], set()
    for name, d, net in specs:
        if name in seen:
            p.error('two nodes named {}'.format(name))
        seen.add(name)
        try:
            nodes.append(Node(name, d, net or args.network))
        except ValueError as e:
            p.error('{}: {}'.format(name, e))
    try:
        app = App(nodes, labels, bitcoin_rpc, elements_rpc, elements_wallet, args.evm_status_every)
    except ValueError as e:
        p.error(str(e))
    srv = serve(app, args.port)
    print('nodeui ({}): http://127.0.0.1:{}'.format(', '.join(app.nodes), srv.server_address[1]), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
