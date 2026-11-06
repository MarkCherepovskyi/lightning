# SPDX-License-Identifier: MIT
"""evmbackend.py without lightningd (SPEC 5, milestone M3): the send path
against anvil (already in chain, missing inputs, revert names, the gas cap,
the time limit with a pending send replaced, receipts making a child's
prevouts available), the hooks' decisions, the option check, the alarms,
and secrets kept out of logs. The plugin's methods are called directly, with
a fake log and a fake lightningd RPC."""
import base64
import json
import os
import re
import socket
import stat
import struct
import threading
import time

import pytest

import btc
import evm
import evmbackend as eb
from conftest import EVMCHAN, SAT, Key, sign, wait_until

A = Key(0xA11CE)
INTERVAL = 2
EXIT_TO = '0x000000000000000000000000000000000000dEaD'


# ------------------------------------------------------------ fixtures

class Log:
    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()

    def __call__(self, level, msg):
        with self.lock:
            self.lines.append((level, msg))

    def at(self, level):
        return [m for lv, m in self.lines if lv == level]

    def text(self):
        return '\n'.join('{} {}'.format(lv, m) for lv, m in self.lines)


class FakeLn:
    """lightningd's RPC: canned answers per method."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        a = self.answers[method]
        return a(params) if callable(a) else a


def make_cfg(tmp_path, url='http://127.0.0.1:1', contract='0x' + '11' * 20, key=None, secrets=(), min_eth=0.01):
    key = key or os.urandom(32)
    return eb.Config(url, evm.checksum_address(contract), key, str(tmp_path / 'evm-key'), 'safe', 1.0,
                     int(min_eth * 10**18), str(tmp_path), secrets=list(secrets) + [url, key.hex()])


def offline_backend(tmp_path, ln=None, log=None, checked=True):
    """A Backend for the hook tests: no EVM node, options checked and fine,
    a funded key."""
    b = eb.Backend(make_cfg(tmp_path), log or Log(), lnrpc=(lambda: ln) if ln else None)
    b.interval = INTERVAL
    b._chain_id = 31337
    if checked:
        b.apply_option_check(GOOD_CONFIGS, GOOD_INFO)
    b.balance = 10**18
    return b


@pytest.fixture
def backend(chain, tmp_path):
    """A Backend on a fresh contract with a funded key, its send worker and
    its tracker running (no poll or option-check thread: tests drive those)."""
    log = Log()
    b = eb.Backend(make_cfg(tmp_path, chain.rpc.url, chain.address), log)
    chain.rpc.call('anvil_setBalance', b.address, evm.qty(10**20))
    chain.mine_at(1)
    b.connect(retry_for=0)
    b.apply_option_check(GOOD_CONFIGS, GOOD_INFO)
    ts = [threading.Thread(target=f, daemon=True) for f in (b._worker_loop, b._tracker_loop)]
    for t in ts:
        t.start()
    b.log_capture = log
    b.test_threads = ts
    yield b
    b.stop()
    for t in ts:
        t.join(5)
    b.ix.close()


def deposit(chain, b, key=A, sats=100_000):
    rc = chain.deposit(key.p2wpkh, sats)
    b.ix.sync()
    log = [x for x in rc['logs'] if x['topics'][0] == '0x' + evm.keccak256(b'TxApplied(uint32,bytes32,bytes)').hex()][0]
    txid = evm.unhex(log['topics'][2])
    return txid, evm.abi_decode(['bytes'], evm.unhex(log['data']))[0]


def spend(b, prev_txid, vout, amount, key=A, out_script=None, fee=1000, locktime=0, sequence=0xffffffff):
    u = b.ix.utxo_internal(prev_txid, vout)
    assert u is not None
    tx = btc.Tx(2, [btc.TxIn(prev_txid, vout, b'', sequence)],
                [(amount - fee, out_script or key.p2wpkh)], locktime)
    return sign(tx, [key], [(u['amount'], u['script'], u['height'])])


# ------------------------------------------------------------ the selector table

def _sol_errors():
    src = open(os.path.join(EVMCHAN, 'contracts', 'src', 'Errors.sol')).read()
    out = set()
    for m in re.finditer(r'^error\s+(\w+)\(([^)]*)\)\s*;', src, re.M):
        args = [a.strip().split()[0] for a in m.group(2).split(',') if a.strip()]
        out.add('{}({})'.format(m.group(1), ','.join(args)))
    return out


def test_selector_table_matches_errors_sol_and_abi():
    for sel, sig in eb.ERRORS.items():
        assert evm.selector(sig).hex() == sel, sig
    ours = set(eb.ERRORS.values()) - {'Error(string)', 'Panic(uint256)'}
    assert ours == _sol_errors()
    artifact = os.path.join(EVMCHAN, 'contracts', 'out', 'UtxoChain.sol', 'UtxoChain.json')
    if not os.path.exists(artifact):
        pytest.skip('contracts/out not built')
    abi = json.load(open(artifact))['abi']
    abi_errors = {'{}({})'.format(e['name'], ','.join(i['type'] for i in e['inputs'])) for e in abi
                  if e['type'] == 'error'}
    assert abi_errors == ours


def test_revert_reason_decoding():
    def err(data, code=3):
        return evm.EvmError('eth_call: JSON-RPC error 3', code, data)
    assert eb.revert_reason(err('0x' + eb_sel('NonFinal()'))) == 'NonFinal'
    assert eb.revert_reason(err('0x' + eb_sel('BadSignature(uint256)') + (1).to_bytes(32, 'big').hex())) \
        == 'BadSignature(1)'
    assert eb.revert_reason(err({'data': '0x' + eb_sel('TooEarly()')})) == 'TooEarly'
    assert eb.revert_reason(err('0x' + eb_sel('Error(string)') + evm.abi_encode(['string'], ['boom']).hex())) \
        == 'Error'          # the node's text is never copied
    assert eb.revert_reason(err('0x' + eb_sel('Panic(uint256)') + (0x11).to_bytes(32, 'big').hex())) \
        == 'Panic(0x11)'
    assert eb.revert_reason(err('0xdeadbeef')) == 'reverted with an unknown error'
    assert eb.revert_reason(err(None)) == 'reverted'
    assert eb.revert_reason(err('0x')) == 'reverted'


def eb_sel(sig):
    return evm.selector(sig).hex()


# ------------------------------------------------------------ the send path, on anvil

def test_already_in_chain_sends_nothing(chain, backend):
    txid, raw = deposit(chain, backend)
    n = chain.rpc.nonce(backend.address)
    assert backend.send_now(raw.hex()) == {'success': True, 'errmsg': ''}
    # and a submitted tx, re-sent
    tx = spend(backend, txid, 0, 100_000)
    assert backend.send_now(tx.serialize().hex())['success']
    n2 = chain.rpc.nonce(backend.address)
    assert backend.send_now(tx.serialize().hex())['success']
    assert chain.rpc.nonce(backend.address) == n2 == n + 1


def test_missing_or_spent_inputs(chain, backend):
    tx = btc.Tx(2, [btc.TxIn(os.urandom(32), 0)], [(1000, A.p2wpkh)], 0, [[b'\x01']])
    assert backend.send_now(tx.serialize().hex()) == {'success': False, 'errmsg': 'missing or spent inputs'}
    txid, _ = deposit(chain, backend)
    t1 = spend(backend, txid, 0, 100_000, fee=1000)
    t2 = spend(backend, txid, 0, 100_000, fee=2000)     # a double spend of the same deposit
    assert backend.send_now(t1.serialize().hex())['success']
    assert backend.send_now(t2.serialize().hex())['errmsg'] == 'missing or spent inputs'


def test_bad_tx_does_not_parse(backend):
    assert backend.send_now('00ff')['errmsg'] == 'the transaction does not parse'
    assert backend.send_now('zz')['errmsg'] == 'the transaction does not parse'


def test_revert_names(chain, backend):
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    sig = bytearray(tx.witnesses[0][0])
    sig[10] ^= 1
    tx.witnesses[0][0] = bytes(sig)
    n = chain.rpc.nonce(backend.address)
    r = backend.send_now(tx.serialize().hex())
    assert not r['success'] and re.fullmatch(r'Bad\w+\(0\)', r['errmsg']), r
    # A height lock far ahead: NonFinal, and CLN will retry it.
    h = chain.height_now()
    late = spend(backend, txid, 0, 100_000, locktime=h + 50, sequence=0xfffffffe)
    assert backend.send_now(late.serialize().hex()) == {'success': False, 'errmsg': 'NonFinal'}
    assert chain.rpc.nonce(backend.address) == n      # no gas spent on either


def test_gas_cap(chain, backend, monkeypatch):
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    monkeypatch.setattr(eb, 'GAS_CAP', 20_000)
    r = backend.send_now(tx.serialize().hex())
    assert not r['success']
    m = re.fullmatch(r'too large for one EVM tx: (\d+) gas', r['errmsg'])
    assert m and int(m.group(1)) > 20_000
    assert backend.ix.tx(tx.txid_hex()) is None


def test_receipt_makes_a_childs_prevouts_available(chain, backend):
    txid, _ = deposit(chain, backend)
    parent = spend(backend, txid, 0, 100_000)
    assert backend.send_now(parent.serialize().hex())['success']
    # No sync in between: the worker ingests the receipt right after the
    # answer, before it takes the next job (SPEC 5.2), e.g. a CPFP child.
    wait_until(lambda: backend.ix.utxo(parent.txid_hex(), 0) is not None, 2)
    assert backend.ix.utxo(parent.txid_hex(), 0)['amount'] == 99_000
    assert backend.getutxout(btc.display(txid), 0) == {'amount': None, 'script': None}
    child = spend(backend, parent.txid(), 0, 99_000)
    assert backend.send_now(child.serialize().hex())['success']
    wait_until(lambda: backend.ix.utxo(child.txid_hex(), 0) is not None, 2)
    assert backend.getutxout(child.txid_hex(), 0) == {'amount': 98_000, 'script': A.p2wpkh.hex()}
    # and the contract agrees with the index
    assert chain.is_unspent(child.txid(), 0, 98_000, A.p2wpkh, backend.ix.tx(child.txid_hex())['height'])


def txpool_pending(chain):
    return int(chain.rpc.request('txpool_status')['pending'], 16)


def test_success_on_accept_then_replaced_at_the_same_nonce(chain, backend):
    """SPEC 5.3 steps 4, 5 and 7: success once the node accepts it; the same
    txid again is answered at once without sending; still pending after
    INTERVAL, it is replaced at the same nonce at >= 1.25x the fees."""
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    chain.rpc.call('evm_setAutomine', False)
    try:
        t0 = time.monotonic()
        assert backend.send_now(tx.serialize().hex()) == {'success': True, 'errmsg': ''}
        assert time.monotonic() - t0 < 2 * INTERVAL
        assert len(backend.pending) == 1 and txpool_pending(chain) == 1
        p = list(backend.pending.values())[0]
        first = chain.rpc.request('eth_getTransactionByHash', p.hashes[0])
        n_sent = len(p.hashes)
        t1 = time.monotonic()
        assert backend.send_now(tx.serialize().hex())['success']        # step 5: at once
        assert time.monotonic() - t1 < 0.2 and len(p.hashes) == n_sent
        wait = time.monotonic() + 3 * INTERVAL + 2
        while len(p.hashes) < 2 and time.monotonic() < wait:
            time.sleep(0.1)
        assert len(p.hashes) >= 2
        # The newest version (on a slow machine a third may already have
        # replaced the second in anvil's pool).
        second = chain.rpc.request('eth_getTransactionByHash', p.hashes[-1])
        assert second['nonce'] == first['nonce']
        assert int(second['maxFeePerGas'], 16) * 4 >= int(first['maxFeePerGas'], 16) * 5
        assert int(second['maxPriorityFeePerGas'], 16) * 4 >= int(first['maxPriorityFeePerGas'], 16) * 5
        assert txpool_pending(chain) == 1
        assert any('replaced submit ' + tx.txid_hex() in m for m in backend.log_capture.at('info'))
        # Tracked sends survive a restart (evm-pending.json: calldata, no signatures).
        saved = json.load(open(backend.pending_file))['pending']
        assert [d['nonce'] for d in saved] == [p.nonce] and saved[0]['hashes'] == p.hashes
        assert backend.cfg.key.hex() not in json.dumps(saved)
        again = eb.Backend(backend.cfg, Log())
        again.connect(retry_for=0)
        assert list(again.pending) == [p.nonce] and again.pending[p.nonce].data == p.data
        assert again._known(tx.txid_hex())
        with again._klock:
            assert again._alloc_nonce() == p.nonce + 1
        again.ix.close()
        chain.mine()
    finally:
        chain.rpc.call('evm_setAutomine', True)
    end = time.monotonic() + 10
    while (backend.pending or backend.ix.tx(tx.txid_hex()) is None) and time.monotonic() < end:
        time.sleep(0.1)
    assert not backend.pending and json.load(open(backend.pending_file))['pending'] == []
    assert backend.ix.tx(tx.txid_hex()) is not None        # ingested from the replacement's receipt
    assert backend.send_now(tx.serialize().hex())['success']


def test_resigned_version_replaces_at_the_same_nonce(chain, backend):
    """Step 6: onchaind re-signs a sweep (same prevout, more fee): the
    tracked submit is replaced at its nonce, never given a new one."""
    txid, _ = deposit(chain, backend)
    v1 = spend(backend, txid, 0, 100_000, fee=1000)
    v2 = spend(backend, txid, 0, 100_000, fee=3000)
    chain.rpc.call('evm_setAutomine', False)
    try:
        assert backend.send_now(v1.serialize().hex())['success']
        p = list(backend.pending.values())[0]
        f1 = int(chain.rpc.request('eth_getTransactionByHash', p.hashes[0])['maxFeePerGas'], 16)
        r2 = backend.send_now(v2.serialize().hex())
        assert r2['success'], r2
        assert list(backend.pending) == [p.nonce] and p.btc_txid == v2.txid_hex()
        h2 = chain.rpc.request('eth_getTransactionByHash', p.hashes[-1])
        assert int(h2['nonce'], 16) == p.nonce and int(h2['maxFeePerGas'], 16) * 4 >= f1 * 5
        assert txpool_pending(chain) == 1
        assert backend.send_now(v1.serialize().hex())['success']        # a known version: at once
        assert txpool_pending(chain) == 1 and p.btc_txid == v2.txid_hex()
        chain.mine()
    finally:
        chain.rpc.call('evm_setAutomine', True)
    end = time.monotonic() + 10
    while backend.pending and time.monotonic() < end:
        time.sleep(0.1)
    assert backend.ix.tx(v2.txid_hex()) is not None and backend.ix.tx(v1.txid_hex()) is None


def test_filler_self_send_frees_an_untracked_nonce(chain, backend):
    """Step 7: a tx at our next nonce that we don't track (a crash before
    persisting, a lost pending file) gets a 0-value self-send at that nonce
    with bumped fees, so later sends aren't wedged behind it."""
    chain.rpc.call('evm_setAutomine', False)
    try:
        n = chain.rpc.nonce(backend.address, 'latest')
        tip, max_fee = chain.rpc.fees()
        raw, h = backend.account.sign_tx({'chainId': chain.chain_id, 'nonce': n, 'maxPriorityFeePerGas': tip,
                                          'maxFeePerGas': max_fee, 'gas': 21_000, 'to': EXIT_TO, 'value': 1,
                                          'data': b''})
        chain.rpc.send_raw(raw)
        end = time.monotonic() + 10
        while not any(q.kind == 'filler' for q in list(backend.pending.values())) and time.monotonic() < end:
            time.sleep(0.1)
        f = backend.pending[n]
        assert f.kind == 'filler' and f.to == backend.address and f.value == 0
        got = chain.rpc.request('eth_getTransactionByHash', f.hashes[0])
        assert int(got['nonce'], 16) == n and int(got['maxFeePerGas'], 16) * 4 >= max_fee * 5
        assert chain.rpc.request('eth_getTransactionByHash', h) is None     # replaced in the pool
        chain.mine()
    finally:
        chain.rpc.call('evm_setAutomine', True)
    end = time.monotonic() + 10
    while backend.pending and time.monotonic() < end:
        time.sleep(0.1)
    assert not backend.pending and chain.rpc.nonce(backend.address, 'latest') == n + 1
    txid, _ = deposit(chain, backend)
    assert backend.send_now(spend(backend, txid, 0, 100_000).serialize().hex())['success']


