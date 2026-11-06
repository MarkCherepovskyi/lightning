# SPDX-License-Identifier: MIT
"""The index of SPEC 5.2 (draft 4): every TxApplied log of the UtxoChain
contract, kept in SQLite, and the virtual chain of SPEC 3 served from it.

    ix = Index(path, evm.EvmRpc(url), contract, tag='safe', log=None)
    ix.sync()                     # one poll
    ix.add_receipt(receipt)       # our own submit, before the next poll
    ix.chaininfo()                # getchaininfo's answer (SPEC 5.3)
    ix.tip(), ix.ibd, ix.lag()    # SPEC 3.4
    ix.block(h)                   # (hash hex, raw block) for h <= tip, else None
    ix.utxo(txid_hex, vout)       # {'amount', 'script', 'height'} or None

How it stays right when the node's answers are not (SPEC 5.2):
- **Checkpoints.** After each poll's logs, the contract's txCount and logHash
  are read with eth_call pinned to the tag block's hash. Only if they equal
  the count and hash of the rows up to that block is a checkpoint recorded,
  and only checkpoints move the tip. Served blocks (h <= tip) are built from
  rows at or below the newest checkpoint only.
- **Rows are deleted only on a confirmed fork:** the node's block at the
  row's number has another hash, or a repair re-read from a checkpoint the
  node still agrees with, proven complete by the log hash, no longer has it. A reply that merely lacks a
  row (clamped, lagging, another backend) deletes nothing.
- **On a mismatch** a repair starts at once: find the newest checkpoint whose
  block hash the node still has (binary search; a block the node lacks
  postpones the repair), stage a re-read from there to the tag, and only if
  the staged rows' count and hash equal the contract's replace the index's
  rows in those blocks, in one transaction. If it still mismatches, the tip
  stays, a BROKEN line is logged, and the repair is retried after a backoff
  that doubles per failure (up to MAX_REPAIR_WAIT polls).
- **Stale backend:** a `latest` or tag below the previous poll's skips the poll.
- **Progress** is committed per log range; rate limits are retried with
  backoff (evm.EvmRpc.request).
- **Undecodable events** (raw tx that doesn't parse, or whose txid isn't its
  hash) are stored and counted in the log hash, kept out of served blocks and
  of the UTXO set, and logged at BROKEN level. They never stop the index.

Txids. Public methods take and return display-order hex; the database and
btc.py's bytes are internal order.

Threading. One SQLite connection, guarded by one lock (`self._lock`, an
RLock). Every database access holds it, briefly; no network call is ever made
while holding it. `sync()` runs one at a time (a second lock); `add_receipt`
and the readers may run concurrently with it.

Logging. `log(level, message)` with level 'broken', 'unusual', 'info' or
'debug' (default: the `evmindex` logger). Messages hold block numbers, counts
and txids, never the RPC URL or a reply's text (evm.EvmError carries only the
method, type and code).

No pyln dependency: the plugin calls this from its own threads.
"""
import logging
import sqlite3
import threading
import time

import btc
import evm

TX_APPLIED = 'TxApplied(uint32,bytes32,bytes)'
TOPIC0 = '0x' + evm.keccak256(TX_APPLIED.encode()).hex()
TAGS = ('safe', 'latest')   # SPEC 3.4: `finalized` is not supported
SCHEMA_VERSION = 2
HEADER_BATCH = 5000         # header hashes computed per lock hold
KEEP_CHECKPOINTS = 256      # walk-back depth before falling back to DEPLOY_BLOCK
MAX_CONFIRMS = 16           # eth_getBlockByNumber calls per sync to confirm forks
HASH_BATCH = 5000           # rows read per lock hold while hashing
MAX_REPAIR_WAIT = 8         # polls between repair attempts, at most (doubling after each failure)
ZERO32 = bytes(32)
# meta 'repair': none; a base checkpoint must be found first; a replacing re-read is under way
REPAIR_NONE, REPAIR_PENDING, REPAIR_ACTIVE = 0, 1, 2

# Output kinds: only KIND_UTXO outputs are ever unspent outputs (SPEC 4.4).
KIND_UTXO, KIND_EXIT, KIND_OP_RETURN = 0, 1, 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value);
-- One row per TxApplied log, bad = 1 for an undecodable one (counted,
-- never served). txid is internal byte order, and not unique: rows of two
-- forks can coexist until a repair settles them.
CREATE TABLE IF NOT EXISTS txs (
    id INTEGER PRIMARY KEY,
    evm_block INTEGER NOT NULL,
    evm_hash BLOB NOT NULL,
    log_index INTEGER NOT NULL,
    height INTEGER NOT NULL,
    txid BLOB NOT NULL,
    raw BLOB NOT NULL,
    bad INTEGER NOT NULL DEFAULT 0,
    UNIQUE (evm_block, log_index));
CREATE INDEX IF NOT EXISTS txs_height ON txs (height, evm_block, log_index);
CREATE INDEX IF NOT EXISTS txs_txid ON txs (txid);
CREATE TABLE IF NOT EXISTS outputs (
    tx_id INTEGER NOT NULL,
    vout INTEGER NOT NULL,
    txid BLOB NOT NULL,
    amount INTEGER NOT NULL,
    script BLOB NOT NULL,
    height INTEGER NOT NULL,
    kind INTEGER NOT NULL,
    PRIMARY KEY (tx_id, vout));
