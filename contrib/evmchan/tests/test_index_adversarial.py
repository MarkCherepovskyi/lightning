# SPDX-License-Identifier: MIT
"""evmindex against nodes that answer wrongly (SPEC 5.2 draft 4): eth_getLogs
from another backend (lagging, clamped, on another fork), stale tags, failures
part-way, rate limits, deep reorgs, a fresh file, bounded RPC use, and the
secrets rule of evm.py."""
import http.server
import json
import random
import threading

import pytest

import btc
import evm
import evmindex
from fakechain import CONTRACT, FakeChain
from test_index_fake import all_blocks, check_chain, fresh


class TwoBackends(FakeChain):
    """Blocks, tags and eth_call from this (healthy) node; eth_getLogs from
    `logs_node` when set: a copy that lags, clamps, or is on another fork."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.logs_node = None
        self.clamp = None           # eth_getLogs' head, below which it answers

    def call(self, method, *params):
        if method == 'eth_getLogs' and (self.logs_node is not None or self.clamp is not None):
            self.calls.append((method, params))
            f = dict(params[0])
            src = self.logs_node or self
            head = len(src.blocks) - 1 if self.clamp is None else min(self.clamp, len(src.blocks) - 1)
            if int(f['fromBlock'], 16) > head:
                return []
            f['toBlock'] = evm.qty(min(int(f['toBlock'], 16), head))
            return FakeChain.call(src, method, f)
        return super().call(method, *params)


def grow(node, n, rng=None, every=3):
    """n more blocks, 2 s apart (one height each at INTERVAL 2), some with deposits."""
    for _ in range(n):
        ts = node.blocks[-1]['timestamp'] + 2
        txs = [node.deposit() for _ in range(rng.randrange(3))] if rng else \
            ([node.deposit()] if len(node.blocks) % every == 0 else [])
        node.add_block(ts, txs)


def heal(ix, node):
    """Healthy polls until one is proven (repairs back off after failures,
    up to MAX_REPAIR_WAIT polls)."""
    for _ in range(evmindex.MAX_REPAIR_WAIT + 2):
        grow(node, 1)
        if ix.sync()['proven']:
            return True
    return False


def test_lagging_logs_backend_loses_nothing(tmp_path):
    node = TwoBackends()
    ix = fresh(tmp_path, node)
    grow(node, 20)
    ix.sync()
    tip = ix.tip()
    lag = node.clone()
    grow(node, 10)
    node.logs_node = lag                      # 10 blocks behind, and silent about them
    r = ix.sync()
    assert not r['proven'] and ix.tip() == tip
    check_chain(ix, node)
    node.logs_node = None
    assert ix.sync()['proven'] and ix.tip() > tip
    check_chain(ix, node)


def test_other_fork_logs_backend(tmp_path):
    """eth_getLogs from a node on another fork: its rows never reach a served
    block, nothing proven is deleted, and the index heals once logs are honest."""
    node = TwoBackends()
    ix = fresh(tmp_path, node)
    grow(node, 15)
    ix.sync()
    proven = all_blocks(ix)
    fork = node.clone()
    grow(node, 10)
    forked = []
    for _ in range(10):                       # same heights, other deposits, other hashes
        forked.append(fork.deposit(amount=7))
        fork.add_block(fork.blocks[-1]['timestamp'] + 2, [forked[-1]])
    node.logs_node = fork
    for _ in range(3):
        ix.sync()
        assert all_blocks(ix)[:len(proven)] == proven
        check_chain(ix, node)
    node.logs_node = None
    assert heal(ix, node)
    check_chain(ix, node)
    grow(node, 2)
    ix.sync()           # rows above the tag are unproven: one more poll settles the last
    assert all(ix.utxo(btc.Tx.parse(d).txid_hex(), 0) is None for d in forked)


def test_clamped_then_healthy_never_loses_a_row(tmp_path):
    node = TwoBackends()
    ix = fresh(tmp_path, node)
    grow(node, 10)
    ix.sync()
    grow(node, 30)
    node.clamp = len(node.blocks) - 20        # answers only up to 20 blocks back
    ix.sync()
    node.clamp = None
    for _ in range(80):
        node.add_block(node.blocks[-1]['timestamp'] + 2)
    ix.sync()
    check_chain(ix, node)
    ix2 = fresh(tmp_path, node, 'healthy.sqlite3')
    ix2.sync()
    assert all_blocks(ix) == all_blocks(ix2)


@pytest.mark.parametrize('seed', range(6))
def test_no_served_row_is_ever_lost(tmp_path, seed):
    """Random polls against adversarial answers: stale tags, lagging, clamped
    and other-fork eth_getLogs. After every poll each served block equals the
    one built from the healthy chain, and the tip never goes down."""
    rng = random.Random(seed)
    node = TwoBackends()
    ix = fresh(tmp_path, node)
    last_tip = 0
    for _ in range(40):
        grow(node, rng.randrange(1, 6), rng)
        mode = rng.choice(['ok', 'ok', 'stale', 'lag', 'clamp', 'fork'])
        node.hide = rng.randrange(1, 4) if mode == 'stale' else 0
        node.clamp = len(node.blocks) - rng.randrange(1, 8) if mode == 'clamp' else None
        node.logs_node = None
        if mode in ('lag', 'fork'):
            other = node.clone()
            if mode == 'lag':
                other.reorg(len(other.blocks) - rng.randrange(1, 6))
            else:
                k = rng.randrange(1, 6)
                ts = other.blocks[-k - 1]['timestamp']
                other.reorg(len(other.blocks) - k)
                for i in range(k):
                    other.add_block(ts + 2 * (i + 1), [other.deposit(amount=9)])
            node.logs_node = other
        ix.sync()
        assert ix.tip() >= last_tip
        last_tip = ix.tip()
        node.hide = 0
        check_chain(ix, node)
    node.logs_node, node.clamp = None, None
    assert heal(ix, node)
    check_chain(ix, node)
    assert last_tip > 0


def test_failure_after_the_first_range_keeps_progress(tmp_path):
    class Flaky(FakeChain):
        fail_at = None

        def call(self, method, *params):
            if method == 'eth_getLogs':
                self.n = getattr(self, 'n', 0) + 1
                if self.n == self.fail_at:
                    raise evm.EvmError('eth_getLogs: URLError')
            return super().call(method, *params)

    node = Flaky()
    ix = fresh(tmp_path, node)
    first = node.deposit()
    c = evm.LOGS_CHUNK
    for i in range(c * 5 // 2):
        node.add_block(node.T(1) + i, [first] if i == 10 else [node.deposit()] if i in (c * 5 // 4, c * 9 // 4) else [])
    node.fail_at = 2
    with pytest.raises(evm.EvmError):
        ix.sync()
    assert ix.utxo(btc.Tx.parse(first).txid_hex(), 0)     # the first range's deposit is stored
    assert ix.tip() == 0 and ix.ibd
    calls = len(node.calls)
    r = ix.sync()
    starts = [int(p[0]['fromBlock'], 16) for m, p in node.calls[calls:] if m == 'eth_getLogs']
    assert starts[0] == node.deploy_block + c         # resumed, not restarted
    assert r['proven'] and not ix.ibd
    check_chain(ix, node)


def test_rate_limited_calls_are_retried(tmp_path):
    class Limited(FakeChain):
        left = 0

        def call(self, method, *params):
            if method == 'eth_getLogs' and self.left:
                self.left -= 1
                raise evm.EvmError('eth_getLogs: JSON-RPC error -32005', -32005)
            return super().call(method, *params)

    node = Limited()
    node.rate_limit_backoff = 0.01
    ix = fresh(tmp_path, node)
    grow(node, 30)
    node.left = 3                    # within rate_limit_retries: the sync still completes
    assert ix.sync()['proven']
    check_chain(ix, node)


def test_reorg_deeper_than_64_blocks(tmp_path):
    node = FakeChain()
    ix = fresh(tmp_path, node)
    for _ in range(15):
        grow(node, 10)
        ix.sync()
    assert len([1 for m, _ in node.calls if m == 'eth_getLogs']) >= 15
    fork_at = len(node.blocks) - 100
    ts = node.blocks[fork_at - 1]['timestamp']
    node.reorg(fork_at)
    for i in range(110):
        node.add_block(ts + 2 * (i + 1), [node.deposit(amount=5)] if i % 7 == 0 else [])
    r = ix.sync()
    assert r['proven'] and r['dropped'] > 0
    check_chain(ix, node)
    ix2 = fresh(tmp_path, node, 'fresh.sqlite3')
    ix2.sync()
    assert all_blocks(ix)[:len(all_blocks(ix2))] == all_blocks(ix2)


def test_fresh_file_reports_the_live_candidate(tmp_path):
    """SPEC 5.3: a new (or deleted) index file must not report a chain shorter
    than the one lightningd has seen."""
    node = FakeChain()
    old = fresh(tmp_path, node)
    grow(node, 50)
    old.sync()
    tip = old.tip()
    assert old.chaininfo() == {'chain': 'regtest', 'headercount': tip, 'blockcount': tip, 'ibd': False}
    new = fresh(tmp_path, node, 'new.sqlite3')
    info = new.chaininfo()
    assert info['ibd'] and info['blockcount'] == 0 and info['headercount'] >= tip
    new.sync()
    assert new.chaininfo() == {'chain': 'regtest', 'headercount': tip, 'blockcount': tip, 'ibd': False}
    # Reopened: ibd again until this process has a checkpoint, with the persisted tip.
    reopened = fresh(tmp_path, node, 'new.sqlite3')
    info = reopened.chaininfo()
    assert info['ibd'] and info['blockcount'] == tip and info['headercount'] >= tip


def test_rpc_calls_per_poll_are_bounded(tmp_path):
    """Many rows above the tag from an old fork (as receipts can leave): a
    poll's eth_getBlockByNumber calls stay within the confirm budget."""
    node = FakeChain()
    ix = fresh(tmp_path, node)
    grow(node, 5)
    ix.sync()
    base = len(node.blocks)
    fork = []
    for i in range(300):
        b = node.add_block(node.blocks[-1]['timestamp'] + 1, [node.deposit()])
        fork.append(node.receipt(b))
    for r in fork:
        ix.add_receipt(r)
    node.reorg(base)
    for _ in range(320):
        node.add_block(node.blocks[-1]['timestamp'] + 1)
    n = len(node.calls)
    ix.sync()
    gets = sum(1 for m, _ in node.calls[n:] if m == 'eth_getBlockByNumber')
    assert gets <= 2 + evmindex.MAX_CONFIRMS + 10, gets
    for _ in range(3):
        node.add_block(node.blocks[-1]['timestamp'] + 1)
        ix.sync()
    check_chain(ix, node)
    assert all(ix.utxo(t, 0) is None for r in fork[:5] for t in [btc.display(evm.unhex(r['logs'][0]['topics'][2]))])


