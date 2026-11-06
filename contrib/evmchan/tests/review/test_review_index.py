# SPDX-License-Identifier: MIT
"""Adversarial review PoCs for evmindex / evm.py. Each test asserts the
property the spec wants; strict xfail marks the ones the current code breaks
(they will start failing, i.e. XPASS, once fixed). Fixed in M2's second
round (SPEC 5.2 draft 4): the marks are gone and every test must pass."""
import pytest

import btc
import evm
import evmindex
from fakechain import CONTRACT, FakeChain


def fresh(tmp_path, node, name='ix.sqlite3', tag='safe'):
    return evmindex.Index(str(tmp_path / name), node, CONTRACT, tag)


def txid(raw):
    return btc.Tx.parse(raw).txid_hex()


class LaggingLogs(FakeChain):
    """Tags and blocks come from an up-to-date backend; eth_getLogs from one
    whose head is `logs_lag` blocks behind and which clamps toBlock to its
    head (as reth does, and as providers with a log-index lag behave)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.logs_lag = 0

    def call(self, method, *params):
        if method == 'eth_getLogs' and self.logs_lag:
            f = dict(params[0])
            head = len(self.blocks) - 1 - self.logs_lag
            f['toBlock'] = evm.qty(min(int(f['toBlock'], 16), head))
            if int(f['fromBlock'], 16) > head:
                self.calls.append((method, params))
                return []
            return super().call(method, f)
        return super().call(method, *params)


class RateLimited(FakeChain):
    """eth_getLogs fails with Infura's rate-limit error after `budget` calls
    per sync (reset by the test)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.budget = None

    def call(self, method, *params):
        if method == 'eth_getLogs' and self.budget is not None:
            if self.budget == 0:
                raise evm.EvmError('eth_getLogs: project ID request rate exceeded', -32005)
            self.budget -= 1
        return super().call(method, *params)


def test_lagging_latest_must_not_change_a_served_block(tmp_path):
    """A load balancer answers `latest`/`safe` from a node behind the rows
    already indexed. Nothing reorganised, yet block 2 (<= tip) loses its tx."""
    node = FakeChain()
    ix = fresh(tmp_path, node)
    node.add_block(node.T(1))
    d = node.deposit()
    node.add_block(node.T(2), [d])
    for h in range(3, 7):
        node.add_block(node.T(h))
    ix.sync()
    assert ix.tip() == 4 and ix.utxo(txid(d), 0)
    before = ix.block(2)
    node.hide = 5                       # latest reported: the block at T(1)
    ix.sync()
    assert ix.tip() == 4
    assert ix.block(2) == before        # FAILS: block 2 rebuilt without d
    assert ix.utxo(txid(d), 0)


def test_clamped_getlogs_must_not_lose_a_confirmed_row(tmp_path):
    """eth_getLogs answered by a node 3 blocks behind: a deposit in a block
    below the tag is missed, the tip covers its height, and once the chain
    moves on by more than REORG_MARGIN it is never read again."""
    node = LaggingLogs()
    ix = fresh(tmp_path, node)
    node.add_block(node.T(1))
    d = node.deposit()
    node.add_block(node.T(2), [d])      # block 7
    node.add_block(node.T(3))           # block 8 (safe)
    node.add_block(node.T(4))           # block 9 (latest)
    node.logs_lag = 3                   # getLogs node's head: block 6
    ix.sync()
    # Before the fix the tip reached 2 here, serving height 2 without d. Now
    # the log hash at the tag disagrees, so the tip waits for a proof.
    assert ix.tip() == 0
    node.logs_lag = 0
    for i in range(70):
        node.add_block(node.T(5) + i)
    ix.sync()
    ix2 = fresh(tmp_path, node, 'healthy.sqlite3')   # built from a healthy node
    ix2.sync()
    assert ix2.tx(txid(d)) is not None
    assert ix.block(2) == ix2.block(2)  # FAILS: d is missing for good
    assert ix.utxo(txid(d), 0)


def test_catch_up_makes_progress_under_rate_limits(tmp_path):
    """8,000 blocks to catch up (4 eth_getLogs ranges) with a provider that
    allows 2 calls per poll: every poll restarts at DEPLOY_BLOCK, so the first
    sync never completes and ibd stays true."""
    node = RateLimited()
    ix = fresh(tmp_path, node)
    for i in range(8000):
        node.add_block(node.T(1) + i)
    starts = []
    for _ in range(6):
        node.budget = 2
        n = len(node.calls)
        try:
            ix.sync()
        except evm.EvmError:
            pass
        first = next(p for m, p in node.calls[n:] if m == 'eth_getLogs')
        starts.append(int(first[0]['fromBlock'], 16))
    assert starts[-1] > node.deploy_block, starts   # FAILS: [5, 5, 5, 5, 5, 5]
    assert not ix.ibd


def test_latest_tag_reorg_deeper_than_margin(tmp_path):
    """tag=latest; an unsafe-head reorg 100 blocks deep (OP Stack can reorg
    the unsafe chain back to the safe head) removes a deposit. The orphaned
    row stays in a served block forever."""
    node = FakeChain(interval=60)
    ix = fresh(tmp_path, node, tag='latest')
    node.add_block(node.T(1))
    d = node.deposit()
    n = node.add_block(node.T(1) + 2, [d])['number']
    for i in range(100):
        node.add_block(node.T(1) + 4 + 2 * i)
    ix.sync()
    assert ix.utxo(txid(d), 0)
    node.reorg(n)
    for i in range(130):
        node.add_block(node.T(1) + 2 + 2 * i)     # same times, no deposit
    ix.sync()
    assert ix.tip() >= 1
    assert ix.tx(txid(d)) is None                 # FAILS: orphaned row kept


def test_api_key_never_in_exceptions():
    rpc = evm.EvmRpc('base-sepolia.infura.io/v3/SECRETKEY123')     # scheme forgotten in --evm-rpc
    with pytest.raises(Exception) as e:
        rpc.call('eth_chainId')
    assert isinstance(e.value, evm.EvmError), type(e.value)
    assert 'SECRETKEY123' not in str(e.value)
