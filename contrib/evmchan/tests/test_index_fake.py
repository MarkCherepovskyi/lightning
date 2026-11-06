# SPDX-License-Identifier: MIT
"""evmindex against an in-memory node (fakechain.py): reorgs of any shape,
the tip, the 10k-result split, a bad event, the header cache, persistence and
concurrent readers. test_index_anvil.py does the same against the contract."""
import threading

import pytest

import btc
import evm
import evmindex
from fakechain import CONTRACT, FakeChain
from test_btc import ref_block


def all_blocks(ix):
    return [ix.block(h) for h in range(ix.tip() + 1)]


def check_chain(ix, node):
    """Every block <= tip, rebuilt independently from the node's logs."""
    by_height = {}
    for b in node.blocks:
        for log in b['logs']:
            h = int(log['topics'][1], 16)
            raw = evm.abi_decode(['bytes'], evm.unhex(log['data']))[0]
            by_height.setdefault(h, []).append(raw)
    prev = bytes(32)
    for h in range(ix.tip() + 1):
        raws = by_height.get(h, [])
        want, whash = ref_block(h, prev, raws, [btc.txid_of(r) for r in raws], node.G, node.I)
        got_hash, got = ix.block(h)
        assert got == want and got_hash == btc.display(whash), h
        prev = whash


def fresh(tmp_path, node, name='ix.sqlite3', tag='safe'):
    return evmindex.Index(str(tmp_path / name), node, CONTRACT, tag)


def test_sync_tip_and_blocks(tmp_path):
    node = FakeChain()
    ix = fresh(tmp_path, node)
    assert ix.ibd and ix.tip() == 0 and ix.lag() is None
    assert btc.parse_block(ix.block(0)[1])[0][4:36] == bytes(32)    # block 0, before any sync
    d1, d2, d3 = node.deposit(), node.deposit(), node.deposit()
    node.add_block(node.T(1), [d1, d2])
    node.add_block(node.T(1) + 1, [d3])
    node.add_block(node.T(4))
    node.add_block(node.T(4) + 1)
    r = ix.sync()
    assert not ix.ibd
    # safe = the block at T(4): candidate H(T(4)) - 1 = 3.
    assert ix.tip() == 3 and r['tip'] == 3
    assert ix.lag() == 4 - 3
    check_chain(ix, node)
    _, raw = ix.block(1)
    hdr, txs = btc.parse_block(raw)
    assert [t.serialize() for t in txs[1:]] == [d1, d2, d3]
    for h in (0, 2, 3):
        assert len(btc.parse_block(ix.block(h)[1])[1]) == 1     # coinbase only
    assert ix.block(4) is None and ix.block(-1) is None
    u = ix.utxo(btc.Tx.parse(d1).txid_hex(), 0)
    assert u == {'amount': 1000, 'script': btc.Tx.parse(d1).outputs[0][1], 'height': 1}
    assert ix.utxo(btc.Tx.parse(d1).txid_hex(), 1) is None
    assert ix.tx(btc.Tx.parse(d3).txid_hex())['height'] == 1


def test_tip_never_decreases(tmp_path):
    node = FakeChain()
    ix = fresh(tmp_path, node)
    for h in range(1, 8):
        node.add_block(node.T(h))
    ix.sync()
    assert ix.tip() == 5        # safe is at T(6)
    node.reorg(len(node.blocks) - 4)   # the chain gets shorter: safe goes back to T(2)
    node.add_block(node.T(3) + 1)
    r = ix.sync()
    assert r['tip'] == 5 and ix.tip() == 5
    assert ix.block(5) is not None
    check_chain(ix, node)


def test_latest_tag(tmp_path):
    node = FakeChain()
    ix = fresh(tmp_path, node, tag='latest')
    node.add_block(node.T(3))
    ix.sync()
    assert ix.tip() == 2
    with pytest.raises(evmindex.EvmIndexError):
        fresh(tmp_path, node, 'other.sqlite3', tag='finalized')