def test_readers_see_a_monotonic_linked_chain(tmp_path):
    node = TwoBackends()
    ix = fresh(tmp_path, node)
    rng = random.Random(1)
    grow(node, 20, rng)
    ix.sync()
    errors, done = [], threading.Event()

    def reader():
        last = 0
        try:
            while not done.is_set():
                t = ix.tip()
                assert t >= last
                last = t
                prev = None
                for h in range(max(0, t - 8), t + 1):
                    b = ix.block(h)
                    if b is None:       # cannot happen: the tip never goes down
                        raise AssertionError('block {} <= tip {} missing'.format(h, t))
                    hdr = btc.parse_block(b[1])[0]
                    if prev is not None:
                        assert hdr[4:36] == btc.internal(prev), h
                    prev = b[0]
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for i in range(25):
            grow(node, rng.randrange(1, 4), rng)
            if i % 6 == 5:                     # a reorg above the checkpoint now and then
                k = 2
                ts = node.blocks[-k - 1]['timestamp']
                node.reorg(len(node.blocks) - k)
                for j in range(k + 1):
                    node.add_block(ts + 2 * (j + 1), [node.deposit(amount=3)])
            ix.sync()
    finally:
        done.set()
        for t in threads:
            t.join()
    assert not errors, errors[0]
    check_chain(ix, node)


