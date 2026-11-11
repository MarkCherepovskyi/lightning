# SPDX-License-Identifier: MIT
"""Index snapshots (SPEC 5.2 "Snapshots") against the in-memory node
(fakechain.py): a good snapshot catches a fresh index up with only the logs
after its checkpoint; a tampered, incomplete, reordered, forked, foreign or
malformed one is refused, leaves the index empty, and a full catch-up from
DEPLOY_BLOCK follows and ends correct."""
import gzip
import json
import os
import sqlite3

import pytest

import btc
import evm
import evmbackend as eb
import evmindex
from conftest import Key, sign
from fakechain import CONTRACT, FakeChain
from test_backend import Log, offline_backend
from test_index_fake import check_chain, fresh

K = Key(0x5A5A)
RANGE = 10          # the node's eth_getLogs block-range cap, so ranges are counted


def history(node, blocks=80):
    """`blocks` EVM blocks after DEPLOY_BLOCK, one per height: a deposit to
    K's P2WPKH every 4th, two of them in one block, and P2WPKH spends of
    some (witness txs). Returns the raw txs in order."""
    out, deps = [], []
    for i in range(1, blocks + 1):
        txs = []
        if i % 4 == 0:
            for _ in range(1 + (i % 16 == 0)):
                raw = node.deposit(10_000, K.p2wpkh)
                deps.append((btc.txid_of(raw), i))
                txs.append(raw)
        if i % 10 == 0 and deps:
            dep, h = deps.pop(0)
            tx = btc.Tx(2, [btc.TxIn(dep, 0)], [(9_000, K.p2tr)], 0)
            txs.append(sign(tx, [K], [(10_000, K.p2wpkh, h)]).serialize())
        node.add_block(node.T(i), txs)
        out += txs
    return out


def chain(blocks=80):
    node = FakeChain()
    node.logs_range_cap = RANGE
    history(node, blocks)
    return node


def made(tmp_path, node, name='snap.jsonl.gz'):
    """A synced index's snapshot: (path, the export's summary)."""
    ix = fresh(tmp_path, node, 'maker.sqlite3')
    ix.sync()
    path = str(tmp_path / name)
    r = ix.export_snapshot(path)
    ix.close()
    return path, r


def read_lines(path):
    with gzip.open(path, 'rb') as f:
        return f.read().split(b'\n')[:-1]


def write_lines(path, lines, gz=True):
    data = b''.join(x + b'\n' for x in lines)
    with (gzip.open(path, 'wb') if gz else open(path, 'wb')) as f:
        f.write(data)


def edit(path, header=None, rows=None, out=None):
    """Rewrites a snapshot: header(dict) and rows(list of row lists) edit in place."""
    lines = read_lines(path)
    h, rs = json.loads(lines[0]), [json.loads(x) for x in lines[1:]]
    if header:
        header(h)
    if rows:
        rows(rs)
    out = out or path + '.edited.gz'
    write_lines(out, [json.dumps(h).encode()] + [json.dumps(r).encode() for r in rs])
    return out


def logs_calls(node):
    return node.count('eth_getLogs')


def full_catchup(tmp_path, node):
    """eth_getLogs calls of a fresh index's first sync, without a snapshot."""
    ix = fresh(tmp_path, node, 'full.sqlite3')
    n = logs_calls(node)
    ix.sync()
    calls = logs_calls(node) - n
    check_chain(ix, node)
    tip = ix.tip()
    ix.close()
    return calls, tip


def refused(tmp_path, node, path, match, name='b.sqlite3'):
    """The snapshot is refused, the index stays empty, and a full catch-up
    follows and ends correct."""
    ix = fresh(tmp_path, node, name)
    with pytest.raises(evmindex.SnapshotRefused, match=match):
        ix.import_snapshot(path)
    assert ix.is_empty() and ix.tip() == 0 and ix.checkpoint() is None
    for table in ('txs', 'outputs', 'spends', 'staged', 'headers'):
        assert ix._db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0, table
    ix.sync()
    assert not ix.ibd
    check_chain(ix, node)
    return ix


# ------------------------------------------------------------ the good case