def test_reorg_drops_and_rebuilds(tmp_path):
    node = FakeChain()
    ix = fresh(tmp_path, node)
    keep, gone, moved = node.deposit(), node.deposit(), node.deposit()
    node.add_block(node.T(1), [keep])
    ix.sync()                                   # tag: the deploy block
    node.add_block(node.T(2), [gone])
    node.add_block(node.T(2) + 1, [moved])
    ix.sync()                                   # tag: block of `keep`
    assert all(ix.utxo(btc.Tx.parse(d).txid_hex(), 0) for d in (keep, gone, moved))
    fork = node.blocks[-2]['number']
    node.reorg(fork)                            # replace the last two blocks
    other = node.deposit()
    node.add_block(node.T(2), [other])          # same number, new hash, new tx
    node.add_block(node.T(3), [])
    node.add_block(node.T(3) + 1, [moved])      # the same tx in another block
    r = ix.sync()
    assert r['dropped'] >= 2
    assert ix.utxo(btc.Tx.parse(gone).txid_hex(), 0) is None
    assert ix.tx(btc.Tx.parse(gone).txid_hex()) is None
    assert ix.utxo(btc.Tx.parse(other).txid_hex(), 0)['height'] == 2
    assert ix.tx(btc.Tx.parse(moved).txid_hex())['evm_block'] == fork + 2
    node.add_block(node.T(6))
    node.add_block(node.T(6) + 1)
    ix.sync()
    check_chain(ix, node)


def test_reorg_to_empty_blocks_and_shorter_chain(tmp_path):
    node = FakeChain()
    ix = fresh(tmp_path, node)
    node.add_block(node.T(1))
    ix.sync()
    d = node.deposit()
    node.add_block(node.T(2), [d])
    ix.sync()
    txid = btc.Tx.parse(d).txid_hex()
    assert ix.utxo(txid, 0)
    # Shorter chain: `latest` below the previous poll's is a stale backend
    # (SPEC 5.2), so the poll is skipped and nothing changes...
    node.reorg(len(node.blocks) - 1)
    assert ix.sync()['skipped'] == 'stale'
    assert ix.utxo(txid, 0)
    # ...until the chain is back to that length; then the log hash at the tag
    # disagrees and a repair drops the row.
    node.add_block(node.T(2) + 1)
    node.add_block(node.T(3))
    r = ix.sync()
    assert r['proven'] and r['dropped'] == 1
    assert ix.utxo(txid, 0) is None
    check_chain(ix, node)


def test_receipt_replaces_only_a_confirmed_block(tmp_path):
    """SPEC 5.2: a receipt replaces an existing row only if its block hash is
    the node's block at that number."""
    node = FakeChain()
    ix = fresh(tmp_path, node)
    node.add_block(node.T(1))
    ix.sync()
    old, new = node.deposit(), node.deposit()
    b = node.add_block(node.T(2), [old])
    ix.add_receipt(node.receipt(b))
    # A fork replaces block b; the receipt of the fork's block is confirmed.
    node.reorg(b['number'])
    b2 = node.add_block(node.T(2), [new])
    assert ix.add_receipt(node.receipt(b2)) == [btc.Tx.parse(new).txid_hex()]
    assert ix.utxo(btc.Tx.parse(old).txid_hex(), 0) is None
    # The old receipt now is not the node's block: refused.
    assert ix.add_receipt(node.receipt(b)) == []
    assert ix.utxo(btc.Tx.parse(old).txid_hex(), 0) is None
    assert ix.utxo(btc.Tx.parse(new).txid_hex(), 0)
    node.add_block(node.T(3))
    node.add_block(node.T(3) + 1)
    assert ix.sync()['proven']
    check_chain(ix, node)


def test_shorter_chain_with_later_timestamps(tmp_path):
    """A fork with fewer blocks but later timestamps must not leave the old
    deposit in a block <= tip."""
    node = FakeChain()
    ix = fresh(tmp_path, node)
    node.add_block(node.T(1))
    node.add_block(node.T(1) + 1)
    d = node.deposit()
    n = node.add_block(node.T(2), [d])['number']
    ix.sync()
    assert ix.utxo(btc.Tx.parse(d).txid_hex(), 0)
    node.reorg(n - 2)
    node.add_block(node.T(4))
    node.add_block(node.T(5))
    assert ix.sync()['skipped'] == 'stale'          # latest n - 1 < n
    node.add_block(node.T(6))
    assert ix.sync()['proven']
    assert ix.tip() == 4
    assert ix.utxo(btc.Tx.parse(d).txid_hex(), 0) is None
    check_chain(ix, node)


