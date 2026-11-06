# SPDX-License-Identifier: MIT
"""A toy chain for spike S1: what the EVM contract plus the backend plugin will
look like to CLN, without the EVM.  Transactions are applied as soon as they
are sent (there is no mempool), into the height that is still open; `mine`
closes it.  It checks inputs exist and are unspent, amounts, and the
timelocks (nLockTime, BIP68), but not scripts or signatures: that is the
contract's job.  Every applied transaction is logged with its prevouts, as
test vectors for the contract (spike S3)."""
from btc import Tx, block_hash, coinbase, header, merkle_root
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import hashlib
import json
import threading

GENESIS_TIME = 1700000000
INTERVAL = 600
# sat/kVB for (blocks): what estimatefees returns, until set_fees changes it
DEFAULT_FEES = ((2, 5000), (6, 4000), (12, 3000), (100, 1000))


class Chain:
    def __init__(self, log_path=None):
        self.lock = threading.Lock()
        self.blocks = []          # [{'hash', 'raw'}]
        self.open = []            # raw txs applied in the open height
        self.utxos = {}           # (txid, vout) -> (sats, script, height)
        self.txs = {}             # txid -> height
        self.raw = {}             # txid -> raw tx
        self.minted = 0
        self.fees = list(DEFAULT_FEES)
        self.rejects = []         # [(height, txid, errmsg, raw hex)]: txs send refused
        self.log = open(log_path, 'a') if log_path else None
        self._close_block()       # block 0

    def next_height(self):
        return len(self.blocks)

    def _close_block(self):
        h = self.next_height()
        txs = [coinbase(h).serialize()] + self.open
        txids = [Tx.parse(t).txid() for t in txs]
        prev = self.blocks[-1]['hash'] if self.blocks else '00' * 32
        hdr = header(prev, merkle_root(txids), GENESIS_TIME + h * INTERVAL)
        raw = hdr + bytes([len(txs)]) + b''.join(txs) if len(txs) < 0xfd else None
        assert raw is not None, 'toy chain: too many txs in a block'
        self.blocks.append({'hash': block_hash(hdr), 'raw': raw})
        self.open = []

    def mine(self, n):
        with self.lock:
            for _ in range(n):
                self._close_block()
            return self.next_height() - 1

    def tip(self):
        with self.lock:
            return self.next_height() - 1

    def block(self, height):
        with self.lock:
            if height >= len(self.blocks):
                return None
            b = self.blocks[height]
            return {'blockhash': b['hash'], 'block': b['raw'].hex()}

    def utxo(self, txid, vout):
        with self.lock:
            u = self.utxos.get((txid, vout))
            return None if u is None else {'amount': u[0], 'script': u[1].hex()}

    def _apply(self, tx, raw, prevouts):
        h = self.next_height()
        for txid, vout, _, _ in tx.inputs:
            self.utxos.pop((txid, vout), None)
        txid = tx.txid()
        for n, (sats, spk) in enumerate(tx.outputs):
            self.utxos[(txid, n)] = (sats, spk, h)
        self.txs[txid] = h
        self.raw[txid] = raw
        self.open.append(raw)
        if self.log:
            self.log.write(json.dumps({'height': h, 'txid': txid, 'tx': raw.hex(),
                                       'prevouts': prevouts}) + '\n')
            self.log.flush()
        return txid

    def send(self, hexstr):
        res = self._send(hexstr)
        if not res['success']:
            with self.lock:
                self.rejects.append((self.next_height(), Tx.parse(bytes.fromhex(hexstr)).txid(),
                                     res['errmsg'], hexstr))
        return res

    def _send(self, hexstr):
        raw = bytes.fromhex(hexstr)
        tx = Tx.parse(raw)
        with self.lock:
            h = self.next_height()
            txid = tx.txid()
            if txid in self.txs:
                return {'success': True, 'errmsg': 'already in chain'}
            prevouts, total_in = [], 0
            for txid_in, vout, _, seq in tx.inputs:
                u = self.utxos.get((txid_in, vout))
                if u is None:
                    return {'success': False, 'errmsg': 'missing or spent input %s:%d' % (txid_in, vout)}
                sats, spk, uh = u
                # BIP68: relative height lock (bit 31 clear, bit 22 clear)
                if tx.version >= 2 and not seq & (1 << 31) and not seq & (1 << 22):
                    if uh + (seq & 0xffff) > h:
                        return {'success': False, 'errmsg': 'non-BIP68-final'}
                prevouts.append({'amount': sats, 'script': spk.hex(), 'height': uh})
                total_in += sats
            if total_in < sum(s for s, _ in tx.outputs):
                return {'success': False, 'errmsg': 'outputs exceed inputs'}
            final = all(seq == 0xffffffff for _, _, _, seq in tx.inputs)
            if not final and tx.locktime < 500000000 and tx.locktime >= h:
                return {'success': False, 'errmsg': 'non-final (locktime %d, height %d)' % (tx.locktime, h)}
            if not final and tx.locktime >= 500000000 and tx.locktime >= GENESIS_TIME + h * INTERVAL:
                return {'success': False, 'errmsg': 'non-final (time locktime)'}
            self._apply(tx, raw, prevouts)
            return {'success': True, 'errmsg': ''}

    def deposit(self, script_hex, sats):
        """Mint: an output out of thin air (the contract's `deposit`)."""
        with self.lock:
            self.minted += 1
            fake = hashlib.sha256(b'mint%d' % self.minted).hexdigest()
            tx = Tx(2, [(fake, 0, b'', 0xffffffff)], [(sats, bytes.fromhex(script_hex))], 0)
            return self._apply(tx, tx.serialize(), [{'mint': True}])

    def feerates(self):
        """estimatefees' answer: (blocks, sat/kVB) pairs."""
        with self.lock:
            return [list(f) for f in self.fees]

    def set_fees(self, fees):
        with self.lock:
            self.fees = [tuple(f) for f in fees]

    def get_tx(self, txid):
        with self.lock:
            return self.raw.get(txid)

    def spenders(self, txid):
        """Applied txs spending an output of txid: [(spender txid, vout)]."""
        with self.lock:
            return [(t, v) for t, raw in self.raw.items()
                    for (pt, v, _, _) in Tx.parse(raw).inputs if pt == txid]

    def find_tx(self, txid):
        with self.lock:
            h = self.txs.get(txid)
            if h is None:
                return None
            return h


def serve(chain, port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            m, p = req['method'], req.get('params', [])
            fn = {'tip': chain.tip, 'block': chain.block, 'utxo': chain.utxo,
                  'send': chain.send, 'mine': chain.mine, 'deposit': chain.deposit,
                  'fees': chain.feerates}[m]
            body = json.dumps({'result': fn(*p)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