@pytest.mark.parametrize('restart', [False, True])
def test_reorg_taking_back_a_mined_nonce_resends_it(chain, backend, restart):
    """M4 (found by tests/test_evmchan.py::test_reorg): a reorg drops the
    block that mined our submit, so the chain's nonce goes back below the
    one we saw mined. mined_floor used to hide that gap from the filler for
    good: every later send queued behind it, the key wedged, and the tx was
    lost (CLN doesn't rebroadcast a wallet tx). Now the tracker re-sends the
    same tx at that nonce, and later sends get mined. With a restart between
    the send and the reorg: the sends seen mined are persisted, so it is
    re-sent then too, not replaced by a 0-value filler."""
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    assert backend.send_now(tx.serialize().hex())['success']
    wait_until(lambda: not backend.pending and backend.ix.tx(tx.txid_hex()) is not None, 5)
    n = chain.rpc.nonce(backend.address, 'latest')
    assert backend.mined_floor == n
    b = backend
    if restart:
        backend.stop()
        for t in backend.test_threads:
            t.join(5)
        b = eb.Backend(backend.cfg, Log())
        b.connect(retry_for=0)
        assert list(b.recent) == [n - 1] and b.mined_floor == n and not b.pending
        b.apply_option_check(GOOD_CONFIGS, GOOD_INFO)
        ts = [threading.Thread(target=f, daemon=True) for f in (b._worker_loop, b._tracker_loop)]
        for t in ts:
            t.start()
    try:
        assert chain.rpc.call('anvil_reorg', 1, []) is None        # anvil drops the block's txs
        assert chain.rpc.nonce(b.address, 'latest') == n - 1
        assert not [x for x in chain.logs() if x['topics'][2] == '0x' + tx.txid().hex()]
        wait_until(lambda: chain.rpc.nonce(b.address, 'latest') == n, 4 * INTERVAL + 5)
        again = [x for x in chain.logs() if x['topics'][2] == '0x' + tx.txid().hex()]
        assert len(again) == 1                                  # the same Bitcoin tx, mined again
        log = b._log
        assert any('a reorg took back nonces [{}]'.format(n - 1) in m for m in log.at('unusual'))
        assert not any('0-value self-send' in m for m in log.at('unusual'))
        wait_until(lambda: not b.pending, 5)
        b.ix.sync()        # the poll thread's job: the old fork's row goes (SPEC 5.2)
        assert b.ix.tx(tx.txid_hex())['evm_hash'] == again[0]['blockHash']
        child = spend(b, tx.txid(), 0, 99_000)
        r = b.send_now(child.serialize().hex())
        assert r['success'], r
        wait_until(lambda: chain.rpc.nonce(b.address, 'latest') == n + 1, 5)
        assert [x for x in chain.logs() if x['topics'][2] == '0x' + child.txid().hex()]
    finally:
        if restart:
            b.stop()
            for t in ts:
                t.join(5)
            b.ix.close()


