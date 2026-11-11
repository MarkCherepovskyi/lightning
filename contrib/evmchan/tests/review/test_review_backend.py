# SPDX-License-Identifier: MIT
"""Adversarial review PoCs for evmbackend.py (no anvil, no lightningd).
Each test asserts the behaviour the review expects. The strict xfail marks
are gone (all pass since the fix); two assertions changed with SPEC draft 5,
which answers success once the node accepts a send (5.3 step 4) instead of
"pending": see the comments marked "draft 5"."""
import os
import threading
import time


import btc
import evm
import evmbackend as eb

INTERVAL = 1          # time limit 1.8 s: keeps the tests short
GOOD_CONFIGS = {'watchtime-blocks': {'value_int': 40}, 'cltv-delta': {'value_int': 40},
                'cltv-final': {'value_int': 40}, 'funding-confirms': {'value_int': 1},
                'max-concurrent-htlcs': {'value_int': 30}, 'developer': {'set': True},
                'dev-bitcoind-poll': {'value_int': 1}}


class Log:
    def __init__(self):
        self.lines = []

    def __call__(self, level, msg):
        self.lines.append((level, msg))


class FakeIx:
    chain_id = 31337

    def tx(self, txid_hex):
        return None

    def prevouts(self, tx):
        return [(100_000, b'\x00\x14' + b'\x11' * 20, 1) for _ in tx.inputs]

    def add_receipt(self, receipt):
        return []


class FakeNode:
    """The send worker's EVM node. Txs whose calldata holds a `stuck` btc tx
    are accepted but never mined; others are mined at once."""

    def __init__(self):
        self.stuck = set()        # btc raw hex
        self.mined = {}           # evm tx hash -> receipt
        self.sent = []            # (btc raw hex or None, nonce-free record)
        self.calls = []
        self.latest_nonce = 0
        self.pending_nonce = 0
        self.lock = threading.Lock()
        self.delay = 0.0          # per-request latency (Infura: ~0.1-0.3 s)

    def eth_call(self, *a, **k):
        time.sleep(self.delay)
        return b''

    def estimate_gas(self, *a, **k):
        time.sleep(self.delay)
        return 100_000

    def fees(self):
        time.sleep(2 * self.delay)     # eth_getBlockByNumber + eth_maxPriorityFeePerGas
        return 1, 10

    def nonce(self, address, tag='pending'):
        time.sleep(self.delay)
        return self.pending_nonce if tag == 'pending' else self.latest_nonce

    def call(self, method, *params):
        time.sleep(self.delay)
        with self.lock:
            self.calls.append(method)
        if method == 'eth_sendRawTransaction':
            raw = params[0][2:]
            h = '0x' + evm.keccak256(bytes.fromhex(raw)).hex()
            which = None
            for s in self.stuck:
                if s in raw:
                    which = s
            self.sent.append(which or raw)
            if which is None:
                self.mined[h] = {'blockHash': '0x' + '22' * 32, 'status': '0x1', 'transactionHash': h,
                                 'logs': []}
            return h
        if method == 'eth_getTransactionReceipt':
            return self.mined.get(params[0])
        raise AssertionError(method)


def mk_tx():
    return btc.Tx(2, [btc.TxIn(os.urandom(32), 0)], [(1000, b'\x00\x14' + b'\x22' * 20)], 0, [[b'\x01']])


def offline(tmp_path, node, log=None):
    cfg = eb.Config('http://127.0.0.1:1', evm.checksum_address('0x' + '11' * 20), os.urandom(32),
                    str(tmp_path / 'evm-key'), 'safe', 1.0, 10**16, str(tmp_path))
    b = eb.Backend(cfg, log or Log())
    b.interval = INTERVAL
    b.ix = FakeIx()
    b.srpc = node
    b.balance = 10**18
    return b


def run_worker(b):
    t = threading.Thread(target=b._worker_loop, daemon=True)
    t.start()
    return t