# ------------------------------------------------------------ secrets (SPEC 5.2)

class _Server(http.server.BaseHTTPRequestHandler):
    replies = []

    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        status, body = self.replies.pop(0) if self.replies else (200, b'{"jsonrpc":"2.0","id":1,"result":"0x1"}')
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = http.server.HTTPServer(('127.0.0.1', 0), _Server)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield 'http://127.0.0.1:{}/v3/SECRETKEY123'.format(srv.server_address[1])
    srv.shutdown()
    srv.server_close()


def test_errors_hold_no_url_or_reply_text(server):
    rpc = evm.EvmRpc(server)
    rpc.rate_limit_backoff = 0.01
    err = json.dumps({'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32000, 'message': 'boom SECRETKEY123'}})
    _Server.replies[:] = [(200, err.encode()), (502, b'<html>SECRETKEY123 bad gateway</html>'),
                          (200, b'SECRETKEY123 not json')]
    for _ in range(3):
        with pytest.raises(evm.EvmError) as e:
            rpc.call('eth_blockNumber')
        assert 'SECRETKEY123' not in str(e.value) and 'boom' not in str(e.value) and 'html' not in str(e.value)
        assert 'eth_blockNumber' in str(e.value)
    for url in ('base-sepolia.infura.io/v3/SECRETKEY123', 'http://[::1/v3/SECRETKEY123',
                'http://127.0.0.1:1/SECRETKEY123'):
        with pytest.raises(evm.EvmError) as e:
            evm.EvmRpc(url, timeout=2).call('eth_chainId')
        assert 'SECRETKEY123' not in str(e.value) and e.value.__cause__ is None


def test_http_429_is_retried(server):
    rpc = evm.EvmRpc(server)
    rpc.rate_limit_backoff = 0.01
    _Server.replies[:] = [(429, b'Too Many Requests SECRETKEY123')] * 2
    assert rpc.request('eth_blockNumber') == '0x1'
    _Server.replies[:] = [(429, b'')] * 10
    with pytest.raises(evm.EvmError) as e:
        rpc.request('eth_blockNumber')
    assert e.value.rate_limited and 'SECRETKEY123' not in str(e.value)
    _Server.replies[:] = []


# ------------------------------------------------------------ second review's findings

class Blind(TwoBackends):
    """Tags fine, but eth_getBlockByNumber(<number>) answered by a backend
    that has no block above `blind_above` (None), and eth_getLogs clamped."""

    blind_above = None

    def _block(self, tag):
        if self.blind_above is not None and tag not in ('latest', 'safe', 'finalized') \
                and int(tag, 16) > self.blind_above:
            return None
        return super()._block(tag)


def test_a_missing_block_is_not_a_fork(tmp_path):
    node = Blind()
    ix = fresh(tmp_path, node)
    rng = random.Random(7)
    for _ in range(7):
        grow(node, 2, rng)
        ix.sync()
    blocks, tip, cp = all_blocks(ix), ix.tip(), ix.checkpoint()
    grow(node, 3, rng)
    node.blind_above = 2                       # every checkpoint block "missing"
    node.clamp = 8                             # and the logs clamped far below
    ix.sync()
    assert ix.tip() == tip and ix.checkpoint() == cp and all_blocks(ix) == blocks
    node.blind_above = node.clamp = None
    assert heal(ix, node)
    check_chain(ix, node)


def test_repair_keeps_our_receipt_above_the_tag(tmp_path):
    node = TwoBackends()
    ix = fresh(tmp_path, node)
    grow(node, 10)
    ix.sync()
    ours = node.deposit()
    b = node.add_block(node.blocks[-1]['timestamp'] + 2, [ours])
    assert ix.add_receipt(node.receipt(b))
    # An unrelated mismatch (a log the index missed, above the checkpoint and
    # at the next tag), with eth_getLogs lagging behind.
    node.add_block(node.blocks[-1]['timestamp'] + 2)
    missed = node.blocks[-3]
    assert missed['number'] > ix.checkpoint()[0]
    d = node.deposit()
    missed['logs'].append(node.make_log(missed, len(missed['logs']),
                                        (missed['timestamp'] - node.G) // node.I, btc.txid_of(d), d))
    node.clamp = len(node.blocks) - 4
    ix.sync()
    assert ix.utxo(btc.Tx.parse(ours).txid_hex(), 0)
    node.clamp = None
    assert heal(ix, node)
    assert ix.utxo(btc.Tx.parse(ours).txid_hex(), 0) and ix.utxo(btc.Tx.parse(d).txid_hex(), 0)
    check_chain(ix, node)


def test_tag_below_deploy_and_junk_logs_do_not_raise(tmp_path):
    class Odd(FakeChain):
        def call(self, method, *params):
            if method == 'eth_getLogs':
                return super().call(method, *params) + [None, 7, {'address': CONTRACT}, {
                    'address': CONTRACT, 'topics': [evmindex.TOPIC0, '0x' + '00' * 32, '0x' + '11' * 32],
                    'blockNumber': '0x6', 'blockHash': '0x' + '22' * 32, 'logIndex': '0x0'}]
            return super().call(method, *params)

    node = Odd()
    ix = fresh(tmp_path, node)
    node.hide = 0
    node.blocks[-1]['timestamp'] = node.G      # safe = deploy block - 1 until two more blocks
    r = ix.sync()
    assert not r['proven'] and ix.tip() == 0
    grow(node, 5)
    r = ix.sync()
    assert ix.undecodable >= 1                  # the data-less log: counted, not served
    check_chain(ix, node)


def test_persistent_mismatch_backs_off(tmp_path):
    """A provider that never returns one block's logs: repairs (full
    re-reads) are spaced out, so eth_getLogs calls stay bounded."""
    class Hole(FakeChain):
        hole = None

        def call(self, method, *params):
            out = super().call(method, *params)
            if method == 'eth_getLogs' and self.hole is not None:
                out = [x for x in out if int(x['blockNumber'], 16) != self.hole]
            return out

    node = Hole()
    ix = fresh(tmp_path, node)
    grow(node, 3)
    ix.sync()
    node.add_block(node.blocks[-1]['timestamp'] + 2, [node.deposit()])
    node.hole = len(node.blocks) - 1
    for _ in range(3000):
        node.add_block(node.blocks[-1]['timestamp'] + 1)
    n = len(node.calls)
    for _ in range(20):
        grow(node, 1)
        ix.sync()
    calls = sum(1 for m, _ in node.calls[n:] if m == 'eth_getLogs')
    assert calls <= 20 + 6 * 3, calls          # one per poll, plus at most ~6 re-reads of 2 ranges
    node.hole = None
    assert heal(ix, node)
    check_chain(ix, node)


def test_odd_error_replies_hold_no_secrets(server):
    rpc = evm.EvmRpc(server)
    _Server.replies[:] = [
        (200, json.dumps({'jsonrpc': '2.0', 'id': 1, 'error': {'code': 'SECRETKEY123', 'message': 5}}).encode()),
        (200, json.dumps({'jsonrpc': '2.0', 'id': 1, 'error': 'SECRETKEY123'}).encode()),
        (200, json.dumps(['SECRETKEY123']).encode())]
    for _ in range(3):
        with pytest.raises(evm.EvmError) as e:
            rpc.call('eth_blockNumber')
        assert 'SECRETKEY123' not in str(e.value)