def test_persisted_before_sending(chain, backend, monkeypatch):
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    seen = []
    push = backend._push

    def spy(raw):
        seen.append([(d['nonce'], d['btc_txid']) for d in json.load(open(backend.pending_file))['pending']])
        return push(raw)
    monkeypatch.setattr(backend, '_push', spy)
    assert backend.send_now(tx.serialize().hex())['success']
    assert seen and seen[0] == [(seen[0][0][0], tx.txid_hex())]


def test_per_job_budget_counts_from_dequeue(chain, backend):
    """Step 8: a job's budget starts when it is dequeued; a job whose reply
    already went out (timed out in the queue) is never sent."""
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    n = chain.rpc.nonce(backend.address)
    late = eb.Reply(lambda r: None)                     # no timer: only the job's own budget
    backend._send_job(tx, late, time.monotonic() - backend.time_limit())
    assert late.result == {'success': False, 'errmsg': 'timed out before sending: not sent'}
    assert chain.rpc.nonce(backend.address) == n and not backend.pending
    # Block the worker past the reply's deadline: the queued send times out unsent.
    gate = threading.Event()
    backend.jobs.put(lambda dq: gate.wait(10))
    got = []
    reply = backend.sendrawtransaction(tx.serialize().hex(), got.append)
    reply.event.wait(2 * INTERVAL + 1)
    assert got and not got[0]['success'] and 'not sent' in got[0]['errmsg']
    gate.set()
    time.sleep(1)
    assert chain.rpc.nonce(backend.address) == n and not backend.pending
    # On time, from dequeue: sent.
    fresh = eb.Reply(lambda r: None)
    backend._send_job(tx, fresh, time.monotonic())
    assert fresh.result == {'success': True, 'errmsg': ''}


def test_committed_send_is_never_reported_failed(chain, backend, monkeypatch):
    """The deadline passing while the tx is being handed to the node
    answers success: it is persisted and tracked (CLN must not unreserve)."""
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    push = backend._push

    def slow(raw):
        time.sleep(2 * INTERVAL)
        return push(raw)
    monkeypatch.setattr(backend, '_push', slow)
    got = []
    t0 = time.monotonic()
    reply = backend.sendrawtransaction(tx.serialize().hex(), got.append)
    reply.event.wait(3 * INTERVAL)
    assert time.monotonic() - t0 < 2 * INTERVAL
    assert got == [{'success': True, 'errmsg': ''}]
    end = time.monotonic() + 10
    while backend.ix.tx(tx.txid_hex()) is None and time.monotonic() < end:
        time.sleep(0.1)
    assert backend.ix.tx(tx.txid_hex()) is not None


class HangingServer:
    """Accepts connections and never answers."""

    def __init__(self):
        self.s = socket.socket()
        self.s.bind(('127.0.0.1', 0))
        self.s.listen(16)
        self.conns = []
        self.url = 'http://127.0.0.1:{}'.format(self.s.getsockname()[1])
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            self.conns.append(c)

    def close(self):
        for c in self.conns:
            c.close()
        self.s.close()


def test_time_limit_holds_when_the_node_hangs(chain, backend):
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    hang = HangingServer()
    try:
        backend.srpc = evm.EvmRpc(hang.url, timeout=30)
        t0 = time.monotonic()
        got = []
        reply = backend.sendrawtransaction(tx.serialize().hex(), got.append)
        reply.event.wait(10)
        took = time.monotonic() - t0
        assert took < 2 * INTERVAL, took
        assert got and not got[0]['success'] and 'timed out' in got[0]['errmsg']
        # Read methods answer meanwhile (they never wait on a send).
        t1 = time.monotonic()
        backend.getchaininfo()
        backend.getutxout(btc.display(txid), 0)
        backend.getrawblockbyheight(0)
        assert time.monotonic() - t1 < 0.5
    finally:
        hang.close()


def test_read_methods_and_ibd(chain, tmp_path):
    log = Log()
    b = eb.Backend(make_cfg(tmp_path, chain.rpc.url, chain.address), log)
    chain.mine_at(3)
    chain.mine_at(4)                       # `safe` is the block at height 3
    b.connect(retry_for=0)
    ci = b.getchaininfo()
    assert ci['chain'] == 'regtest' and ci['ibd'] is True and ci['blockcount'] == 0
    assert ci['headercount'] == 2          # from the live read at startup, never below the tip
    chain.mine()
    b.poll_once()
    ci = b.getchaininfo()
    assert ci['ibd'] is False and ci['headercount'] == ci['blockcount'] == b.ix.tip() >= 1
    blk = b.getrawblockbyheight(1)
    hdr, txs = btc.parse_block(bytes.fromhex(blk['block']))
    assert btc.display(btc.block_hash(hdr)) == blk['blockhash']
    assert b.getrawblockbyheight(b.ix.tip() + 1) == {'blockhash': None, 'block': None}
    assert b.getrawblockbyheight('x') == {'blockhash': None, 'block': None}
    assert b.getutxout('00' * 32, 0) == {'amount': None, 'script': None}
    assert b.getutxout('nothex', 0) == {'amount': None, 'script': None}
    assert b.estimatefees() == eb.ESTIMATEFEES
    b.ix.close()


def test_deposit_withdraw_status(chain, backend):
    ln = FakeLn({'newaddr': {'p2tr': btc.segwit_address('bcrt', 1, A.xonly)}})
    backend.lnrpc = lambda: ln
    d = backend.deposit(50_000)
    assert d['status'] == 'mined' and d['amount_sat'] == 50_000 and d['address'].startswith('bcrt1p')
    assert backend.ix.utxo(d['txid'], 0) == {'amount': 50_000, 'script': A.p2tr, 'height': d['height']}
    with pytest.raises(eb.UserError, match='BadDepositScript'):
        backend.deposit(1000, backend.exitaddr(backend.address)['address'])
    with pytest.raises(eb.UserError, match='nothing to withdraw'):
        backend.withdraw()
    # Pay an exit to our own address, then withdraw it.
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000, out_script=btc.exit_script(backend.address), fee=500)
    assert backend.send_now(tx.serialize().hex())['success']
    assert backend.withdrawable() == 100_000 * SAT        # 99,500 exit + 500 fee to the submitter
    before = int(chain.rpc.request('eth_getBalance', backend.address, 'latest'), 16)
    w = backend.withdraw()
    rc = chain.rpc.request('eth_getTransactionReceipt', w['evm_tx'])
    after = int(chain.rpc.request('eth_getBalance', backend.address, 'latest'), 16)
    assert after - before == 100_000 * SAT - int(rc['gasUsed'], 16) * int(rc['effectiveGasPrice'], 16)
    s = backend.status()
    for k in ('contract', 'chain_id', 'eth_address', 'balance_wei', 'withdrawable_wei', 'tip', 'current_height',
              'lag', 'pending_sends', 'alarms', 'opens_refused'):
        assert k in s
    assert s['withdrawable_wei'] == 0 and s['pending_sends'] == [] and s['opens_refused'] is None
    assert backend.exitaddr(backend.address)['address'].startswith('bcrt1s')


# ------------------------------------------------------------ hooks

GOOD_CONFIGS = {'watchtime-blocks': {'value_int': 40}, 'cltv-delta': {'value_int': 40},
                'cltv-final': {'value_int': 40}, 'funding-confirms': {'value_int': 1},
                'max-concurrent-htlcs': {'value_int': 30}, 'developer': {'set': True},
                'dev-bitcoind-poll': {'value_int': 1}}
# Real init/node features of this tree's lightningd with --dev-force-features=-63.
GOOD_INFO = {'our_features': {'init': '08a0880a8a59a1', 'node': '88a0880a8a59a1'}}


def features(*bits):
    return '{:x}'.format(sum(1 << b for b in bits))


def test_peer_connected(tmp_path):
    b = offline_backend(tmp_path)
    assert b.peer_connected({'id': '02' * 33, 'features': GOOD_INFO['our_features']['init']}) \
        == {'result': 'continue'}
    assert b.peer_connected({'id': '02' * 33, 'features': features(12, 22)})['result'] == 'continue'
    for f in (features(12, 13, 20, 21), '', None, 'zz'):
        assert b.peer_connected({'id': '02' * 33, 'features': f})['result'] == 'disconnect'