CREATE INDEX IF NOT EXISTS outputs_outpoint ON outputs (txid, vout);
CREATE TABLE IF NOT EXISTS spends (
    tx_id INTEGER NOT NULL,
    vin INTEGER NOT NULL,
    prev_txid BLOB NOT NULL,
    prev_vout INTEGER NOT NULL,
    PRIMARY KEY (tx_id, vin));
CREATE INDEX IF NOT EXISTS spends_prev ON spends (prev_txid, prev_vout);
-- Proven states: rows in EVM blocks <= number count `count` and hash to
-- `loghash`, as the contract said at block `hash`.
CREATE TABLE IF NOT EXISTS checkpoints (
    number INTEGER PRIMARY KEY,
    hash BLOB NOT NULL,
    timestamp INTEGER NOT NULL,
    count INTEGER NOT NULL,
    loghash BLOB NOT NULL);
-- A repair's re-read, kept apart until its count and hash are proven.
CREATE TABLE IF NOT EXISTS staged (
    evm_block INTEGER NOT NULL,
    evm_hash BLOB NOT NULL,
    log_index INTEGER NOT NULL,
    height INTEGER NOT NULL,
    txid BLOB NOT NULL,
    raw BLOB NOT NULL,
    bad INTEGER NOT NULL,
    PRIMARY KEY (evm_block, log_index));