def test_latest_tag_deep_reorg(tmp_path):
    """With tag `latest`, a reorg deeper than one block."""
    node = FakeChain()
    ix = fresh(tmp_path, node, tag='latest')
    node.add_block(node.T(1))
    d = node.deposit()
    n = node.add_block(node.T(2), [d])['number']
    node.add_block(node.T(3))
    ix.sync()
    assert ix.utxo(btc.Tx.parse(d).txid_hex(), 0)
    node.reorg(n)
    node.add_block(node.T(2))
    node.add_block(node.T(3))
    node.add_block(node.T(5))
    ix.sync()
    assert ix.tip() == 4
    assert ix.utxo(btc.Tx.parse(d).txid_hex(), 0) is None
    check_chain(ix, node)


def test_stale_receipt_is_refused_below_the_checkpoint(tmp_path):
    """A receipt from before a reorg, ingested after the sync that saw the
    reorg, is not the node's block: it changes nothing."""
    node = FakeChain()
    ix = fresh(tmp_path, node)
    node.add_block(node.T(1))
    d = node.deposit()
    b = node.add_block(node.T(2), [d])
    stale = node.receipt(b)
    node.reorg(b['number'])
    node.add_block(node.T(2))
    node.add_block(node.T(2) + 1, [d])
    for h in range(3, 80):
        node.add_block(node.T(h))
    ix.sync()
    blocks = all_blocks(ix)
    assert ix.tx(btc.Tx.parse(d).txid_hex())['evm_block'] == b['number'] + 1
    assert ix.add_receipt(stale) == []
    assert all_blocks(ix) == blocks
    ix.sync()
    assert ix.tx(btc.Tx.parse(d).txid_hex())['evm_block'] == b['number'] + 1
    check_chain(ix, node)


def test_add_receipt_before_sync(tmp_path):
    node = FakeChain()
    ix = fresh(tmp_path, node)
    d = node.deposit()
    b = node.add_block(node.T(1), [d])
    assert ix.utxo(btc.Tx.parse(d).txid_hex(), 0) is None
    assert ix.add_receipt(node.receipt(b)) == [btc.Tx.parse(d).txid_hex()]
    assert ix.utxo(btc.Tx.parse(d).txid_hex(), 0)['height'] == 1
    assert ix.add_receipt(node.receipt(b)) == [btc.Tx.parse(d).txid_hex()]   # idempotent
    assert ix.add_receipt(dict(node.receipt(b), status='0x0')) == []
    ix.sync()
    node.add_block(node.T(3))
    node.add_block(node.T(3) + 1)
    ix.sync()
    check_chain(ix, node)


def test_split_through_the_index(tmp_path):
    node = FakeChain(max_logs=50)
    ix = fresh(tmp_path, node)
    raws = []
    for i in range(40):
        batch = [node.deposit() for _ in range(10)]
        raws += batch
        node.add_block(node.T(1) + i, batch)
    node.add_block(node.T(25))
    node.add_block(node.T(25) + 1)
    ix.sync()
    assert all(ix.utxo(btc.Tx.parse(r).txid_hex(), 0) for r in raws)
    ranges = [(int(p[0]['fromBlock'], 16), int(p[0]['toBlock'], 16)) for m, p in node.calls if m == 'eth_getLogs']
    assert len(ranges) > 3
    check_chain(ix, node)