def refused(r):
    return 'return' in r and r['return']['error']['code'] == eb.ERR_REFUSED


def test_rpc_command_channel_types(tmp_path):
    b = offline_backend(tmp_path, ln=FakeLn({'listfunds': {'outputs': []}}))
    rc = b.rpc_command
    assert rc({'method': 'fundchannel', 'params': {'id': 'x', 'amount': 1000}}) == {'result': 'continue'}
    for ok_t in ([12, 22], [22, 12], [12, 22, 46], [12, 22, 50], [12, 22, 46, 50]):
        assert rc({'method': 'fundchannel', 'params': {'id': 'x', 'amount': 1000, 'channel_type': ok_t}}) \
            == {'result': 'continue'}, ok_t
    for bad in ([12], [], [22], [12, 22, 99], [12, 20], 'anchors', [12, '22'], [True, 22]):
        for m in ('fundchannel', 'fundchannel_start', 'openchannel_init'):
            assert refused(rc({'method': m, 'params': {'id': 'x', 'amount': 1000, 'channel_type': bad}})), (m, bad)
        assert refused(rc({'method': 'multifundchannel', 'params': {
            'destinations': [{'id': 'x', 'amount': 1}, {'id': 'y', 'amount': 1, 'channel_type': bad}]}}))
    # Positional: channel_type is fundchannel's 13th parameter (the spender's order).
    pos = ['x', 1000] + [None] * 10 + [[12]]
    assert refused(rc({'method': 'fundchannel', 'params': pos}))
    pos[12] = [12, 22]
    assert rc({'method': 'fundchannel', 'params': pos}) == {'result': 'continue'}
    assert refused(rc({'method': 'fundchannel_start', 'params': ['x', 1000, None, None, None, None, None, None,
                                                                 [12]]}))
    assert refused(rc({'method': 'fundchannel', 'params': list(range(20))}))     # unreadable


def test_rpc_command_leases_assets_and_refusals(tmp_path):
    b = offline_backend(tmp_path, ln=FakeLn({'listfunds': {'outputs': []}}))
    rc = b.rpc_command
    assert refused(rc({'method': 'fundchannel', 'params': {'id': 'x', 'amount': 1000, 'request_amt': 5000}}))
    assert refused(rc({'method': 'fundchannel', 'params': {'id': 'x', 'amount': 1000, 'compact_lease': 'ab'}}))
    assert refused(rc({'method': 'openchannel_init', 'params': {'id': 'x', 'amount': 1, 'initialpsbt': P2WPKH_PSBT,
                                                                'request_amt': '1000sat'}}))
    assert rc({'method': 'fundchannel', 'params': {'id': 'x', 'amount': 1000, 'request_amt': 0}}) \
        == {'result': 'continue'}
    assert refused(rc({'method': 'fundchannel_start', 'params': {'id': 'x', 'amount': 1, 'asset': 'USDT'}}))
    # Low balance, unchecked options, bad options: every open refused.
    b.balance = 10**15
    assert refused(rc({'method': 'fundchannel', 'params': {'id': 'x', 'amount': 1000}}))
    b.balance = 10**18
    b.options_checked = False
    assert refused(rc({'method': 'openchannel_init', 'params': {'id': 'x', 'amount': 1, 'initialpsbt': P2WPKH_PSBT}}))
    b.apply_option_check({}, {})
    assert refused(rc({'method': 'multifundchannel', 'params': {'destinations': [{'id': 'x', 'amount': 1}]}}))
    assert refused(rc({'method': 'openchannel_signed', 'params': {'channel_id': 'c', 'signed_psbt': P2WPKH_PSBT}}))
    # ... but not other spending
    assert rc({'method': 'withdraw', 'params': {'destination': 'x', 'satoshi': 1000}}) == {'result': 'continue'}


def test_dev_no_outbound_filter(tmp_path):
    """Tests only: with evm-dev-no-outbound-filter (accepted only under
    lightningd --developer) our own opens skip the rpc_command checks, so a
    peer's openchannel* hooks are what refuses them; those hooks still refuse."""
    opt = options(**{eb.DEV_NO_OUTBOUND_FILTER: True})
    with pytest.raises(eb.ConfigError, match='--developer'):
        eb.Config.from_options(opt, str(tmp_path), {}, None)
    assert not eb.Config.from_options(options(), str(tmp_path), {}, None, developer=True).dev_no_outbound_filter
    cfg = eb.Config.from_options(opt, str(tmp_path), {}, None, developer=True)
    assert cfg.dev_no_outbound_filter
    b = eb.Backend(cfg, Log())
    b.interval, b._chain_id, b.balance = INTERVAL, 31337, 10**18
    b.apply_option_check(GOOD_CONFIGS, GOOD_INFO)
    bad = psbt_v0([(P2PKH, 'nonwitness')])
    for req in ({'method': 'fundchannel', 'params': {'id': 'x', 'amount': 1000, 'channel_type': [12]}},
                {'method': 'openchannel_init', 'params': ['x', 1000, bad]},
                {'method': 'openchannel_update', 'params': ['c', bad]}):
        assert b.rpc_command(req) == {'result': 'continue'}
    assert b.openchannel(open_payload(channel_type={'bits': [12]}))['result'] == 'reject'
    assert b.openchannel2_psbt({'channel_id': 'c', 'psbt': bad}, 'openchannel2_changed') == {'result': 'reject'}


def test_no_utxo_count_precheck(tmp_path):
    """Draft 5 dropped the 200-UTXO rule (dust deposits could block every
    withdraw); sendrawtransaction step 3 answers it. Nothing calls listfunds."""
    ln = FakeLn({})
    b = offline_backend(tmp_path, ln=ln)
    utx = ['{}:{}'.format('00' * 32, i) for i in range(500)]
    assert b.rpc_command({'method': 'fundchannel', 'params': {'id': 'x', 'amount': 'all', 'utxos': utx}}) \
        == {'result': 'continue'}
    assert b.rpc_command({'method': 'withdraw', 'params': ['x', 'all']}) == {'result': 'continue'}
    assert not ln.calls


def open_payload(**kw):
    p = {'id': '02' * 33, 'funding_msat': 10**9, 'max_accepted_htlcs': 30, 'to_self_delay': 40,
         'channel_type': {'bits': [12, 22], 'names': ['static_remotekey/even', 'anchors/even']}}
    p.update(kw)
    return p


def test_openchannel_hooks(tmp_path):
    b = offline_backend(tmp_path)
    assert b.openchannel(open_payload()) == {'result': 'continue'}
    assert b.openchannel(open_payload(channel_type={'bits': [12, 22, 46, 50]})) == {'result': 'continue'}
    for bad in (open_payload(channel_type={'bits': [12]}), open_payload(max_accepted_htlcs=31),
                open_payload(channel_type=None), open_payload(max_accepted_htlcs='x')):
        r = b.openchannel(bad)
        assert r['result'] == 'reject' and r['error_message'], bad
    assert b.openchannel2(open_payload()) == {'result': 'continue'}
    assert b.openchannel2(open_payload(requested_lease_msat=10**6, lease_blockheight_start=100))['result'] == 'reject'
    assert b.openchannel2(open_payload(max_accepted_htlcs=483))['result'] == 'reject'
    b.apply_option_check({'developer': {'set': True}}, {})
    assert b.openchannel(open_payload())['result'] == 'reject'
    assert 'SPEC 5.6' in b.openchannel2(open_payload())['error_message']


# --- PSBTs for the dual-funding hooks

def _kv(key, val):
    return btc.varint(len(key)) + key + btc.varint(len(val)) + val


def _witness_utxo(amount, spk):
    return struct.pack('<Q', amount) + btc.varint(len(spk)) + spk


def psbt_v0(inputs):
    """inputs: [(spk, kind)] kind 'witness', 'nonwitness', 'both', 'none', 'badprev', 'redeem'."""
    prevs = []
    for spk, kind in inputs:
        ptx = btc.Tx(2, [btc.TxIn(os.urandom(32), 0)], [(5000, b'\x6a'), (10_000, spk)], 0)
        prevs.append(ptx)
    utx = btc.Tx(2, [btc.TxIn(p.txid(), 1) for p in prevs], [(1000, b'\x00\x14' + bytes(20))], 0)
    out = b'psbt\xff' + _kv(b'\x00', utx.serialize(witness=False)) + b'\x00'
    for (spk, kind), ptx in zip(inputs, prevs):
        m = b''
        if kind in ('witness', 'both', 'redeem'):
            m += _kv(b'\x01', _witness_utxo(10_000, spk))
        if kind in ('nonwitness', 'both'):
            m += _kv(b'\x00', ptx.serialize())
        if kind == 'badprev':
            other = btc.Tx(2, [btc.TxIn(os.urandom(32), 0)], [(5000, b'\x6a'), (10_000, spk)], 0)
            m += _kv(b'\x00', other.serialize())
        if kind == 'redeem':
            m += _kv(b'\x04', b'\x00\x14' + bytes(20))
        m += _kv(b'\xfc\x09lightning\x01', b'\x00' * 8)      # a serial_id, as CLN adds
        out += m + b'\x00'
    out += b'\x00'
    return base64.b64encode(out).decode()


