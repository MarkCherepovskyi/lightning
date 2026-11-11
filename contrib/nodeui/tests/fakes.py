"""Fake lightningd RPCs for the unit tests (and demo.json): no daemons."""
import copy
import errno
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import ui  # noqa: E402

ASSET = 'ddc8da3f868bd193d25e3bbdd3e3c075d6629fa45dee1c3d829682d82094ba50'
OTHER_ASSET = '5e56519ab27fcaf0f51affd5e82d299f5b07863739ed804a3a9f4c24dc5f149d'
CONTRACT = '0xB820' + '0' * 32 + '940E'
ANVIL_CONTRACT = '0x5FbDB2315678afecb367f032d93F642f64180aa3'
TOKEN = '0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238'


def node_id(i):
    return '02' + '{:02x}'.format(i) * 32


def conf(v):
    if isinstance(v, bool):
        return {'value_bool': v, 'source': 'cmdline'}
    if isinstance(v, int):
        return {'value_int': v, 'source': 'cmdline'}
    if isinstance(v, list):
        return {'values_str': v, 'sources': ['cmdline']}
    return {'value_str': v, 'source': 'cmdline'}


class FakeRpc:
    """What one lightningd answers: a dict of method -> reply (or a function
    of the params). .calls records what was asked; .down makes it refuse."""

    def __init__(self, i, network='regtest', methods=(), configs=None, port=None):
        self.id = node_id(i)
        self.network = network
        self.down = False
        self.calls = []
        self.methods = ['getinfo', 'listfunds', 'listpeers', 'listpeerchannels', 'listconfigs', 'listchannels',
                        'fundchannel', 'connect', 'close', 'invoice', 'xpay', 'withdraw', 'newaddr', 'decode',
                        'dev-fail', 'listforwards'] + list(methods)
        cfg = {'funding-confirms': 1, 'watchtime-blocks': 6}
        cfg.update(configs or {})
        self.r = {
            'help': lambda p: {'help': [{'command': m} for m in self.methods]},
            'getinfo': {'id': self.id, 'alias': 'n{}'.format(i), 'network': network, 'blockheight': 101,
                        'binding': [{'type': 'ipv4', 'address': '127.0.0.1', 'port': port or 9000 + i}]},
            'listconfigs': {'configs': {k: conf(v) for k, v in cfg.items()}},
            'listfunds': {'outputs': [{'txid': 'aa' * 32, 'output': 0, 'amount_msat': 5_000_000_000,
                                       'status': 'confirmed'}], 'channels': []},
            'listpeers': {'peers': []},
            'listpeerchannels': {'channels': []},
            'listchannels': {'channels': []},
            'listforwards': {'forwards': []},
            'newaddr': lambda p: {p.get('addresstype', 'bech32'): self.address(p.get('addresstype'))},
            'invoice': lambda p: {'bolt11': self.bolt11(p), 'payment_hash': '11' * 32},
            'xpay': {'payment_preimage': '22' * 32, 'amount_msat': 1_000_000, 'amount_sent_msat': 1_001_000,
                     'successful_parts': 1, 'failed_parts': 0},
            'withdraw': {'txid': 'cc' * 32, 'tx': '00'},
            'decode': lambda p: {'payee': self.payees.get(p['string'], '03' + '00' * 32), 'valid': True},
            'connect': {'id': 'x'},
        }
        self.payees = {}

    def address(self, kind):
        hrp = {'regtest': 'bcrt', 'testnet': 'tb', 'bitcoin': 'bc'}.get(self.network, 'ert')
        return hrp + '1q' + 'q' * 38 if kind != 'p2tr' else hrp + '1p' + 'q' * 58

    def bolt11(self, p):
        hrp = {'regtest': 'bcrt', 'testnet': 'tb', 'bitcoin': 'bc', 'liquid-regtest': 'ert',
               'liquid-testnet': 'tex'}[self.network]
        inv = 'ln{}{}n1p'.format(hrp, p.get('amount_msat', p.get('amount', 1000)) // 1000 or 1) + 'q' * 120
        self.payees[inv] = self.id
        return inv

    def __call__(self, path):          # the Node factory
        return self

    def call(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if self.down:
            raise OSError(errno.ECONNREFUSED, 'Connection refused')
        if method not in self.r:
            raise ui.RpcError(method, params, {'code': -32601, 'message': 'Unknown command {}'.format(method)})
        v = self.r[method]
        if isinstance(v, Exception):
            raise v
        return v(params) if callable(v) else copy.deepcopy(v)

    def called(self, method):
        return [p for m, p in self.calls if m == method]


def bitcoin(i, network='regtest', **kw):
    return FakeRpc(i, network, **kw)


def liquid(i, network='liquid-regtest', assets=(ASSET,), plugin=True):
    f = FakeRpc(i, network, methods=['assetchan-open', 'assetchan-close', 'assetchan-list', 'assetchan-funds',
                                     'assetchan-send', 'assetchan-listchannels', 'assetchan-newaddr',
                                     'assetchan-invoice', 'assetchan-pay', 'assetchan-sweeps'] if plugin else [],
                configs={'assetchan-asset': list(assets), 'assetchan-default-fee-reserve': 2000,
                         'assetchan-elements-rpc': 'http://poc:s3cretpw@127.0.0.1:18884',
                         'assetchan-elements-wallet': 'poc'} if plugin else {})
    f.r['assetchan-funds'] = {'elements': {'bitcoin': 89085, ASSET: 99_000_000_000}}
    f.r['assetchan-sweeps'] = {'sweeps': []}
    f.r['assetchan-listchannels'] = {'channels': []}
    f.r['assetchan-invoice'] = lambda p: dict(f.r['invoice'](p), asset=p['asset'])
    f.r['assetchan-pay'] = f.r['xpay']
    f.r['assetchan-send'] = {'txid': 'dd' * 32}
    f.r['assetchan-newaddr'] = {'address': 'el1qq' + 'q' * 60}
    return f


def evm_status(chain_id=31337, contract=ANVIL_CONTRACT,
               token=None, eth=10**18, withdrawable=0, **extra):
    st = {'contract': contract, 'chain_id': chain_id, 'interval': 2, 'confirm': 'safe',
          'eth_address': '0x70997970C51812dc3A010C7d01b50e0d17dc79C8', 'balance_wei': eth,
          'tip': 120, 'ibd': False, 'current_height': 121, 'lag': 1, 'smallest_margin': 20,
          'tag_block': 130, 'tag_timestamp': 1700000000, 'tag_age_s': 3.2, 'tag_unchanged_s': 1.0,
          'checkpoint': {'evm_block': 128, 'count': 40}, 'pending_sends': [], 'alarms': {},
          'rpc_calls': {'eth_call': 100, 'eth_getLogs': 10, 'eth_chainId': 2}, 'rpc_calls_since': 0,
          'options_checked': True, 'option_problems': [], 'opens_refused': None}
    if token:
        st.update({'token': token, 'units_per_sat': 100, 'token_balance_units': 5_000_000_000,
                   'withdrawable_units': withdrawable})
    else:
        st['withdrawable_wei'] = withdrawable
    st.update(extra)
    return st


def evm(i, chain_id=31337, contract=ANVIL_CONTRACT, token=None, rpc='http://127.0.0.1:8545'):
    f = FakeRpc(i, 'regtest', methods=['evm-status', 'evm-deposit', 'evm-exitaddr', 'evm-withdraw', 'getutxout'],
                configs={'evm-rpc': rpc, 'evm-contract': contract, 'evm-min-balance': '0.01',
                         'watchtime-blocks': 40})
    f.status = evm_status(chain_id, contract, token)
    f.r['evm-status'] = lambda p: copy.deepcopy(f.status)
    f.r['evm-deposit'] = lambda p: dict({'address': p.get('address'), 'amount_sat': p['amount'],
                                         'evm_tx': '0x' + 'ab' * 32, 'status': 'mined', 'txid': 'ee' * 32,
                                         'height': 130},
                                        **({'amount_units': p['amount'] * 100, 'token': token} if token else {}))
    f.r['evm-exitaddr'] = {'address': 'bcrt1s' + 'q' * 34, 'ethaddress': f.status['eth_address']}
    f.r['evm-withdraw'] = lambda p: dict({'address': f.status['eth_address'], 'evm_tx': '0x' + 'cd' * 32,
                                          'status': 'mined'},
                                         **({'amount_units': 1234500, 'token': token} if token
                                            else {'amount_wei': 1_000_010_000_000_000}))
    return f


def channel(peer, cid, state='CHANNELD_NORMAL', to_us=600_000, total=1_000_000, opener='local', asset=None,
            scid='103x1x0', status=()):
    c = {'peer_id': peer, 'peer_connected': True, 'state': state, 'channel_id': cid, 'short_channel_id': scid,
         'opener': opener, 'to_us_msat': to_us * 1000, 'total_msat': total * 1000, 'status': list(status)}
    if asset:
        c['asset'] = asset
    return c


def connect(a, b, cid, **kw):
    """A channel a -> b, seen from both sides."""
    for x, y in ((a, b), (b, a)):
        if y.id not in [p['id'] for p in x.r['listpeers']['peers']]:
            x.r['listpeers']['peers'].append({'id': y.id, 'connected': True})
    to_us, total = kw.pop('to_us', 600_000), kw.pop('total', 1_000_000)
    a.r['listpeerchannels']['channels'].append(channel(b.id, cid, to_us=to_us, total=total, **kw))
    b.r['listpeerchannels']['channels'].append(channel(a.id, cid, to_us=total - to_us, total=total,
                                                       **dict(kw, opener='remote')))


class FakeMiner(ui.CoreMiner):
    def __init__(self):
        self.mined = 0
        self.rpc = self

    def mine(self, n):
        self.mined += n

    def check(self):
        pass

    def call(self, method, *params):        # as bitcoind, for fund
        return 'ff' * 32


def make_app(fakes, **kw):
    """An App over fakes {name: FakeRpc}, polled once."""
    nodes = [ui.Node(name, '/nonexistent/' + name, factory=f) for name, f in fakes.items()]
    app = ui.App(nodes, env={}, **kw)
    app.make_miner = lambda node: FakeMiner() if (node.evm.get('chain_id') == 31337 if node.kind == 'evm'
                                                  else node.network in ('regtest', 'liquid-regtest')) else None
    app.poll_once()
    return app