def test_good_snapshot_reads_only_the_logs_after_its_checkpoint(tmp_path):
    node = chain(400)
    path, r = made(tmp_path, node)
    assert r['rows'] == r['checkpoint']['count'] > 100 and r['contract'] == evm.checksum_address(CONTRACT)
    cp_block = r['checkpoint']['evm_block']
    # The chain goes on: 30 more blocks, with txs, after the snapshot.
    more = []
    for i in range(401, 431):
        txs = [node.deposit(5_000)] if i % 3 == 0 else []
        node.add_block(node.T(i), txs)
        more += txs
    full, full_tip = full_catchup(tmp_path, node)

    ix = fresh(tmp_path, node, 'b.sqlite3')
    n_logs, n_calls = logs_calls(node), len(node.calls)
    got = ix.import_snapshot(path)
    assert got == {'rows': r['rows'], 'checkpoint': cp_block, 'count': r['rows']}
    assert logs_calls(node) == n_logs, 'the import reads no logs'
    # It asked the node for: the tag, the checkpoint block, txCount and logHash
    # there, and the checkpoint block again at the end.
    assert [m for m, _ in node.calls[n_calls:]] == ['eth_getBlockByNumber'] * 2 + ['eth_call'] * 2 + \
        ['eth_getBlockByNumber']
    assert all(p[1] == {'blockHash': r['checkpoint']['hash'], 'requireCanonical': True}
               for m, p in node.calls[n_calls:] if m == 'eth_call')
    assert ix.ibd and not ix.is_empty() and ix.checkpoint()[0] == cp_block
    assert ix.tip() == ix.height_at(node.blocks[cp_block]['timestamp']) - 1
    ix.sync()
    snap = logs_calls(node) - n_logs
    assert not ix.ibd and ix.tip() == full_tip
    check_chain(ix, node)
    # Outputs and spends were derived here: a spent deposit is spent, the rest unspent.
    spends = [btc.Tx.parse(raw) for raw in more]
    assert all(ix.utxo(t.txid_hex(), 0) for t in spends)
    first_spend = [t for t in (btc.Tx.parse(x) for b in node.blocks for x in
                               (evm.abi_decode(['bytes'], evm.unhex(lg['data']))[0] for lg in b['logs']))
                   if t.has_witness()][0]
    assert ix.utxo(btc.display(first_spend.inputs[0].prev_txid), 0) is None
    assert ix.utxo(first_spend.txid_hex(), 0)['amount'] == 9_000
    # 430 blocks at 10 a range: about 43 calls; from the checkpoint, about 3.
    print('eth_getLogs: full catch-up {}, from the snapshot {}'.format(full, snap))
    assert full >= 40 and snap <= 4


def test_cli_export_and_empty_contract(tmp_path, capsys):
    node = chain(40)
    ix = fresh(tmp_path, node, 'a.sqlite3')
    with pytest.raises(evmindex.EvmIndexError, match='no checkpoint'):
        ix.export_snapshot(str(tmp_path / 'none.gz'))
    ix.sync()
    ix.close()
    out = str(tmp_path / 'cli.jsonl.gz')
    assert evmindex.main([str(tmp_path / 'a.sqlite3'), out]) == 0
    assert json.loads(capsys.readouterr().out)['path'] == out
    assert evmindex.main([str(tmp_path / 'missing.sqlite3'), out]) == 1
    b = fresh(tmp_path, node, 'b.sqlite3')
    b.import_snapshot(out)
    b.sync()
    check_chain(b, node)
    # A contract with no txs yet: a snapshot of 0 rows works too.
    empty = FakeChain()
    for i in range(1, 6):
        empty.add_block(empty.T(i))
    e = fresh(tmp_path, empty, 'e.sqlite3')
    e.sync()
    r = e.export_snapshot(str(tmp_path / 'empty.gz'))
    assert r['rows'] == 0
    f = fresh(tmp_path, empty, 'f.sqlite3')
    assert f.import_snapshot(str(tmp_path / 'empty.gz'))['rows'] == 0
    f.sync()
    check_chain(f, empty)