def psbt_v2(spks):
    g = _kv(b'\x02', struct.pack('<I', 2)) + _kv(b'\x04', btc.varint(len(spks))) + \
        _kv(b'\x05', btc.varint(1)) + _kv(b'\xfb', struct.pack('<I', 2))
    out = b'psbt\xff' + g + b'\x00'
    for spk in spks:
        out += _kv(b'\x0e', os.urandom(32)) + _kv(b'\x0f', struct.pack('<I', 0)) + \
            _kv(b'\x01', _witness_utxo(10_000, spk)) + b'\x00'
    out += _kv(b'\x03', struct.pack('<Q', 1000)) + _kv(b'\x04', b'\x00\x14' + bytes(20)) + b'\x00'
    return base64.b64encode(out).decode()


P2WPKH = b'\x00\x14' + bytes(range(20))
P2TR = b'\x51\x20' + bytes(range(32))
P2SH = b'\xa9\x14' + bytes(20) + b'\x87'
P2PKH = b'\x76\xa9\x14' + bytes(20) + b'\x88\xac'
P2WSH = b'\x00\x20' + bytes(32)
P2WPKH_PSBT = psbt_v0([(P2WPKH, 'witness')])


def test_psbt_checks():
    assert eb.psbt_problem(psbt_v0([(P2WPKH, 'witness'), (P2TR, 'nonwitness'), (P2TR, 'both')])) is None
    assert eb.psbt_problem(psbt_v2([P2WPKH, P2TR])) is None
    for bad in (psbt_v0([(P2WPKH, 'witness'), (P2SH, 'witness')]), psbt_v0([(P2PKH, 'nonwitness')]),
                psbt_v0([(P2WSH, 'witness')]), psbt_v0([(P2WPKH, 'none')]), psbt_v0([(P2WPKH, 'badprev')]),
                psbt_v0([(P2WPKH, 'redeem')]), psbt_v2([P2TR, P2SH]), 'notbase64!', base64.b64encode(b'xx').decode()):
        assert eb.psbt_problem(bad), bad
    assert eb.psbt_problem(psbt_v2([P2TR] * 201)) is None        # no count limit (draft 5)


def test_openchannel2_psbt_hooks(tmp_path):
    b = offline_backend(tmp_path)
    good, bad = psbt_v2([P2WPKH, P2TR]), psbt_v0([(P2WPKH, 'witness'), (P2PKH, 'nonwitness')])
    for hook in ('openchannel2_changed', 'openchannel2_sign'):
        assert b.openchannel2_psbt({'channel_id': 'c', 'psbt': good}, hook) == {'result': 'continue', 'psbt': good}
        assert b.openchannel2_psbt({'channel_id': 'c', 'psbt': bad}, hook) == {'result': 'reject'}
        assert b.openchannel2_psbt({'channel_id': 'c'}, hook) == {'result': 'reject'}
    rc = b.rpc_command
    assert refused(rc({'method': 'openchannel_signed', 'params': ['c', bad]}))
    assert refused(rc({'method': 'openchannel_update', 'params': ['c', bad]}))
    assert rc({'method': 'openchannel_update', 'params': {'channel_id': 'c', 'psbt': good}}) == {'result': 'continue'}
    assert rc({'method': 'openchannel_signed', 'params': ['c', good]}) == {'result': 'continue'}
    assert refused(rc({'method': 'openchannel_init', 'params': ['x', 1000, bad]}))
    b.apply_option_check({}, {})
    assert b.openchannel2_psbt({'channel_id': 'c', 'psbt': good}, 'openchannel2_sign') == {'result': 'reject'}


def test_openchannel2_psbt_refusal_aborts_first(tmp_path):
    """lightningd crashes on a 'reject' from openchannel2_changed/_sign while
    dualopend runs (upstream): a refusal aborts the open first, then answers
    'continue' (ignored once dualopend is gone); if the abort fails, a forced
    disconnect of the channel's peer."""
    bad = psbt_v0([(P2WSH, 'witness')])
    ln = FakeLn({'openchannel_abort': {'channel_canceled': True}})
    b = offline_backend(tmp_path, ln=ln)
    for hook in ('openchannel2_changed', 'openchannel2_sign'):
        assert b.openchannel2_psbt({'channel_id': 'c', 'psbt': bad}, hook) == {'result': 'continue', 'psbt': bad}
    assert ln.calls == [('openchannel_abort', {'channel_id': 'c'})] * 2

    def no_abort(params):
        raise RuntimeError('Another openchannel command is in progress')
    ln = FakeLn({'openchannel_abort': no_abort, 'disconnect': {},
                 'listpeerchannels': {'channels': [{'channel_id': 'c', 'peer_id': '02' * 33}]}})
    b = offline_backend(tmp_path, ln=ln)
    assert b.openchannel2_psbt({'channel_id': 'c', 'psbt': bad}, 'openchannel2_changed')['result'] == 'continue'
    assert ln.calls[-1] == ('disconnect', {'id': '02' * 33, 'force': True})
    ln.answers['listpeerchannels'] = {'channels': []}          # nothing to end: reject
    assert b.openchannel2_psbt({'channel_id': 'c', 'psbt': bad}, 'openchannel2_changed') == {'result': 'reject'}


def test_htlc_accepted_cap(tmp_path):
    def chans(n_out, n_in=0):
        return {'channels': [{'peer_id': '03' * 33, 'channel_id': 'aa' * 32, 'short_channel_id': '1x1x0',
                              'alias': {'local': '9x9x9'}, 'state': 'CHANNELD_NORMAL',
                              'htlcs': [{'direction': 'out'}] * n_out + [{'direction': 'in'}] * n_in}]}
    ln = FakeLn({'listpeerchannels': chans(29, 30)})
    b = offline_backend(tmp_path, ln=ln)
    assert b.htlc_accepted({'payload': '00'}, {}) == {'result': 'continue'}       # we are the destination
    assert not ln.calls
    for onion, fwd in (({'short_channel_id': '1x1x0'}, None), ({'short_channel_id': '9x9x9'}, None),
                       ({'next_node_id': '03' * 33}, None), ({}, 'aa' * 32)):
        ln.answers['listpeerchannels'] = chans(29, 30)
        b._approved.clear()
        assert b.htlc_accepted(onion, {}, fwd) == {'result': 'continue'}
        ln.answers['listpeerchannels'] = chans(30)
        assert b.htlc_accepted(onion, {}, fwd) == {'result': 'fail', 'failure_message': '10070000'}, onion
    assert b.htlc_accepted({'short_channel_id': '5x5x5'}, {}) == {'result': 'continue'}   # not ours to judge


# ------------------------------------------------------------ the option check (5.6)

def test_option_check():
    assert eb.check_options(GOOD_CONFIGS, GOOD_INFO, 2, 31337) == []
    regtest = {'watchtime-blocks': {'value_int': 6}, 'cltv-delta': {'value_int': 6},
               'cltv-final': {'value_int': 10}, 'funding-confirms': {'value_int': 1},
               'max-concurrent-htlcs': {'value_int': 483}}
    probs = eb.check_options(regtest, {'our_features': {'init': features(12, 23, 63)}}, 2, 31337)
    text = '\n'.join(probs)
    for want in ('watchtime-blocks', 'cltv-delta', 'cltv-final', 'max-concurrent-htlcs', '--developer',
                 'dev-bitcoind-poll', 'splicing'):
        assert want in text, want
    assert 'funding-confirms' not in text
    c = dict(GOOD_CONFIGS, **{'dev-bitcoind-poll': {'value_int': 2}})
    assert len(eb.check_options(c, GOOD_INFO, 2, 31337)) == 1            # anvil needs --dev-bitcoind-poll=1
    # Wall-clock time: on anvil at INTERVAL 1, 40 blocks are 40 s < 80 s.
    assert len(eb.check_options(GOOD_CONFIGS, GOOD_INFO, 1, 31337)) == 3
    assert eb.option_minimums(1, 31337)[0]['watchtime-blocks'] == 80
    # Base Sepolia (INTERVAL 60): 24 h and 2.4 h; CLN's default 30 s poll is fine.
    sep = {'watchtime-blocks': {'value_int': 1440}, 'cltv-delta': {'value_int': 144},
           'cltv-final': {'value_int': 144}, 'funding-confirms': {'value_int': 3},
           'max-concurrent-htlcs': {'value_int': 30}, 'developer': {'set': True}}
    assert eb.check_options(sep, GOOD_INFO, 60, 84532) == []
    assert eb.check_options(dict(sep, **{'funding-confirms': {'value_int': 2}}), GOOD_INFO, 60, 84532) != []
    # The anvil column only on chain id 31337: anvil's values fail on any other chain,
    # even at INTERVAL 2 (where a day is 43,200 blocks and the poll must be 1 s).
    probs = eb.check_options(GOOD_CONFIGS, GOOD_INFO, 2, 84532)
    assert len(probs) == 4 and '43200' in probs[0]
    assert eb.option_minimums(2, 1)[1] == 1 and eb.option_minimums(60, 1)[1] == 30