def test_undecodable_events_are_counted_not_served(tmp_path):
    """SPEC 5.2: an undecodable event counts in the log hash, is kept out of
    served blocks and of the UTXO set, is logged at BROKEN level, and never
    stops the index."""
    node = FakeChain()
    logged = []
    ix = evmindex.Index(str(tmp_path / 'u.sqlite3'), node, CONTRACT, log=lambda lv, m: logged.append((lv, m)))
    good, other = node.deposit(), node.deposit()
    junk = b'\x02\x00\x00\x00junk'
    node.add_block(node.T(1), [good, (other, btc.Tx.parse(good).txid()), (junk, bytes(range(32)))])
    node.add_block(node.T(2))
    node.add_block(node.T(2) + 1)
    r = ix.sync()
    assert r['proven'] and ix.tip() == 1 and not ix.ibd
    assert ix.undecodable == 2
    assert [lv for lv, _ in logged].count('broken') == 2
    assert all('junk' not in m for _, m in logged)
    txs = btc.parse_block(ix.block(1)[1])[1]
    assert [t.serialize() for t in txs[1:]] == [good]
    assert ix.utxo(btc.Tx.parse(other).txid_hex(), 0) is None
    assert ix.tx(btc.display(bytes(range(32)))) is None
    assert ix.checkpoint()[2] == 3               # all three counted


def test_header_cache_follows_changes(tmp_path):
    """A change at a height <= tip (here a log appended to an old block, as a
    node that missed it would show: the same block hash) rebuilds that block
    and every later prev_hash once proven, as a fresh index would."""
    node = FakeChain()
    ix = fresh(tmp_path, node)
    for h in range(1, 30):
        node.add_block(node.T(h), [node.deposit()] if h % 3 == 0 else [])
    ix.sync()
    before = all_blocks(ix)
    assert len(before) == 28
    d = node.deposit()
    b = node.blocks[node.deploy_block + 10]       # height 10
    b['logs'].append(node.make_log(b, len(b['logs']), 10, btc.txid_of(d), d))
    assert ix.add_receipt(node.receipt(b))        # confirmed: the node has that block hash
    node.add_block(node.T(30))
    assert ix.sync()['proven']
    after = all_blocks(ix)
    assert after[:10] == before[:10] and all(a != c for a, c in zip(after[10:], before[10:]))
    check_chain(ix, node)
    ix2 = fresh(tmp_path, node, 'fresh.sqlite3')
    ix2.sync()
    assert all_blocks(ix2) == after[:len(all_blocks(ix2))]


def test_persistence(tmp_path):
    node = FakeChain()
    path = str(tmp_path / 'p.sqlite3')
    ix = evmindex.Index(path, node, CONTRACT)
    d = node.deposit()
    node.add_block(node.T(1), [d])
    for h in range(2, 8):
        node.add_block(node.T(h))
    ix.sync()
    blocks, tip = all_blocks(ix), ix.tip()
    ix.close()
    n_calls = node.count('eth_getLogs')
    ix = evmindex.Index(path, node, CONTRACT)
    assert ix.ibd and ix.tip() == tip and all_blocks(ix) == blocks
    assert ix.utxo(btc.Tx.parse(d).txid_hex(), 0)['height'] == 1
    assert node.count('eth_getLogs') == n_calls
    ix.sync()
    assert not ix.ibd and all_blocks(ix)[:len(blocks)] == blocks
    ix.close()
    other = FakeChain(interval=3)
    with pytest.raises(evmindex.EvmIndexError):
        evmindex.Index(path, other, CONTRACT)
    with pytest.raises(evmindex.EvmIndexError):
        evmindex.Index(path, node, '0x' + '11' * 20)       # no code there


def test_readers_during_sync(tmp_path):
    node = FakeChain(max_logs=30)
    ix = fresh(tmp_path, node)
    raws = []
    for h in range(1, 300):
        batch = [node.deposit() for _ in range(h % 4)]
        raws += batch
        node.add_block(node.T(h), batch)
    errors, done = [], threading.Event()

    def reader():
        try:
            while not done.is_set():
                t = ix.tip()
                ix.block(t)
                ix.utxo(btc.Tx.parse(raws[0]).txid_hex(), 0)
                ix.lag()
        except Exception as e:      # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        for _ in range(3):
            ix.sync()
            node.add_block(node.blocks[-1]['timestamp'] + 2, [node.deposit()])
    finally:
        done.set()
        for t in threads:
            t.join()
    assert not errors
    check_chain(ix, node)