def test_plain_json_lines_are_read_too(tmp_path):
    node = chain(40)
    path, _ = made(tmp_path, node)
    plain = str(tmp_path / 'plain.jsonl')
    write_lines(plain, read_lines(path), gz=False)
    ix = fresh(tmp_path, node, 'b.sqlite3')
    ix.import_snapshot(plain)
    ix.sync()
    check_chain(ix, node)


# ------------------------------------------------------------ refusals

def row_index(rows, pred):
    return [i for i, r in enumerate(rows) if pred(btc.Tx.parse(bytes.fromhex(r[5][2:])))][0]


def test_tampered_raw_tx_is_refused(tmp_path):
    node = chain()
    path, _ = made(tmp_path, node)

    def amount(rows):        # a deposit's amount: its txid no longer matches
        i = row_index(rows, lambda t: not t.has_witness())
        tx = btc.Tx.parse(bytes.fromhex(rows[i][5][2:]))
        tx.outputs[0] = (tx.outputs[0][0] + 1, tx.outputs[0][1])
        rows[i][5] = '0x' + tx.serialize().hex()
    refused(tmp_path, node, edit(path, rows=amount), 'raw tx is not the tx')


def test_tampered_witness_is_refused(tmp_path):
    """No txid commits to a witness: each one is checked against SPEC 4.3."""
    node = chain()
    path, _ = made(tmp_path, node)

    def sig(rows):
        i = row_index(rows, lambda t: t.has_witness())
        tx = btc.Tx.parse(bytes.fromhex(rows[i][5][2:]))
        s = bytearray(tx.witnesses[0][0])
        s[10] ^= 1
        tx.witnesses[0][0] = bytes(s)
        rows[i][5] = '0x' + tx.serialize().hex()

    def strip(rows):         # the witness dropped: same txid, but not valid
        i = row_index(rows, lambda t: t.has_witness())
        rows[i][5] = '0x' + btc.Tx.parse(bytes.fromhex(rows[i][5][2:])).serialize(witness=False).hex()

    def to_deposit(rows):    # a witness added to a deposit
        i = row_index(rows, lambda t: not t.has_witness())
        tx = btc.Tx.parse(bytes.fromhex(rows[i][5][2:]))
        tx.witnesses[0] = [b'\x01']
        rows[i][5] = '0x' + tx.serialize().hex()
    refused(tmp_path, node, edit(path, rows=sig), 'is not valid at height', 'b1.sqlite3')
    refused(tmp_path, node, edit(path, rows=strip), 'is not valid at height', 'b2.sqlite3')
    refused(tmp_path, node, edit(path, rows=to_deposit), 'deposit .* has a witness', 'b3.sqlite3')