def test_option_check_refuses_opens_and_logs_broken(tmp_path):
    log = Log()
    ln = FakeLn({'listconfigs': {'configs': {}}, 'getinfo': {'our_features': {}}})
    b = offline_backend(tmp_path, ln=ln, log=log, checked=False)
    assert 'not checked' in b.open_refusal()
    b._option_check_loop()           # what the background thread runs
    assert b.options_checked and b.option_problems
    assert len(log.at('broken')) == len(b.option_problems)
    assert 'SPEC 5.6' in b.open_refusal()
    assert b.openchannel(open_payload())['result'] == 'reject'


# ------------------------------------------------------------ alarms (5.5)

class FakeIx:
    chain_id = 31337

    def __init__(self):
        self.lag_v, self.tag, self.wall = 1, (100, '0x00', 0), 0

    def current_height(self):
        return 100 + self.lag_v

    def tip(self):
        return 100

    def height_at(self, ts):
        return self.wall

    def tag_block(self):
        return self.tag


def test_alarms(tmp_path):
    log = Log()
    b = offline_backend(tmp_path, log=log)
    b.ix = FakeIx()
    now = 1000.0
    assert b.check_alarms(now) == {}
    assert b.margin() == 20                       # min(40, 41 // 2, 41 // 2)
    b.ix.lag_v = 6                                # 6 + 1 s / 2 s = 6.5 <= 20 / 3
    assert b.check_alarms(now) == {}
    b.ix.lag_v = 7
    assert set(b.check_alarms(now + 1)) == {'lag'}
    assert len(log.at('broken')) == 1
    b.check_alarms(now + 2)                       # not repeated at once
    assert len(log.at('broken')) == 1
    b.ix.tag = (100.5, '0x00', 0)                 # (the tag moves meanwhile)
    b.check_alarms(now + 700)                     # but every 10 minutes
    assert len(log.at('broken')) == 2
    b.ix.lag_v = 1
    b.check_alarms(now + 701)
    assert 'alarm cleared: lag' in log.at('info')[-1]
    # An outage: no sync, but the wall-clock height runs on (SPEC 3.1).
    b.ix.wall = 110
    assert 'lag' in b.check_alarms(now + 701.5)
    b.ix.wall = 0
    b.check_alarms(now + 701.6)
    # The tag not advancing for 10 x INTERVAL (anvil: 20 s), then advancing.
    t = now + 702
    b.check_alarms(t)
    assert 'tag' in b.check_alarms(t + 21)
    b.ix.tag = (101, '0x01', 2)
    assert 'tag' not in b.check_alarms(t + 22)
    # Balance below evm-min-balance.
    b.balance = 10**15
    assert 'balance' in b.check_alarms(t + 23)
    b.balance = 10**18
    # An index-sync error lasting more than 5 x INTERVAL.
    b._sync_failed('eth_getLogs: URLError', t + 24)
    assert 'sync' not in b.check_alarms(t + 30)
    assert 'sync' in b.check_alarms(t + 35)
    assert any('eth_getLogs: URLError' in m for m in log.at('broken'))


# ------------------------------------------------------------ options, keys and secrets (5.1, 5.2)

def options(**kw):
    o = {'evm-rpc': 'http://127.0.0.1:8545', 'evm-contract': '0x' + '22' * 20, 'evm-key-file': None,
         'evm-confirm': 'safe', 'evm-poll': '45', 'evm-min-balance': '0.01'}
    o.update(kw)
    return o.get


def test_config_and_key_file(tmp_path):
    log = Log()
    cfg = eb.Config.from_options(options(), str(tmp_path), {}, log)
    path = tmp_path / 'evm-key'
    assert cfg.key_file == str(path) and cfg.poll == 45 and cfg.min_balance_wei == 10**16 and cfg.confirm == 'safe'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert evm.unhex(path.read_text().strip()) == cfg.key
    assert cfg.key.hex() not in log.text()
    assert eb.Config.from_options(options(), str(tmp_path), {}, log).key == cfg.key     # reused
    os.chmod(path, 0o644)
    eb.Config.from_options(options(), str(tmp_path), {}, log)
    assert any('chmod 600' in m for m in log.at('broken'))
    (tmp_path / 'bad').write_text('not a key 0123')
    with pytest.raises(eb.ConfigError) as e:
        eb.Config.from_options(options(**{'evm-key-file': 'bad'}), str(tmp_path), {}, log)
    assert '0123' not in str(e.value)
    for bad in ({'evm-confirm': 'finalized'}, {'evm-poll': '0'}, {'evm-poll': 'x'}, {'evm-min-balance': '-1'},
                {'evm-contract': '0x12'}, {'evm-rpc': 'file:///etc/passwd'}, {'evm-rpc': None}):
        with pytest.raises(eb.ConfigError):
            eb.Config.from_options(options(**bad), str(tmp_path), {}, log)


SECRET = 'S3cr3tInfuraKey0123456789abcdef'


def test_infura_key_substitution(tmp_path):
    url = 'https://base-sepolia.infura.io/v3/${INFURA_API_KEY}'
    with pytest.raises(eb.ConfigError) as e:
        eb.Config.from_options(options(**{'evm-rpc': url}), str(tmp_path), {}, None)
    assert 'INFURA_API_KEY' in str(e.value)
    cfg = eb.Config.from_options(options(**{'evm-rpc': url}), str(tmp_path), {'INFURA_API_KEY': SECRET}, None)
    assert cfg.rpc_url == 'https://base-sepolia.infura.io/v3/' + SECRET
    assert SECRET in cfg.secrets and cfg.rpc_url in cfg.secrets


def test_secrets_never_logged_unreachable(tmp_path):
    log = Log()
    hang = HangingServer()
    port = hang.s.getsockname()[1]
    hang.close()                      # a port nothing listens on
    url = 'http://127.0.0.1:{}/v3/${{INFURA_API_KEY}}'.format(port)
    cfg = eb.Config.from_options(options(**{'evm-rpc': url}), str(tmp_path), {'INFURA_API_KEY': SECRET}, log)
    b = eb.Backend(cfg, log)
    with pytest.raises(evm.EvmError) as e:
        b.connect(retry_for=1.5)
    for text in (str(e.value), repr(e.value), log.text()):
        assert SECRET not in text and cfg.key.hex() not in text and str(port) + '/v3' not in text
    assert log.lines                  # it did log the retries


def test_secrets_never_logged_on_anvil(chain, tmp_path):
    log = Log()
    url = chain.rpc.url + '/?key=${INFURA_API_KEY}'
    cfg = eb.Config.from_options(options(**{'evm-rpc': url, 'evm-contract': chain.address, 'evm-poll': '1'}),
                                 str(tmp_path), {'INFURA_API_KEY': SECRET}, log)
    b = eb.Backend(cfg, log)
    chain.rpc.call('anvil_setBalance', b.address, evm.qty(10**20))
    chain.mine_at(1)
    b.connect(retry_for=0)
    b.apply_option_check(GOOD_CONFIGS, GOOD_INFO)
    t = threading.Thread(target=b._worker_loop, daemon=True)
    t.start()
    try:
        txid, _ = deposit(chain, b)
        txid2, _ = deposit(chain, b)
        results = [b.send_now(spend(b, txid, 0, 100_000, locktime=10**6, sequence=0).serialize().hex()),
                   b.send_now(spend(b, txid, 0, 100_000).serialize().hex()),
                   b.send_now('00')]
        chain.mine()
        b.poll_once()
        results.append(b.status())
        b.srpc = evm.EvmRpc(chain.rpc.url.replace(str(chain.rpc.url.rsplit(':', 1)[1]), '1') + '/' + SECRET,
                            timeout=1)
        b.rpc = b.srpc
        results.append(b.send_now(spend(b, txid2, 0, 100_000, fee=7).serialize().hex()))
        b.poll_once()
        text = log.text() + json.dumps(results, default=str)
        assert SECRET not in text and cfg.key.hex() not in text and cfg.key.hex()[2:] not in text
        assert results[1]['success'] and not results[0]['success']
    finally:
        b.stop()
        t.join(5)
        b.ix.close()


def test_scrub_is_applied_to_every_log_line(tmp_path):
    log = Log()
    b = eb.Backend(make_cfg(tmp_path, url='http://h/' + SECRET, secrets=[SECRET]), log)
    b.log('broken', 'leak http://h/{} {}'.format(SECRET, b.cfg.key.hex()))
    assert SECRET not in log.text() and b.cfg.key.hex() not in log.text() and '<redacted>' in log.text()