def test_pending_send_starves_the_queue(tmp_path):
    """CLN's rebroadcast_txs sends every outgoing tx at once. One of them
    (A) is pending without a receipt (stuck, nonce gap, low fees); the next
    one (B, say a penalty) is queued behind it, round after round."""
    node = FakeNode()
    node.delay = 0.05
    b = offline(tmp_path, node)
    t = run_worker(b)
    try:
        a, pen = mk_tx(), mk_tx()
        node.stuck.add(a.serialize().hex())
        assert b.send_now(a.serialize().hex())['success']      # draft 5: success once accepted
        rounds = []
        for _ in range(3):                          # three rebroadcast rounds
            got = {}
            ra = b.sendrawtransaction(a.serialize().hex(), lambda r: got.setdefault('a', r))
            rb = b.sendrawtransaction(pen.serialize().hex(), lambda r: got.setdefault('b', r))
            ra.event.wait(5)
            rb.event.wait(5)
            rounds.append(got['b'])
        print(rounds)
        # The penalty never reached the EVM node in any round.
        assert any(r['success'] for r in rounds), rounds
    finally:
        b.stop()
        t.join(5)


def test_track_cost_grows_with_replacements(tmp_path):
    """A tx stuck for a day at INTERVAL 60 has ~1440 hashes; each _track()
    (every <= 2 s, and after every job) makes one eth_getTransactionReceipt
    per hash: Infura's rate limit and credits go, and with them the index
    sync that shares the node."""
    node = FakeNode()
    b = offline(tmp_path, node)
    p = eb.Pending('submit', 'aa' * 32, 0, 100_000, b.contract, b'', 0, 1, 10, '0x' + '00' * 32)
    p.hashes += ['0x{:064x}'.format(i) for i in range(1, 200)]
    b.pending[0] = p
    b._track()
    n = node.calls.count('eth_getTransactionReceipt')
    print('receipt calls for one _track():', n)
    assert n <= 2


def test_untracked_nonce_wedges_every_later_send(tmp_path):
    """The process died between eth_sendRawTransaction and _save_pending
    (or evm-pending.json was unreadable): nonce 0 sits in the node's pool,
    underpriced, untracked. Every later send gets nonce 1+ and waits behind
    it; replacements bump 1+, never 0. Nothing from the key is mined again."""
    node = FakeNode()
    node.latest_nonce, node.pending_nonce = 0, 1       # nonce 0 known to the pool, never mined
    b = offline(tmp_path, node)
    tx = mk_tx()
    node.stuck.add(tx.serialize().hex())               # stays queued behind nonce 0
    t = run_worker(b)
    try:
        assert b.send_now(tx.serialize().hex())['success']   # draft 5: success once accepted
    finally:
        b.stop()
        t.join(5)
    assert list(b.pending) == [1]
    for _ in range(4):
        for p in b.pending.values():
            p.replace_at = 0
        b._track()
    nonces_sent = sorted(b.pending)
    # The plugin never addresses nonce 0 (a cancel or re-send there).
    assert 0 in nonces_sent, nonces_sent


def test_htlc_cap_double_counts_recent_forwards(tmp_path):
    out = []

    class Ln:
        def call(self, method, params=None):
            return {'channels': [{'peer_id': '03' * 33, 'channel_id': 'aa' * 32, 'short_channel_id': '1x1x0',
                                  'state': 'CHANNELD_NORMAL', 'htlcs': list(out)}]}
    b = offline(tmp_path, FakeNode())
    b.lnrpc = lambda: Ln()
    b.apply_option_check(GOOD_CONFIGS, {'our_features': {}})
    results = []
    for _ in range(20):
        r = b.htlc_accepted({'short_channel_id': '1x1x0'}, {})
        results.append(r['result'])
        if r['result'] == 'continue':
            out.append({'direction': 'out'})        # lightningd adds it at once
    print(results)
    # 20 HTLCs out on one channel is well under the cap of 30.
    assert results == ['continue'] * 20


def test_dust_deposits_block_every_withdraw_and_open(tmp_path):
    """Anyone can deposit 1 sat to one of our (public) addresses, 201 times,
    for a few cents of gas. Then every withdraw / fundchannel without
    `utxos` is refused, though lightningd would pick the one real UTXO."""
    outs = [{'amount_msat': 1000, 'status': 'confirmed', 'reserved': False}] * 201
    outs.append({'amount_msat': 10**8 * 1000, 'status': 'confirmed', 'reserved': False})

    class Ln:
        def call(self, method, params=None):
            assert method == 'listfunds'
            return {'outputs': outs}
    b = offline(tmp_path, FakeNode())
    b.lnrpc = lambda: Ln()
    b.apply_option_check(GOOD_CONFIGS, {'our_features': {}})
    r = b.rpc_command({'method': 'withdraw', 'params': {'destination': 'x', 'satoshi': 10_000}})
    print(r)
    assert r == {'result': 'continue'}