def test_missing_extra_and_reordered_rows_are_refused(tmp_path):
    node = chain()
    path, _ = made(tmp_path, node)

    def drop(rows):
        del rows[len(rows) // 2]

    def swap_order(rows):
        rows[3], rows[4] = rows[4], rows[3]

    def swap_txs(rows):      # the EVM positions kept, the txs moved
        for k in (3, 4, 5):
            rows[3][k], rows[4][k] = rows[4][k], rows[3][k]

    def extra(rows):
        rows.append(rows[-1])

    def drop_and_count(h):   # the header says one row fewer: not the contract's count
        h['checkpoint']['count'] -= 1
    refused(tmp_path, node, edit(path, rows=drop), 'ends after', 'b1.sqlite3')
    refused(tmp_path, node, edit(path, rows=swap_order), 'not in EVM order', 'b2.sqlite3')
    refused(tmp_path, node, edit(path, rows=swap_txs), 'do not hash to its checkpoint', 'b3.sqlite3')
    refused(tmp_path, node, edit(path, rows=extra), 'more than its', 'b4.sqlite3')
    refused(tmp_path, node, edit(path, header=drop_and_count, rows=drop), 'not the contract', 'b5.sqlite3')


def test_wrong_or_forked_checkpoint_is_refused(tmp_path):
    node = chain()
    path, r = made(tmp_path, node)

    def bad_hash(h):
        h['checkpoint']['hash'] = '0x' + os.urandom(32).hex()

    def bad_loghash(h):
        h['checkpoint']['loghash'] = '0x' + os.urandom(32).hex()
    refused(tmp_path, node, edit(path, header=bad_hash), 'not the chain\'s block', 'b1.sqlite3')
    refused(tmp_path, node, edit(path, header=bad_loghash), 'not the contract\'s', 'b2.sqlite3')

    # Above the tag: a checkpoint at `latest`, right in every other way.
    top = node.blocks[-1]
    count, loghash = node.state_at({'blockHash': top['hash']})

    def at_latest(h):
        h['checkpoint'] = {'number': top['number'], 'hash': top['hash'], 'count': count,
                           'loghash': '0x' + loghash.hex()}

    def all_rows(rows):
        rows[:] = [[b['number'], b['hash'], int(lg['logIndex'], 16), int(lg['topics'][1], 16), lg['topics'][2],
                    '0x' + evm.abi_decode(['bytes'], evm.unhex(lg['data']))[0].hex()]
                   for b in node.blocks for lg in b['logs']]
    refused(tmp_path, node, edit(path, header=at_latest, rows=all_rows), 'above the safe block', 'b3.sqlite3')

    # A reorg below the checkpoint: its block is no longer the chain's.
    cp = r['checkpoint']['evm_block']
    fork = cp - 6
    kept = [b['logs'] for b in node.blocks[fork:]]
    ts = [b['timestamp'] for b in node.blocks[fork:]]
    node.reorg(fork)
    for t, logs in zip(ts, kept):
        node.add_block(t, [(evm.abi_decode(['bytes'], evm.unhex(lg['data']))[0], evm.unhex(lg['topics'][2]))
                           for lg in logs])
    refused(tmp_path, node, path, 'not the chain\'s block', 'b4.sqlite3')


def test_other_contract_or_chain_is_refused(tmp_path):
    node = chain(40)
    path, _ = made(tmp_path, node)

    def contract(h):
        h['contract'] = '0x' + '12' * 20

    def chain_id(h):
        h['chain_id'] = 84532

    def deploy(h):
        h['deploy_block'] += 1
    refused(tmp_path, node, edit(path, header=contract), 'for contract', 'b1.sqlite3')
    refused(tmp_path, node, edit(path, header=chain_id), 'for chain id 84532, not 31337', 'b2.sqlite3')
    refused(tmp_path, node, edit(path, header=deploy), 'DEPLOY_BLOCK', 'b3.sqlite3')


def test_malformed_files_are_refused(tmp_path):
    node = chain(40)
    path, _ = made(tmp_path, node)
    lines = read_lines(path)
    cases = {
        'missing': (None, 'cannot open'),
        'garbage': (b'\x1f\x8bnot gzip at all', 'cannot read'),
        'not json': (b'{"format": \n', 'header is not JSON'),
        'deep json': (b'[' * 3000 + b'\n', 'not JSON|longer than'),
        'not ours': (b'{"format": "other"}\n', 'not an evmchan'),
        'version': (lines[0].replace(b'"version":1', b'"version":2') + b'\n', 'version'),
        'float count': (lines[0].replace(b'"count":', b'"count":1.5,"x":') + b'\n', 'bad checkpoint count'),
        'bool block': (lines[0].replace(b'"number":', b'"number":true,"x":') + b'\n', 'bad checkpoint block'),
        'row shape': (lines[0] + b'\n[1,2]\n', 'row 0 is not'),
        'row hex': (lines[0] + b'\n' + lines[1].replace(b'"0x', b'"0xzz', 1) + b'\n', 'bad row 0'),
    }
    for name, (data, match) in cases.items():
        p = str(tmp_path / name.replace(' ', '_'))
        if data is not None:
            with open(p, 'wb') as f:
                f.write(data)
        refused(tmp_path, node, p, match, name.replace(' ', '_') + '.sqlite3')
    # A truncated gzip stream.
    with open(path, 'rb') as f:
        data = f.read()
    p = str(tmp_path / 'truncated.gz')
    with open(p, 'wb') as f:
        f.write(data[:len(data) * 2 // 3])
    refused(tmp_path, node, p, 'cannot read|ends after|not JSON', 'truncated.sqlite3')


def test_gzip_bomb_and_huge_claims(tmp_path):
    """Reading is bounded: a line is at most SNAPSHOT_LINE_MAX bytes, and no
    more rows are read than the contract's own txCount."""
    node = chain(40)
    path, _ = made(tmp_path, node)
    bomb = str(tmp_path / 'bomb.gz')
    with gzip.open(bomb, 'wb') as f:
        chunk = b'0' * (1 << 20)
        for _ in range(64):            # 64 MiB of one line, in a ~64 KiB file
            f.write(chunk)
    assert os.path.getsize(bomb) < 1 << 20
    refused(tmp_path, node, bomb, 'longer than', 'b1.sqlite3')
    lines = read_lines(path)
    h = json.loads(lines[0])
    row = lines[1]
    big = str(tmp_path / 'big.gz')
    with gzip.open(big, 'wb') as f:
        f.write(lines[0] + b'\n' + b'\n'.join(lines[1:]) + b'\n')
        for _ in range(100_000):       # rows past the count: one is read and refused
            f.write(row + b'\n')
    refused(tmp_path, node, big, 'more than its', 'b2.sqlite3')

    def huge(hd):
        hd['checkpoint']['count'] = 10**12
    refused(tmp_path, node, edit(path, header=huge), 'not the contract', 'b3.sqlite3')
    long_row = json.dumps([h['checkpoint']['number'], h['checkpoint']['hash'], 0, 1, '0x' + '00' * 32,
                           '0x' + '00' * (evmindex.MAX_RAW_TX + 1)]).encode()
    p = str(tmp_path / 'long.gz')
    write_lines(p, [lines[0], long_row])
    refused(tmp_path, node, p, 'bad row 0 raw tx|longer than', 'b4.sqlite3')


# ------------------------------------------------------------ the index's state

def test_only_into_an_empty_index_and_interrupted_imports_are_undone(tmp_path):
    node = chain(40)
    path, _ = made(tmp_path, node)
    ix = fresh(tmp_path, node, 'b.sqlite3')
    ix.sync()
    with pytest.raises(evmindex.SnapshotRefused, match='not empty'):
        ix.import_snapshot(path)
    assert ix.checkpoint() is not None
    ix.close()
    # A crash mid-import: rows in, no checkpoint, the flag still set.
    ix = fresh(tmp_path, node, 'c.sqlite3')
    real = ix._ingest_snapshot

    def crash(f, cp):
        real(f, cp)
        raise KeyboardInterrupt
    ix._ingest_snapshot = crash
    with pytest.raises(KeyboardInterrupt):
        ix.import_snapshot(path)
    assert ix.is_empty()     # undone on the way out
    db = sqlite3.connect(str(tmp_path / 'c.sqlite3'), isolation_level=None)
    db.execute("INSERT INTO meta (key, value) VALUES ('snapshot_import', 1)")
    db.execute("INSERT INTO txs (evm_block, evm_hash, log_index, height, txid, raw) VALUES (7, x'00', 0, 1, x'00', x'00')")
    db.close()
    ix.close()
    log = Log()
    ix = evmindex.Index(str(tmp_path / 'c.sqlite3'), node, CONTRACT, log=log)
    assert ix.is_empty() and 'interrupted snapshot import was undone' in log.text()
    ix.import_snapshot(path)
    ix.sync()
    check_chain(ix, node)


def test_backend_imports_or_falls_back(tmp_path):
    """The plugin's side: --evm-snapshot with an empty index imports it
    before the first poll; a refused one is logged at UNUSUAL and the poll
    catches up from DEPLOY_BLOCK; a non-empty index ignores the option."""
    node = chain(40)
    path, _ = made(tmp_path, node)

    def backend(name, snap):
        d = tmp_path / name
        d.mkdir()
        b = offline_backend(d)
        b.cfg.snapshot = snap
        b.rpc = node
        b.ix = fresh(d, node)
        return b
    b = backend('good', path)
    assert b.import_snapshot()
    assert any('imported' in m for m in b._log.at('info'))
    b.poll_once()
    check_chain(b.ix, node)
    assert not b.import_snapshot() and any('not empty' in m for m in b._log.at('info'))

    def tamper(h):
        h['checkpoint']['hash'] = '0x' + os.urandom(32).hex()
    b = backend('bad', edit(path, header=tamper))
    assert not b.import_snapshot()
    assert any('refused' in m and 'catching up from DEPLOY_BLOCK' in m for m in b._log.at('unusual'))
    b.poll_once()
    assert not b.ix.ibd
    check_chain(b.ix, node)

    # EVM node errors: retried, then a full catch-up.
    b = backend('down', path)
    calls = []

    def down(*a):
        calls.append(a)
        raise evm.EvmError('eth_getBlockByNumber: URLError')
    b.ix.rpc = type('Down', (), {'block': staticmethod(down)})()
    b.stop_event.wait = lambda t: False
    assert not b.import_snapshot() and len(calls) == eb.SNAPSHOT_TRIES
    assert any('not imported: catching up' in m for m in b._log.at('unusual'))
    assert b.ix.is_empty()

    # export through the plugin's RPC, relative to the lightning dir.
    r = backend('export', None)
    r.ix.sync()
    out = r.export_snapshot('mine.jsonl.gz')
    assert out['path'] == os.path.join(r.cfg.lightning_dir, 'mine.jsonl.gz') and os.path.exists(out['path'])
    assert r.export_snapshot()['path'] == os.path.join(r.cfg.lightning_dir, evmindex.SNAPSHOT_FILE)
    with pytest.raises(eb.UserError, match='string'):
        r.export_snapshot(5)
    for name in ('hsm_secret', 'evm-key', 'evmchan.sqlite3', 'x.gz'):    # it replaces the file
        with pytest.raises(eb.UserError, match='must end in .jsonl.gz'):
            r.export_snapshot(name)


def test_failed_wipe_blocks_polls_until_removed(tmp_path):
    """A refused import whose rows could not be removed (disk full) leaves
    its flag set: the plugin removes them before it ever syncs."""
    node = chain(40)
    path, _ = made(tmp_path, node)
    b = offline_backend(tmp_path)
    b.rpc, b.ix = node, fresh(tmp_path, node, 'b.sqlite3')

    real_wipe, real_ingest = b.ix._wipe, b.ix._ingest_snapshot

    def no_wipe():
        raise sqlite3.OperationalError('disk I/O error')

    def ingest_then_fail(f, cp):
        real_ingest(f, cp)
        raise SnapshotRefusedLate()
    b.ix._wipe, b.ix._ingest_snapshot = no_wipe, ingest_then_fail
    with pytest.raises(sqlite3.OperationalError):
        b.ix.import_snapshot(path)
    assert b.ix.importing() and b.ix._db.execute('SELECT COUNT(*) FROM txs').fetchone()[0] > 0
    b.ix._wipe = real_wipe
    b.poll_once()
    assert not b.ix.importing() and any('failed snapshot import were removed' in m for m in b._log.at('unusual'))
    assert not b.ix.ibd
    check_chain(b.ix, node)


class SnapshotRefusedLate(Exception):
    pass


def test_nothing_is_stored_before_the_rows_are_proven(tmp_path):
    """Pass 1 only hashes: a file whose rows don't hash to the proven
    checkpoint writes no row, however big."""
    node = chain(40)
    path, _ = made(tmp_path, node)

    def swap_txs(rows):
        for k in (3, 4, 5):
            rows[3][k], rows[4][k] = rows[4][k], rows[3][k]
    ix = fresh(tmp_path, node, 'b.sqlite3')
    ix._ingest = lambda *a: pytest.fail('rows stored before the proof')
    with pytest.raises(evmindex.SnapshotRefused, match='do not hash'):
        ix.import_snapshot(edit(path, rows=swap_txs))


def test_lagging_node_after_an_import_is_a_stale_backend(tmp_path):
    """The import records the tag it was proven at: a poll whose tag is
    below it (a lagging node behind a load balancer) is skipped."""
    node = chain(60)
    path, r = made(tmp_path, node)
    ix = fresh(tmp_path, node, 'b.sqlite3')
    ix.import_snapshot(path)
    tip = ix.tip()
    node.hide = 5
    assert ix.sync()['skipped'] == 'stale' and ix.tip() == tip
    node.hide = 0
    ix.sync()
    check_chain(ix, node)