def test_pending_submit_mined_by_someone_else_is_cancelled(chain, backend):
    """SPEC 4.2: anyone may submit. If a third party's copy is mined first,
    the stuck submit is replaced by a 0-value self-send freeing the nonce."""
    txid, _ = deposit(chain, backend)
    tx = spend(backend, txid, 0, 100_000)
    chain.rpc.call('evm_setAutomine', False)
    try:
        assert backend.send_now(tx.serialize().hex())['success']
        p = list(backend.pending.values())[0]
        # The third party's copy, at a far higher fee, from another key.
        prev = backend.ix.prevouts(tx)
        rc_hash = chain.rpc.send_raw(chain.acct.sign_tx({
            'chainId': chain.chain_id, 'nonce': chain.rpc.nonce(chain.acct.address), 'maxPriorityFeePerGas': 10**10,
            'maxFeePerGas': 10**11, 'gas': 2_000_000, 'to': chain.address, 'value': 0,
            'data': chain.submit_data(tx.serialize(), prev)})[0])
        # Mine only the third party's tx: drop ours from the pool first.
        for h in p.hashes:
            chain.rpc.call('anvil_dropTransaction', h)
        chain.mine()
        assert int(chain.rpc.request('eth_getTransactionReceipt', rc_hash)['status'], 16) == 1
        backend.ix.sync()
        end = time.monotonic() + 4 * INTERVAL + 3
        while p.kind != 'cancel' and time.monotonic() < end:
            time.sleep(0.1)
        assert p.kind == 'cancel' and p.to == backend.address
        chain.mine()
    finally:
        chain.rpc.call('evm_setAutomine', True)
    end = time.monotonic() + 10
    while backend.pending and time.monotonic() < end:
        time.sleep(0.1)
    assert not backend.pending
    assert backend.send_now(tx.serialize().hex())['success']      # already in chain


def test_htlc_accepted_counts_forwards_not_yet_listed(tmp_path):
    listed = [{'direction': 'out', 'payment_hash': 'h%d' % i} for i in range(27)]
    ln = FakeLn({'listpeerchannels': lambda _: {'channels': [{
        'peer_id': '03' * 33, 'channel_id': 'aa' * 32, 'short_channel_id': '1x1x0', 'state': 'CHANNELD_NORMAL',
        'htlcs': list(listed)}]}})
    b = offline_backend(tmp_path, ln=ln)
    # Approved but not yet listed: counted once.
    got = [b.htlc_accepted({'short_channel_id': '1x1x0'}, {'payment_hash': 'x%d' % i})['result'] for i in range(4)]
    assert got == ['continue', 'continue', 'continue', 'fail']
    # Once listed, the same forwards are not counted again (no double count).
    b._approved.clear()
    listed[:] = [{'direction': 'out', 'payment_hash': 'h%d' % i} for i in range(27)]
    for i in range(3):
        assert b.htlc_accepted({'short_channel_id': '1x1x0'}, {'payment_hash': 'y%d' % i})['result'] == 'continue'
        listed.append({'direction': 'out', 'payment_hash': 'y%d' % i})
    assert b.htlc_accepted({'short_channel_id': '1x1x0'}, {'payment_hash': 'z'})['result'] == 'fail'   # 30 listed


# ------------------------------------------------------------ send path, offline (re-review fixes)

class StubIx:
    chain_id = 31337

    def __init__(self):
        self.known = set()

    def tx(self, txid_hex):
        return {'height': 1} if txid_hex in self.known else None

    def prevouts(self, tx):
        return [(100_000, A.p2wpkh, 1) for _ in tx.inputs]

    def add_receipt(self, receipt):
        return []

    def utxo(self, txid, vout):
        return None


class StubNode:
    """eth_* for the send worker; `refuse` and `lost_reply` script the
    answers to eth_sendRawTransaction."""

    def __init__(self):
        self.pending_nonce = self.latest_nonce = 0
        self.refuse = 0           # refuse this many sends (JSON-RPC error, tx unknown)
        self.lost_reply = 0       # accept but answer with a JSON-RPC error body (a proxy's 5xx)
        self.pool = {}
        self.sent = []            # hashes of the txs handed over, in order
        self.calls = []

    def eth_call(self, *a, **k):
        return b''

    def estimate_gas(self, *a, **k):
        return 100_000

    def fees(self):
        return 1, 10

    def nonce(self, address, tag='pending'):
        self.calls.append(('nonce', tag))
        return self.pending_nonce if tag == 'pending' else self.latest_nonce

    def call(self, method, *params):
        self.calls.append((method,))
        if method == 'eth_sendRawTransaction':
            h = '0x' + evm.keccak256(bytes.fromhex(params[0][2:])).hex()
            if self.refuse:
                self.refuse -= 1
                raise evm.EvmError('eth_sendRawTransaction: JSON-RPC error -32000', -32000)
            self.pool[h] = True
            self.sent.append(h)
            if self.lost_reply:
                self.lost_reply -= 1
                raise evm.EvmError('eth_sendRawTransaction: JSON-RPC error -32603', -32603)
            return h
        if method == 'eth_getTransactionByHash':
            return {'hash': params[0]} if params[0] in self.pool else None
        if method == 'eth_getTransactionReceipt':
            return None
        raise AssertionError(method)


def stub_backend(tmp_path):
    b = offline_backend(tmp_path)
    b.ix, b.srpc = StubIx(), StubNode()
    return b


def mk_tx():
    return btc.Tx(2, [btc.TxIn(os.urandom(32), 0)], [(1000, A.p2wpkh)], 0, [[b'\x01']])


def send_direct(b, tx):
    reply = eb.Reply(lambda r: None, time.monotonic() + b.time_limit())
    b._send_job(tx, reply, time.monotonic())
    return reply.result


def test_error_body_after_acceptance_is_success(tmp_path):
    """A JSON-RPC error from a proxy after the node took the tx: the node
    knows it, so it is accepted (step 4), tracked and not re-sent at a new nonce."""
    b = stub_backend(tmp_path)
    b.srpc.lost_reply = 1
    tx = mk_tx()
    assert send_direct(b, tx) == {'success': True, 'errmsg': ''}
    assert list(b.pending) == [0] and b._known(tx.txid_hex())


def test_refusal_releases_and_resyncs_the_nonce(tmp_path):
    """A refused send (the node doesn't know the tx) is failure (unsent), its
    nonce is released, and the next send re-reads eth_getTransactionCount
    (a stale read must not wedge the key); the tracker never lets the
    high-water mark fall below the chain's nonce."""
    b = stub_backend(tmp_path)
    b.srpc.refuse = 1
    r = send_direct(b, mk_tx())
    assert not r['success'] and 'refused' in r['errmsg'] and not b.pending
    assert b.nonce_hwm == 0 and not b._hwm_init
    b.srpc.pending_nonce = b.srpc.latest_nonce = 5      # the real nonce, read again
    assert send_direct(b, mk_tx())['success'] and list(b.pending) == [5]
    b.pending.clear()
    b.nonce_hwm, b._hwm_init, b.srpc.latest_nonce, b.srpc.pending_nonce = 2, True, 9, 9
    b._track(check_gaps=False)
    assert b.nonce_hwm == 9


def test_not_persisted_not_sent(tmp_path, monkeypatch):
    b = stub_backend(tmp_path)
    monkeypatch.setattr(b, '_save_pending', lambda: False)
    r = send_direct(b, mk_tx())
    assert r == {'success': False, 'errmsg': 'cannot persist the send: not sent'}
    assert ('eth_sendRawTransaction',) not in b.srpc.calls and not b.pending and b.nonce_hwm == 0


def test_refused_after_deadline_success_stays_tracked(tmp_path, monkeypatch):
    """The time limit answered success while the send was in flight; if the
    node then refuses it, it stays tracked and is re-sent at its nonce."""
    b = stub_backend(tmp_path)
    push = b._push

    def slow_refusal(raw):
        time.sleep(b.time_limit() + 0.2)
        b.srpc.refuse = 1
        return push(raw)
    monkeypatch.setattr(b, '_push', slow_refusal)
    got = []
    reply = b.sendrawtransaction(mk_tx().serialize().hex(), got.append)
    t = threading.Thread(target=b._worker_loop, daemon=True)
    t.start()
    try:
        reply.event.wait(5)
        assert got == [{'success': True, 'errmsg': ''}]
        end = time.monotonic() + 5
        while not any('after its time limit answered success' in m for m in b._log.at('broken')) \
                and time.monotonic() < end:
            time.sleep(0.05)
        assert list(b.pending) == [0] and b.pending[0].replace_at <= time.monotonic()
    finally:
        b.stop()
        t.join(5)


def test_only_the_head_nonce_is_bumped(tmp_path):
    """Step 7: a tx queued behind a lower pending nonce can't be mined;
    bumping it each INTERVAL would only compound its fees."""
    b = stub_backend(tmp_path)
    for n in (0, 1):
        b.pending[n] = eb.Pending('submit', 'aa' * 32, n, 100_000, b.contract, b'', 0, 1, 10, '0x%064x' % n)
        b.pending[n].replace_at = 0
    b._track(check_gaps=False)
    assert len(b.pending[0].hashes) == 2 and len(b.pending[1].hashes) == 1


def test_rebroadcast_during_the_push_is_not_answered_early(tmp_path, monkeypatch):
    """Step 5 answers success at once only for versions already handed to
    the node: during the push (persisted, not yet accepted) a rebroadcast
    goes to the queue, and a refused push leaves nothing claiming success."""
    b = stub_backend(tmp_path)
    tx = mk_tx()
    seen = []
    push = b._push

    def spy(raw):
        seen.append(b._known(tx.txid_hex()))
        b.srpc.refuse = 1
        return push(raw)
    monkeypatch.setattr(b, '_push', spy)
    r = send_direct(b, tx)
    assert seen == [False] and not r['success'] and not b._known(tx.txid_hex())
    monkeypatch.setattr(b, '_push', push)
    assert send_direct(b, tx)['success'] and b._known(tx.txid_hex())