-- Block hashes of heights 0..m (always a prefix), derived from the served
-- rows. Any change to what is served at height h deletes the entries >= h in
-- the same transaction, so a served block always reflects the index (3.4).
CREATE TABLE IF NOT EXISTS headers (height INTEGER PRIMARY KEY, hash BLOB NOT NULL);
"""


class EvmIndexError(Exception):
    """The index cannot be used as asked (wrong file, bad tag, no contract)."""


def output_kind(spk):
    if btc.is_exit_script(spk):
        return KIND_EXIT
    if btc.is_op_return(spk):
        return KIND_OP_RETURN
    return KIND_UTXO


def log_step(loghash, height, txid):
    """SPEC 4.1: keccak256(abi.encodePacked(logHash, uint32 height, bytes32 txid))."""
    return evm.keccak256(bytes(loghash) + (height & 0xffffffff).to_bytes(4, 'big') + bytes(txid))


def _int(x):
    return int(x, 16) if isinstance(x, str) else int(x)


def _default_log(level, message):
    logging.getLogger('evmindex').log(
        {'broken': logging.ERROR, 'unusual': logging.WARNING, 'info': logging.INFO}.get(level, logging.DEBUG),
        message)


class Index:
    def __init__(self, path, rpc, contract, tag='safe', log=None):
        """Opens (or creates) the index at `path` for `contract` on `rpc`'s
        chain. Reads the chain id and the contract's DEPLOY_BLOCK, GENESIS_TIME
        and INTERVAL (SPEC 5.1) and checks them against the file's, so a file
        is never reused for another contract or chain. Raises evm.EvmError if
        the node can't be reached, EvmIndexError on any mismatch."""
        if tag not in TAGS:
            raise EvmIndexError('confirmation tag must be one of {}, not {!r}'.format(TAGS, tag))
        self.rpc, self.tag, self.log = rpc, tag, log or _default_log
        self.contract = evm.checksum_address(contract)
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self.ibd = True             # until a sync of this process records a checkpoint
        self.last_sync = None       # time.time() of the last completed (not skipped) sync
        self.undecodable = 0        # undecodable events seen by this process
        self._latest = None         # (number, hash hex, timestamp) of `latest` at the last sync
        self._tagblock = None       # the same for the tag
        self._rev = 0               # bumped on every row change (under the lock)
        self._confirm_budget = MAX_CONFIRMS

        if not rpc.code(self.contract):
            raise EvmIndexError('no contract code at {}'.format(self.contract))
        chain_id = rpc.chain_id()
        consts = {k: self._read_const(k) for k in ('DEPLOY_BLOCK', 'GENESIS_TIME', 'INTERVAL')}
        if consts['INTERVAL'] == 0:
            raise EvmIndexError('INTERVAL is 0')
        self.chain_id = chain_id
        self.deploy_block = consts['DEPLOY_BLOCK']
        self.genesis_time = consts['GENESIS_TIME']
        self.interval = consts['INTERVAL']

        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        with self._lock:
            self._db.execute('PRAGMA journal_mode=WAL')
            self._db.execute('PRAGMA synchronous=FULL')    # the tip must survive a power loss
            with self._tx():
                for stmt in _SCHEMA.split(';'):
                    if stmt.strip():
                        self._db.execute(stmt)
                want = {'schema': SCHEMA_VERSION, 'contract': self.contract.lower(), 'chain_id': chain_id,
                        'deploy_block': self.deploy_block, 'genesis_time': self.genesis_time,
                        'interval': self.interval}
                have = dict(self._db.execute('SELECT key, value FROM meta'))
                for k, v in want.items():
                    if k in have and have[k] != v:
                        raise EvmIndexError('{} is for {} {}, not {}'.format(path, k, have[k], v))
                    if k not in have:
                        self._set_meta(k, v)

    def _read_const(self, name, block='latest'):
        return evm.abi_decode(['uint256'], self.rpc.eth_call(self.contract, evm.selector(name + '()'),
                                                             block))[0]

    def close(self):
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ db helpers

    class _Txn:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            self.db.execute('BEGIN IMMEDIATE')

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.db.execute('COMMIT')
            elif self.db.in_transaction:     # SQLite may already have rolled back (e.g. disk full)
                try:
                    self.db.execute('ROLLBACK')
                except sqlite3.Error:
                    pass                     # keep the original exception
            return False

    def _tx(self):
        """A write transaction; the caller holds self._lock."""
        return self._Txn(self._db)

    def _meta(self, key, default=None):
        row = self._db.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        return default if row is None else row[0]

    def _set_meta(self, key, value):
        self._db.execute('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)', (key, value))

    def _served(self):
        """Rows in EVM blocks up to this number are served in blocks: the
        newest checkpoint, or none (DEPLOY_BLOCK - 1)."""
        row = self._db.execute('SELECT MAX(number) FROM checkpoints').fetchone()
        return self.deploy_block - 1 if row[0] is None else row[0]

    def _invalidate_served_range(self, lo, hi):
        """What is served changed for rows in EVM blocks (lo, hi]: drop the
        cached header hashes from the lowest height among them."""
        row = self._db.execute('SELECT MIN(height) FROM txs WHERE evm_block > ? AND evm_block <= ? AND bad = 0',
                               (lo, hi)).fetchone()
        if row[0] is not None:
            self._db.execute('DELETE FROM headers WHERE height >= ?', (row[0],))

    def _drop_checkpoints_from(self, number):
        """Deletes the checkpoints >= number (they no longer describe the rows)."""
        old = self._served()
        self._db.execute('DELETE FROM checkpoints WHERE number >= ?', (number,))
        new = self._served()
        if new < old:
            self._invalidate_served_range(new, old)

    def _row_changed(self, evm_block, height):
        """A row in `evm_block` was added or deleted."""
        self._rev += 1
        if evm_block <= self._served():
            self._db.execute('DELETE FROM headers WHERE height >= ?', (height,))
        self._drop_checkpoints_from(evm_block)

    def _drop(self, row_id, evm_block, height):
        for table, col in (('outputs', 'tx_id'), ('spends', 'tx_id'), ('txs', 'id')):
            self._db.execute('DELETE FROM {} WHERE {} = ?'.format(table, col), (row_id,))
        self._row_changed(evm_block, height)

    def _drop_where(self, where, args):
        rows = self._db.execute('SELECT id, evm_block, height FROM txs WHERE ' + where, args).fetchall()
        for row in rows:
            self._drop(*row)
        return len(rows)

    def _insert(self, ev):
        """Stores one event. Returns 'new', 'same', or 'conflict' (another row
        holds its block and log index: left alone)."""
        db = self._db
        row = db.execute('SELECT evm_hash, txid FROM txs WHERE evm_block = ? AND log_index = ?',
                         (ev['block'], ev['log_index'])).fetchone()
        if row is not None:
            return 'same' if (row[0], row[1]) == (ev['hash'], ev['txid']) else 'conflict'
        cur = db.execute('INSERT INTO txs (evm_block, evm_hash, log_index, height, txid, raw, bad) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (ev['block'], ev['hash'], ev['log_index'], ev['height'], ev['txid'], ev['raw'],
                          1 if ev['tx'] is None else 0))
        row_id, tx = cur.lastrowid, ev['tx']
        if tx is not None:
            db.executemany('INSERT INTO outputs (tx_id, vout, txid, amount, script, height, kind) '
                           'VALUES (?, ?, ?, ?, ?, ?, ?)',
                           [(row_id, i, ev['txid'], amount, spk, ev['height'], output_kind(spk))
                            for i, (amount, spk) in enumerate(tx.outputs)])
            db.executemany('INSERT INTO spends (tx_id, vin, prev_txid, prev_vout) VALUES (?, ?, ?, ?)',
                           [(row_id, i, txin.prev_txid, txin.vout) for i, txin in enumerate(tx.inputs)])
        self._row_changed(ev['block'], ev['height'])
        return 'new'

    def _other_hash_blocks(self, events):
        """{block number: the events' hash} for the events' blocks where the
        index holds rows with another hash; the caller holds the lock."""
        out = {}
        for ev in events:
            if ev['block'] in out:
                continue
            if self._db.execute('SELECT 1 FROM txs WHERE evm_block = ? AND evm_hash != ? LIMIT 1',
                                (ev['block'], ev['hash'])).fetchone():
                out[ev['block']] = ev['hash']
        return out

    def _confirm(self, blocks, budget):
        """{number: True if the node's block at number has the given hash}, with
        at most `budget` eth_getBlockByNumber calls (unchecked ones False)."""
        out = {}
        for number, h in sorted(blocks.items()):
            if budget <= 0:
                out[number] = False
                continue
            budget -= 1
            b = self.rpc.block(number)
            out[number] = b is not None and evm.unhex(b['hash']) == h
        return out

    # ------------------------------------------------------------ events

    def parse_log(self, log):
        """A TxApplied log (from eth_getLogs or a receipt) -> event dict, or
        None if it isn't this contract's TxApplied. An undecodable event (raw
        tx that doesn't parse, or txid not its hash) comes back with tx None
        and is logged at BROKEN level. Never raises."""
        try:
            if evm.unhex(log['address']) != evm.address_bytes(self.contract):
                return None
            topics = [evm.unhex(t) for t in log['topics']]
            if len(topics) != 3 or '0x' + topics[0].hex() != TOPIC0 or any(len(t) != 32 for t in topics):
                return None
            block, li = _int(log['blockNumber']), _int(log['logIndex'])
            bhash = evm.unhex(log['blockHash'])
            if len(bhash) != 32 or block < 0 or li < 0:
                return None
        except Exception:
            return None         # pending, or not a log at all
        height, txid = int.from_bytes(topics[1], 'big'), topics[2]
        ev = {'block': block, 'hash': bhash, 'log_index': li, 'height': height, 'txid': txid,
              'raw': b'', 'tx': None}
        why = None
        try:
            ev['raw'] = evm.abi_decode(['bytes'], evm.unhex(log['data']))[0]
            tx = btc.Tx.parse(ev['raw'])
            if tx.txid() != txid:
                why = 'its raw tx hashes to {}'.format(tx.txid_hex())
            elif not 1 <= height <= 0xffffffff:
                why = 'height {}'.format(height)
            else:
                ev['tx'] = tx
        except Exception as e:
            why = 'its raw tx does not decode ({})'.format(type(e).__name__)
        if why:
            self.undecodable += 1
            self.log('broken', 'undecodable TxApplied {} in EVM block {} log {}: {}; counted, not served'
                     .format(btc.display(txid), block, li, why))
        return ev

    def _parse_logs(self, logs):
        return [ev for ev in (self.parse_log(x) for x in (logs or [])
                              if isinstance(x, dict) and not x.get('removed')) if ev is not None]

    def _needs_confirm(self, events):
        """{block number: hash} of the events' blocks that the node must
        confirm before they go in: the index holds rows there with another
        hash, or the block is at or below the newest checkpoint (proven) and
        the event isn't already its row. The caller holds the lock."""
        other = self._other_hash_blocks(events)
        served = self._served()
        for ev in events:
            if ev['block'] <= served and ev['block'] not in other and self._db.execute(
                    'SELECT 1 FROM txs WHERE evm_block = ? AND log_index = ? AND evm_hash = ? AND txid = ?',
                    (ev['block'], ev['log_index'], ev['hash'], ev['txid'])).fetchone() is None:
                other[ev['block']] = ev['hash']
        return other

    def _ingest(self, events, budget):
        """Inserts events (never deleting what a reply lacks). Where
        _needs_confirm says so, asks the node (at most `budget` calls) and
        replaces that block's rows only if the node has the event's hash; the
        check is redone inside the write, so a checkpoint made meanwhile is
        respected. Returns (inserted events, dropped rows, calls used)."""
        with self._lock:
            need = self._needs_confirm(events)
        confirmed = self._confirm(need, budget) if need else {}
        ok = {n for n, good in confirmed.items() if good}
        inserted, dropped = [], 0
        with self._lock, self._tx():
            for number in sorted(ok):
                dropped += self._drop_where('evm_block = ? AND evm_hash != ?', (number, need[number]))
            again = self._needs_confirm(events)
            for ev in events:
                if ev['block'] in again and ev['block'] not in ok:
                    continue
                if self._insert(ev) == 'new':
                    inserted.append(ev)
        return inserted, dropped, len(need)

    def add_receipt(self, receipt):
        """Ingests the TxApplied logs of a mined receipt at once (SPEC 5.2, our
        own submits), marked with its EVM block. A row already holding its
        block (another fork's), or a block at or below the newest checkpoint,
        is changed only if the node's block at that number has the receipt's
        hash. Returns the txids (hex) of its events now in the index."""
        if not isinstance(receipt, dict) or _int(receipt.get('status', '0x0')) != 1:
            return []
        events = self._parse_logs(receipt.get('logs'))
        self._ingest(events, MAX_CONFIRMS)
        out = []
        with self._lock:
            for ev in events:
                if self._db.execute('SELECT 1 FROM txs WHERE evm_block = ? AND log_index = ? AND evm_hash = ? '
                                    'AND txid = ?', (ev['block'], ev['log_index'], ev['hash'],
                                                     ev['txid'])).fetchone():
                    out.append(btc.display(ev['txid']))
                else:
                    self.log('unusual', 'receipt for EVM block {} disagrees with the index; not ingested'
                             .format(ev['block']))
        return out

    # ------------------------------------------------------------ polling

    def _scan(self, lat_n):
        """Reads logs from the cursor to lat_n (insert only), committing the
        cursor per range."""
        with self._lock:
            start = max(self.deploy_block, self._meta('cursor', self.deploy_block - 1) + 1)
        added = dropped = 0
        for first, last, logs in self.rpc.get_logs_chunked(self.contract, [TOPIC0], start, lat_n):
            inserted, d, used = self._ingest(self._parse_logs(logs), self._confirm_budget)
            self._confirm_budget -= used
            added, dropped = added + len(inserted), dropped + d
            with self._lock, self._tx():
                self._set_meta('cursor', last)
        return added, dropped

    def _contract_state(self, tagb, latestb=None):
        """((txCount, logHash) at the tag block, pinned to its hash
        (EIP-1898), quiet). The state is None if the tag is below DEPLOY_BLOCK
        or the node can't say. With latestb, logHash is first read at the
        latest block (pinned to its hash): it chains every applied tx, so if
        it equals the newest checkpoint's (and no repair is under way),
        nothing was applied from that checkpoint up to latest, the tag's
        state is the checkpoint's, and quiet is True: one eth_call."""
        tag_n = _int(tagb['number'])
        if tag_n < self.deploy_block:
            return None, False
        at = {'blockHash': tagb['hash'], 'requireCanonical': True}
        with self._lock:
            _, cp_count, cp_hash = self._base_checkpoint(tag_n)
            calm = self._meta('repair', REPAIR_NONE) == REPAIR_NONE and self._meta('cursor', -1) >= 0
        try:
            if latestb is not None and calm:
                at_latest = {'blockHash': latestb['hash'], 'requireCanonical': True}
                newest = evm.abi_decode(['bytes32'], self.rpc.eth_call(
                    self.contract, evm.selector('logHash()'), at_latest))[0]
                if bytes(newest) == bytes(cp_hash):
                    return (cp_count, bytes(cp_hash)), True
            count = self._read_const('txCount', at)
            loghash = evm.abi_decode(['bytes32'], self.rpc.eth_call(
                self.contract, evm.selector('logHash()'), at))[0]
        except evm.EvmError as e:
            if e.rate_limited:
                raise
            self.log('unusual', 'contract state at the {} block not readable: {}'.format(self.tag, e))
            return None, False
        except ValueError:
            self.log('unusual', 'contract state at the {} block: bad reply to eth_call'.format(self.tag))
            return None, False
        return (count, loghash), False

    def _base_checkpoint(self, upto):
        """(number, count, loghash) of the newest checkpoint <= upto, or the
        state before DEPLOY_BLOCK; the caller holds the lock."""
        row = self._db.execute('SELECT number, count, loghash FROM checkpoints WHERE number <= ? '
                               'ORDER BY number DESC LIMIT 1', (upto,)).fetchone()
        return tuple(row) if row else (self.deploy_block - 1, 0, ZERO32)

    def _hash_rows(self, table, lo, hi, count, loghash, rev):
        """Extends (count, loghash) over the rows of `table` in EVM blocks
        (lo, hi], in EVM order, reading in batches under the lock and hashing
        outside it. Returns None if the index changed meanwhile (rev)."""
        key = (lo, 1 << 62)
        while True:
            with self._lock:
                if rev is not None and self._rev != rev:
                    return None
                rows = self._db.execute(
                    'SELECT evm_block, log_index, height, txid FROM {} WHERE (evm_block, log_index) > (?, ?) '
                    'AND evm_block <= ? ORDER BY evm_block, log_index LIMIT ?'.format(table),
                    (key[0], key[1], hi, HASH_BATCH)).fetchall()
            if not rows:
                return count, loghash
            for _, _, height, txid in rows:
                count, loghash = count + 1, log_step(loghash, height, txid)
            key = (rows[-1][0], rows[-1][1])

    def _record_checkpoint(self, tagb, count, loghash):
        """Records a checkpoint at the tag block and raises the tip to its
        candidate; the caller holds the lock in a write transaction."""
        tag_n, tag_ts = _int(tagb['number']), _int(tagb['timestamp'])
        old = self._served()
        self._db.execute('INSERT OR REPLACE INTO checkpoints (number, hash, timestamp, count, loghash) '
                         'VALUES (?, ?, ?, ?, ?)', (tag_n, evm.unhex(tagb['hash']), tag_ts, count, loghash))
        self._db.execute('DELETE FROM checkpoints WHERE number NOT IN '
                         '(SELECT number FROM checkpoints ORDER BY number DESC LIMIT ?)', (KEEP_CHECKPOINTS,))
        if tag_n > old:
            self._invalidate_served_range(old, tag_n)
        candidate = max(self.height_at(tag_ts) - 1, 0)
        if candidate > self._meta('tip', 0):
            self._set_meta('tip', candidate)
        if self._meta('cursor', tag_n) > tag_n:
            self._set_meta('cursor', tag_n)     # re-read the unproven blocks above the tag next poll
        self._set_meta('repair', REPAIR_NONE)
        self._set_meta('repair_failures', 0)
        self._db.execute('DELETE FROM staged')

    def _prove(self, tagb, state):
        """True (checkpoint recorded) if the rows up to the tag block match the
        contract's state there, False on a mismatch, None if the index changed
        while hashing (try again next poll)."""
        tag_n = _int(tagb['number'])
        with self._lock:
            number, count, loghash = self._base_checkpoint(tag_n)
            rev = self._rev
        got = self._hash_rows('txs', number, tag_n, count, loghash, rev)
        if got is None:
            return None
        if got != tuple(state):
            return False
        with self._lock, self._tx():
            if self._rev != rev:
                return None
            self._record_checkpoint(tagb, *got)
        self.ibd = False
        return True

    def _drop_impossible(self, tagb):
        """Rows above the proven tag block at a height <= its candidate can't
        be on that chain (later blocks have later timestamps): they are left
        from a fork. Each such block is checked with the node (within the
        confirm budget) and its rows dropped if the node's block there has
        another hash. Returns the number dropped."""
        tag_n = _int(tagb['number'])
        candidate = max(self.height_at(_int(tagb['timestamp'])) - 1, 0)
        with self._lock:
            blocks = dict(self._db.execute('SELECT DISTINCT evm_block, evm_hash FROM txs '
                                           'WHERE evm_block > ? AND height <= ?', (tag_n, candidate)).fetchall())
        dropped = 0
        for number in sorted(blocks)[:max(self._confirm_budget, 0)]:
            self._confirm_budget -= 1
            b = self.rpc.block(number)
            if b is None:
                continue        # not (yet) on this backend: no proof of a fork
            with self._lock, self._tx():
                dropped += self._drop_where('evm_block = ? AND evm_hash != ? AND height <= ?',
                                            (number, evm.unhex(b['hash']), candidate))
        return dropped

    # A repair (SPEC 5.2 "On a mismatch"): find the newest checkpoint whose
    # block the node still has, stage a re-read of the logs from there to the
    # tag (committed per range, so it resumes), and only if the staged rows'
    # count and hash equal the contract's at the tag replace the index's rows
    # in those blocks with them, in one transaction. Nothing is deleted, and
    # no checkpoint dropped, before that proof; rows above the tag are never
    # touched by a repair.

    def _find_base(self, tag_n):
        """The newest checkpoint <= tag_n whose block hash the node has
        (binary search: valid ones are a prefix), DEPLOY_BLOCK - 1 if none, or
        None if the node can't tell (a block it doesn't have: a lagging
        backend is not a fork)."""
        with self._lock:
            cps = self._db.execute('SELECT number, hash FROM checkpoints WHERE number <= ? ORDER BY number',
                                   (tag_n,)).fetchall()
        lo, hi, valid = 0, len(cps) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            b = self.rpc.block(cps[mid][0])
            if b is None:
                return None
            if evm.unhex(b['hash']) == cps[mid][1]:
                valid, lo = mid, mid + 1
            else:
                hi = mid - 1
        return cps[valid][0] if valid >= 0 else self.deploy_block - 1

    def _stage(self, tag_n):
        """Continues the staged re-read up to tag_n."""
        with self._lock:
            start = self._meta('stage_cursor') + 1
        for first, last, logs in self.rpc.get_logs_chunked(self.contract, [TOPIC0], start, tag_n):
            events = self._parse_logs(logs)
            with self._lock, self._tx():
                self._db.execute('DELETE FROM staged WHERE evm_block BETWEEN ? AND ?', (first, last))
                self._db.executemany(
                    'INSERT OR REPLACE INTO staged (evm_block, evm_hash, log_index, height, txid, raw, bad) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    [(ev['block'], ev['hash'], ev['log_index'], ev['height'], ev['txid'], ev['raw'],
                      1 if ev['tx'] is None else 0) for ev in events if first <= ev['block'] <= last])
                self._set_meta('stage_cursor', last)

    def _apply_staged(self, tagb, base, got):
        """Replaces the rows of blocks (base, tag] with the staged ones and
        records the checkpoint. Returns (added, dropped), or None if the base
        checkpoint went away meanwhile."""
        tag_n = _int(tagb['number'])
        added = dropped = 0
        with self._lock, self._tx():
            if self._base_checkpoint(base)[0] != base:
                return None
            staged = {(r[0], r[2]): r for r in self._db.execute(
                'SELECT evm_block, evm_hash, log_index, height, txid, raw, bad FROM staged '
                'WHERE evm_block > ? AND evm_block <= ?', (base, tag_n))}
            for row_id, blk, li, bhash, txid, height in self._db.execute(
                    'SELECT id, evm_block, log_index, evm_hash, txid, height FROM txs '
                    'WHERE evm_block > ? AND evm_block <= ?', (base, tag_n)).fetchall():
                s = staged.get((blk, li))
                if s is None or s[1] != bhash or s[4] != txid:
                    self._drop(row_id, blk, height)
                    dropped += 1
            for key in sorted(staged):
                blk, bhash, li, height, txid, raw, bad = staged[key]
                tx = None
                if not bad:
                    try:
                        tx = btc.Tx.parse(raw)
                    except ValueError:
                        tx = None
                ev = {'block': blk, 'hash': bhash, 'log_index': li, 'height': height, 'txid': txid,
                      'raw': raw, 'tx': tx}
                added += self._insert(ev) == 'new'
            self._drop_checkpoints_from(base + 1)
            self._record_checkpoint(tagb, *got)
        self.ibd = False
        return added, dropped

    def _repair(self, tagb, state):
        """One repair attempt; True if it ended in a checkpoint."""
        tag_n = _int(tagb['number'])
        with self._lock:
            mode = self._meta('repair', REPAIR_NONE)
            failures, polls = self._meta('repair_failures', 0), self._meta('polls', 0)
            if mode != REPAIR_ACTIVE and polls < self._meta('repair_after', 0):
                return False        # backing off after failed repairs
        if mode != REPAIR_ACTIVE:
            base = self._find_base(tag_n)
            if base is None:
                self.log('unusual', 'repair postponed: the node lacks a checkpoint block')
                return False
            with self._lock, self._tx():
                self._db.execute('DELETE FROM staged')
                self._set_meta('repair_base', base)
                self._set_meta('stage_cursor', base)
                self._set_meta('repair', REPAIR_ACTIVE)
        with self._lock:
            base = self._meta('repair_base')
        self._stage(tag_n)
        with self._lock:
            _, count, loghash = self._base_checkpoint(base)
        got = self._hash_rows('staged', base, tag_n, count, loghash, None)
        if got == tuple(state):
            r = self._apply_staged(tagb, base, got)
            if r is not None:
                self.log('unusual', 'repaired from EVM block {}: {} rows dropped, {} added'
                         .format(base, r[1], r[0]))
                self._repaired = r
                return True
        with self._lock, self._tx():
            self._set_meta('repair', REPAIR_PENDING)
            self._set_meta('repair_failures', failures + 1)
            self._set_meta('repair_after', polls + min(2 ** failures, MAX_REPAIR_WAIT))
        return False

    def sync(self):
        """One poll (SPEC 5.2). Returns a summary dict; 'skipped' is set when
        the backend was stale, 'proven' says whether a checkpoint was made.
        Raises evm.EvmError on RPC failure (the next sync resumes from the last
        committed range)."""
        with self._sync_lock:
            tagb = self.rpc.block(self.tag)
            latest = tagb if self.tag == 'latest' else self.rpc.block('latest')
            try:
                tag_n, lat_n = _int(tagb['number']), _int(latest['number'])
                _int(tagb['timestamp']), _int(latest['timestamp']), evm.unhex(tagb['hash'])
            except Exception:
                raise evm.EvmError('eth_getBlockByNumber: bad reply') from None
            with self._lock:
                last_tag, last_lat = self._meta('last_tag', -1), self._meta('last_latest', -1)
            if tag_n < last_tag or lat_n < last_lat or tag_n > lat_n:
                self.log('unusual', 'stale backend: {} {} and latest {} (previous poll: {} and {}); poll skipped'
                         .format(self.tag, tag_n, lat_n, last_tag, last_lat))
                return {'skipped': 'stale', 'tip': self.tip()}
            self._confirm_budget = MAX_CONFIRMS
            self._repaired = (0, 0)
            with self._lock, self._tx():
                self._set_meta('polls', self._meta('polls', 0) + 1)
            with self._lock:
                caught_up = 0 <= last_tag <= self._meta('cursor', -1)
            if caught_up:
                # Nothing applied anywhere after the newest checkpoint (up to
                # latest): no logs to read, which saves the poll's eth_getLogs
                # (M5: Infura credits). Only once caught up: a backlog is
                # read first, so a rate-limited provider's budget goes to
                # making progress.
                state, quiet = self._contract_state(tagb, latest)
                added, dropped = (0, 0) if quiet else self._scan(lat_n)
            else:
                added, dropped = self._scan(lat_n)
                state, quiet = self._contract_state(tagb, None)
            if quiet:
                # logHash at latest proves there are no logs up to it.
                with self._lock, self._tx():
                    self._set_meta('quiet_polls', self._meta('quiet_polls', 0) + 1)
                    self._set_meta('cursor', max(self._meta('cursor', -1), lat_n))
            proven = None if state is None else self._prove(tagb, state)
            if proven is False:
                self.log('unusual', 'log hash mismatch at {} block {}'.format(self.tag, tag_n))
                proven = self._repair(tagb, state)
                added, dropped = added + self._repaired[0], dropped + self._repaired[1]
                if not proven:
                    self.log('broken', 'log hash mismatch at {} block {}: the tip stays at {}, retrying'
                             .format(self.tag, tag_n, self.tip()))
            if proven:
                dropped += self._drop_impossible(tagb)
            with self._lock, self._tx():
                self._set_meta('last_tag', tag_n)
                self._set_meta('last_latest', lat_n)
            self._latest = (lat_n, latest['hash'], _int(latest['timestamp']))
            self._tagblock = (tag_n, tagb['hash'], _int(tagb['timestamp']))
            self.last_sync = time.time()
            return {'skipped': None, 'proven': bool(proven), 'added': added, 'dropped': dropped,
                    'tip': self.tip(), 'tag_block': tag_n, 'latest_block': lat_n}

    # ------------------------------------------------------------ the chain (SPEC 3)

    def height_at(self, timestamp):
        """H(t), which is negative before GENESIS_TIME."""
        return (timestamp - self.genesis_time) // self.interval

    def tip(self):
        """The reported tip: the candidate of the newest checkpoint, the
        highest seen, persisted, never lower."""
        with self._lock:
            return self._meta('tip', 0)

    def chaininfo(self):
        """getchaininfo's answer (SPEC 5.3). Until a sync of this process has
        reached a checkpoint: ibd true, headercount the candidate tip of a live
        read of the tag (so a new or deleted index file doesn't report a chain
        shorter than lightningd's), blockcount the tip so far. Raises
        evm.EvmError if the tag can't be read then."""
        tip = self.tip()
        if not self.ibd:
            return {'chain': 'regtest', 'headercount': tip, 'blockcount': tip, 'ibd': False}
        try:     # one attempt, no rate-limit backoff: a read method must answer
            b = self.rpc.call('eth_getBlockByNumber', self.tag, False)
            ts = _int(b['timestamp']) if b else None
        except Exception:
            ts = None
        if ts is None:
            if self._tagblock is None:
                raise evm.EvmError('eth_getBlockByNumber: no {} block'.format(self.tag))
            ts = self._tagblock[2]
        return {'chain': 'regtest', 'headercount': max(self.height_at(ts) - 1, tip, 0),
                'blockcount': tip, 'ibd': True}

    def current_height(self):
        """H(latest) as of the last sync, or None before one."""
        return None if self._latest is None else max(self.height_at(self._latest[2]), 0)

    def lag(self):
        """H(latest) - tip, in virtual blocks, as of the last sync (SPEC 3.4,
        without CLN's own poll time, which the caller adds); None before one."""
        h = self.current_height()
        return None if h is None else h - self.tip()

    def tag_block(self):
        """(number, hash hex, timestamp) of the tag block at the last sync."""
        return self._tagblock

    def latest_block(self):
        return self._latest

    def checkpoint(self):
        """(number, hash hex, count, logHash hex) of the newest checkpoint, or None."""
        with self._lock:
            row = self._db.execute('SELECT number, hash, count, loghash FROM checkpoints '
                                   'ORDER BY number DESC LIMIT 1').fetchone()
        return None if row is None else (row[0], '0x' + row[1].hex(), row[2], '0x' + row[3].hex())

    def _cached_top(self):
        row = self._db.execute('SELECT MAX(height) FROM headers').fetchone()
        return -1 if row[0] is None else row[0]

    def _served_rows(self, lo, hi):
        return self._db.execute('SELECT height, txid, raw FROM txs WHERE height BETWEEN ? AND ? AND bad = 0 '
                                'AND evm_block <= ? ORDER BY height, evm_block, log_index',
                                (lo, hi, self._served()))

    def _fill_headers(self, upto):
        """Computes the header hashes of heights (cached top + 1)..upto, at most
        HEADER_BATCH of them; the caller holds the lock."""
        top = self._cached_top()
        first, last = top + 1, min(upto, top + HEADER_BATCH)
        if first > last:
            return
        prev = ZERO32 if first == 0 else self._db.execute(
            'SELECT hash FROM headers WHERE height = ?', (top,)).fetchone()[0]
        txids = {}
        for h, txid, _ in self._served_rows(first, last):
            txids.setdefault(h, []).append(txid)
        rows = []
        for h in range(first, last + 1):
            hdr, _ = btc.block_header(h, prev, txids.get(h, []), self.genesis_time, self.interval)
            prev = btc.block_hash(hdr)
            rows.append((h, prev))
        with self._tx():
            self._db.executemany('INSERT INTO headers (height, hash) VALUES (?, ?)', rows)

    def block(self, h):
        """Virtual block h of SPEC 3.2, built from the current index:
        (block hash, display hex; raw block bytes), or None above the tip."""
        if h < 0:
            return None
        while True:
            with self._lock:
                if h > self._meta('tip', 0):
                    return None
                if h > 0 and self._cached_top() < h - 1:
                    self._fill_headers(h - 1)
                    continue    # let other threads in between batches
                prev = ZERO32 if h == 0 else self._db.execute(
                    'SELECT hash FROM headers WHERE height = ?', (h - 1,)).fetchone()[0]
                rows = self._served_rows(h, h).fetchall()
                raw, hsh = btc.build_block(h, prev, [r[2] for r in rows], self.genesis_time,
                                           self.interval, txids=[r[1] for r in rows])
                if self._cached_top() == h - 1:
                    with self._tx():
                        self._db.execute('INSERT INTO headers (height, hash) VALUES (?, ?)', (h, hsh))
                return btc.display(hsh), raw

    def block_hash(self, h):
        """Hash (display hex) of block h <= tip, or None."""
        b = self.block(h)
        return None if b is None else b[0]

    # ------------------------------------------------------------ outputs

    def utxo_internal(self, txid, vout):
        """{'amount': sats, 'script': bytes, 'height': h} of an unspent output
        as of `latest` (everything indexed), or None if it is unknown, spent,
        an exit or an OP_RETURN. txid in internal byte order."""
        with self._lock:
            row = self._db.execute('SELECT amount, script, height, kind FROM outputs WHERE txid = ? AND vout = ? '
                                   'ORDER BY tx_id LIMIT 1', (bytes(txid), vout)).fetchone()
            if row is None or row[3] != KIND_UTXO:
                return None
            if self._db.execute('SELECT 1 FROM spends WHERE prev_txid = ? AND prev_vout = ? LIMIT 1',
                                (bytes(txid), vout)).fetchone():
                return None
            return {'amount': row[0], 'script': row[1], 'height': row[2]}

    def utxo(self, txid_hex, vout):
        """utxo_internal for a display-order hex txid (getutxout's)."""
        return self.utxo_internal(btc.internal(txid_hex), vout)

    def tx(self, txid_hex):
        """{'height', 'evm_block', 'evm_hash' (hex), 'log_index', 'raw'} of an
        indexed, decodable tx (display-order hex txid), or None."""
        with self._lock:
            row = self._db.execute('SELECT height, evm_block, evm_hash, log_index, raw FROM txs '
                                   'WHERE txid = ? AND bad = 0 ORDER BY evm_block DESC LIMIT 1',
                                   (btc.internal(txid_hex),)).fetchone()
        if row is None:
            return None
        return {'height': row[0], 'evm_block': row[1], 'evm_hash': '0x' + row[2].hex(),
                'log_index': row[3], 'raw': row[4]}

    def prevouts(self, tx):
        """[(amount, script, height)] for each input of a btc.Tx, as `submit`
        takes them, or None if any input is unknown or spent."""
        out = []
        for txin in tx.inputs:
            u = self.utxo_internal(txin.prev_txid, txin.vout)
            if u is None:
                return None
            out.append((u['amount'], u['script'], u['height']))
        return out