def test_refusal_answered_as_failure_never_success(tmp_path, monkeypatch):
    """A refusal near the deadline: uncommit() wins the race against the
    timer, so the reply is the failure, not a success for an unsent tx."""
    b = stub_backend(tmp_path)
    b.srpc.refuse = 1
    saves = []
    real = b._save_pending

    def slow_save():
        saves.append(1)
        if len(saves) == 2:          # the clean-up save after the refusal
            time.sleep(b.time_limit())
        return real()
    monkeypatch.setattr(b, '_save_pending', slow_save)
    got = []
    reply = b.sendrawtransaction(mk_tx().serialize().hex(), got.append)
    job = b.jobs.get_nowait()
    job(time.monotonic())
    reply.event.wait(5)
    assert len(got) == 1 and not got[0]['success'] and not b.pending      # refused or 'not sent': never success


# --- reorgs taking back mined nonces (M4), offline

def mined(b, p, version=-1):
    """The tracker saw p's version `version` mined."""
    b._finish(p, {'status': '0x1', 'transactionHash': p.hashes[version], 'blockHash': '0x' + '11' * 32})


def reorged_back(b, nonce):
    """The chain's nonce reads below a nonce b saw mined."""
    b.srpc.latest_nonce = b.srpc.pending_nonce = nonce


def test_one_stale_nonce_read_requeues_nothing(tmp_path):
    """A single read of the chain's nonce below mined_floor (a lagging node
    behind a load balancer) is not a reorg: nothing is re-sent, not even
    after another one an INTERVAL later once a read in between was fine.
    Below it for an INTERVAL, it is."""
    b = stub_backend(tmp_path)
    tx = mk_tx()
    assert send_direct(b, tx)['success']
    mined(b, b.pending[0])
    b.srpc.latest_nonce = b.srpc.pending_nonce = 1
    assert b.mined_floor == 1 and list(b.recent) == [0] and not b.pending
    sent = len(b.srpc.sent)
    reorged_back(b, 0)
    b._track(check_gaps=False)
    assert not b.pending and list(b.recent) == [0] and b.mined_floor == 1
    reorged_back(b, 1)
    b._track(check_gaps=False)
    assert b._behind_since is None
    time.sleep(INTERVAL + 0.1)
    reorged_back(b, 0)
    b._track(check_gaps=False)
    assert not b.pending and len(b.srpc.sent) == sent and not b._log.at('unusual')
    time.sleep(INTERVAL + 0.1)
    b._track(check_gaps=False)
    assert list(b.pending) == [0] and len(b.srpc.sent) == sent + 1
    assert b.pending[0].hash_txid[b.srpc.sent[-1]] == tx.txid_hex()


def test_requeued_nonces_are_all_sent_at_once_in_order(tmp_path):
    """A reorg taking back three mined nonces re-sends all three at once,
    lowest first (one per INTERVAL would keep the later ones waiting)."""
    b = stub_backend(tmp_path)
    txs = [mk_tx() for _ in range(3)]
    for tx in txs:
        assert send_direct(b, tx)['success']
    for n in range(3):
        mined(b, b.pending[n])
    assert b.mined_floor == 3 and list(b.recent) == [0, 1, 2]
    sent = len(b.srpc.sent)
    reorged_back(b, 0)
    b._behind_since = time.monotonic() - INTERVAL      # below mined_floor for an INTERVAL
    b._track(check_gaps=False)
    assert sorted(b.pending) == [0, 1, 2] and b.mined_floor == 0
    again = b.srpc.sent[sent:]
    assert again == [b.pending[n].hashes[-1] for n in range(3)]
    assert [b.pending[n].hash_txid[h] for n, h in enumerate(again)] == [tx.txid_hex() for tx in txs]
    assert not any(p.requeued for p in b.pending.values())


def test_rebroadcast_of_a_reorged_send_keeps_its_nonce(tmp_path):
    """Between the reorg and the tracker's requeue, CLN rebroadcasts the
    same tx (the index no longer has it): it is re-sent at the nonce it was
    mined at, not at a new one, where the second copy would only revert."""
    b = stub_backend(tmp_path)
    tx = mk_tx()
    assert send_direct(b, tx)['success']
    mined(b, b.pending[0])
    other = mk_tx()                                    # an unrelated send, mined at nonce 1
    assert send_direct(b, other)['success']
    mined(b, b.pending[1])
    b.srpc.latest_nonce = b.srpc.pending_nonce = 2
    assert b.mined_floor == 2 and b.nonce_hwm == 2
    sent = len(b.srpc.sent)
    reorged_back(b, 0)
    assert send_direct(b, tx)['success']
    assert sorted(b.pending) == [0, 1] and b.nonce_hwm == 2 and b.mined_floor == 0
    assert b.srpc.sent[sent:] == [b.pending[0].hashes[-1], b.pending[1].hashes[-1]]
    assert b.pending[0].hash_txid[b.pending[0].hashes[-1]] == tx.txid_hex()
    # The tracker doesn't send it again, at this nonce or another.
    b._behind_since = time.monotonic() - INTERVAL
    b._track(check_gaps=False)
    assert sorted(b.pending) == [0, 1] and len(b.srpc.sent) == sent + 2


def test_requeue_resends_the_version_that_was_mined(tmp_path):
    """The mined version of a nonce may not be its last: here the submit
    was mined while a cancel had replaced it. A reorg re-sends the submit.
    A refused re-send leaves it requeued (and persisted so), and it is
    re-sent later."""
    b = stub_backend(tmp_path)
    tx = mk_tx()
    assert send_direct(b, tx)['success']
    p = b.pending[0]
    data = p.data
    b.ix.known.add(tx.txid_hex())          # a third party's copy is in the chain: cancel ours
    p.replace_at = 0
    b._track(check_gaps=False)
    assert p.kind == 'cancel' and len(p.hashes) == 2 and p.data == b''
    b.ix.known.clear()
    mined(b, p, version=0)                 # ... but our submit was mined first
    r = b.recent[0]
    assert (r.kind, r.to, r.data, r.btc_txid) == ('submit', b.contract, data, tx.txid_hex())
    saved = json.load(open(b.pending_file))
    assert [d['kind'] for d in saved['recent']] == ['submit'] and saved['mined_floor'] == 1
    reorged_back(b, 0)
    b.srpc.refuse = 1
    b._behind_since = time.monotonic() - INTERVAL
    b._track(check_gaps=False)
    p = b.pending[0]
    assert p.requeued and len(p.hashes) == 2 and (p.kind, p.data) == ('submit', data)
    assert json.load(open(b.pending_file))['pending'][0]['requeued'] is True
    again = eb.Backend(b.cfg, Log())
    again.interval = INTERVAL
    again._load_pending()
    assert again.pending[0].requeued and again.pending[0].data == data
    p.replace_at = 0
    b._track(check_gaps=False)
    assert not p.requeued and len(p.hashes) == 3 and p.kind == 'submit'
    assert p.hash_txid[p.hashes[-1]] == tx.txid_hex() and b.srpc.sent[-1] == p.hashes[-1]


def test_resigned_and_mined_version_survives(tmp_path):
    """A submit re-signed with a new tx (step 6) but whose first version was
    mined: that first version is what a reorg re-sends."""
    b = stub_backend(tmp_path)
    tx = mk_tx()
    assert send_direct(b, tx)['success']
    p = b.pending[0]
    first = p.data
    tx2 = btc.Tx(2, [btc.TxIn(tx.inputs[0].prev_txid, 0)], [(900, A.p2wpkh)], 0, [[b'\x02']])
    assert send_direct(b, tx2)['success'] and p.btc_txid == tx2.txid_hex() and p.data != first
    mined(b, p, version=0)
    assert (b.recent[0].btc_txid, b.recent[0].data) == (tx.txid_hex(), first)
    assert list(b.recent[0].bodies) == [tx.txid_hex()]


def test_sends_seen_mined_survive_a_restart(tmp_path):
    """evm-pending.json keeps the sends seen mined and mined_floor: after a
    restart a reorg still re-sends the tx at its nonce (without them the gap
    looked untracked and got a 0-value filler)."""
    b = stub_backend(tmp_path)
    tx = mk_tx()
    assert send_direct(b, tx)['success']
    mined(b, b.pending[0])
    b2 = stub_backend(tmp_path)
    b2._load_pending()
    assert list(b2.recent) == [0] and b2.mined_floor == 1 and b2.nonce_hwm == 1 and not b2.pending
    assert b2.recent[0].data == b.recent[0].data
    reorged_back(b2, 0)
    b2._track()                            # gaps checked too: none
    assert not b2.pending and not b2.srpc.sent
    b2._behind_since = time.monotonic() - INTERVAL
    b2._track()
    assert list(b2.pending) == [0] and b2.pending[0].kind == 'submit'
    assert b2.pending[0].hash_txid[b2.srpc.sent[-1]] == tx.txid_hex() and len(b2.srpc.sent) == 1
    assert not any('self-send' in m for m in b2._log.at('unusual'))
