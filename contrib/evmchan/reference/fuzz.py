#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Deterministic mutation fuzzer: writes cases/*.jsonl for differential
testing of the contract against pyref.

- cases/cln.jsonl: the real CLN transactions of spike S1, mutated without
  re-signing (byte flips, hash types, sequences, locktimes, versions,
  amounts, prevouts, witness shapes, inputs/outputs, encodings) and swept
  across the heights around every timelock boundary;
- cases/synth.jsonl: transactions built and signed here for every template
  path, then varied and RE-SIGNED, so that each case breaks exactly one rule
  (or none): CSV/CLTV one below, hash types, DER, NULLDUMMY, MINIMALIF,
  preimages, sigops/size/MAX_MONEY boundaries, MTP;
- cases/deposits.jsonl: 3.3's deposit tx and 4.2's deposit checks.

Each tx line: {tx, prevouts, height, genesis_time, interval, valid, error,
origin, txid, fee}.  Hand-written expectations (from the SPEC, not from
pyref) are asserted as cases are generated; a mismatch fails the run.
It follows SPEC draft 3's fixed readings.  Origins containing "ambiguous"
are the few cases whose verdict still depends on a reading the SPEC leaves
open (README).  In run(), a variant label starting with "~" gets that tag.
"""
import hashlib
import json
import os
import random
import struct
import sys

import coincurve

import pyref
from pyref import Tx, TxIn, hash160, ripemd160, sha256, varint
from check_vectors import analytic_earliest, load_cln, TIMELOCK_ERRORS

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 20261105
GEN, IV = pyref.GENESIS_TIME, pyref.INTERVAL
N = pyref.SECP_N
MAXM = pyref.MAX_MONEY
HASHTYPES_ALL = [0x00, 0x01, 0x02, 0x03, 0x04, 0x80, 0x81, 0x82, 0x83, 0x84, 0xff]


def H(label):
    return hashlib.sha256(b'evmchan-fuzz/' + label.encode()).digest()


class Key:
    def __init__(self, label):
        self.priv = coincurve.PrivateKey(H('key/' + label))
        self.pub = self.priv.public_key.format(True)
        self.xonly = self.priv.public_key_xonly.format()


K = {n: Key(n) for n in ('wallet', 'wallet2', 'tr', 'tr2', 'out', 'fund_a', 'fund_b', 'rev',
                         'delayed', 'remote', 'local_htlc', 'remote_htlc', 'anchor', 'other')}
PRE = H('preimage')
PHASH = sha256(PRE)
OUT1 = (99000, b'\x51\x20' + K['out'].xonly)
OUT2 = (500, b'\x00\x14' + hash160(K['out'].pub))

_aux = [0]


def ecdsa_sign(key, msg, ht):
    return key.priv.sign(msg, hasher=None) + bytes([ht])


def schnorr_sign(key, msg):
    _aux[0] += 1
    return key.priv.sign_schnorr(msg, H('aux/%d' % _aux[0]))


# ---------------------------------------------------------------- scripts
# Written from bitcoin/script.c (not from pyref's matcher), so they check it.
def push(b):
    assert len(b) < 76
    return bytes([len(b)]) + b


def num(n):
    """A minimal script number push, as add_number() writes a u32."""
    if n == 0:
        return b'\x00'
    if 1 <= n <= 16:
        return bytes([0x50 + n])
    b = n.to_bytes((n.bit_length() + 7) // 8, 'little')
    if b[-1] & 0x80:
        b += b'\x00'
    return push(b)


def w_2of2(ka, kb, sort=True):
    a, b = ka.pub, kb.pub
    if sort and b < a:
        a, b = b, a
    return b'\x52' + push(a) + push(b) + b'\x52\xae'


def w_to_local(rev, delayed, delay, numenc=None):
    return (b'\x63' + push(rev) + b'\x67' + (numenc if numenc is not None else num(delay))
            + b'\xb2\x75' + push(delayed) + b'\x68\xac')


def w_to_remote(key, csv=1):
    return push(key) + b'\xad' + num(csv) + b'\xb2'


def w_anchor(key):
    return push(key) + b'\xac\x73\x64\x60\xb2\x68'


def w_offered(local, remote, rev, phash):
    return (b'\x76\xa9' + push(hash160(rev)) + b'\x87\x63\xac\x67' + push(remote) + b'\x7c\x82'
            + num(32) + b'\x87\x64\x75' + num(2) + b'\x7c' + push(local) + num(2) + b'\xae\x67\xa9'
            + push(ripemd160(phash)) + b'\x88\xac\x68' + num(1) + b'\xb2\x75\x68')


def w_received(local, remote, rev, phash, cltv, numenc=None):
    return (b'\x76\xa9' + push(hash160(rev)) + b'\x87\x63\xac\x67' + push(remote) + b'\x7c\x82'
            + num(32) + b'\x87\x63\xa9' + push(ripemd160(phash)) + b'\x88' + num(2) + b'\x7c'
            + push(local) + num(2) + b'\xae\x67\x75' + (numenc if numenc is not None else num(cltv))
            + b'\xb1\x75\xac\x68' + num(1) + b'\xb2\x75\x68')


def offcurve_key(prefix=2):
    """A 33-byte 'key' whose x is not on the curve."""
    x = 1
    while True:
        c = (pow(x, 3, pyref.SECP_P) + 7) % pyref.SECP_P
        if pow(c, (pyref.SECP_P - 1) // 2, pyref.SECP_P) != 1:
            return bytes([prefix]) + x.to_bytes(32, 'big')
        x += 1


# ---------------------------------------------------------------- sig edits
def high_s(sig):
    r, s = pyref.der_rs(sig)
    return pyref.der_encode(r, N - s) + sig[-1:]


def der_pad_r(sig):
    r, s = pyref.der_rs(sig)
    rb = b'\x00' + r.to_bytes(32, 'big').lstrip(b'\x00')
    if rb[1] & 0x80:
        rb = b'\x00' + rb
    sb = s.to_bytes(32, 'big').lstrip(b'\x00')
    if sb[0] & 0x80:
        sb = b'\x00' + sb
    body = b'\x02' + bytes([len(rb)]) + rb + b'\x02' + bytes([len(sb)]) + sb
    return b'\x30' + bytes([len(body)]) + body + sig[-1:]


def set_ht(sig, ht):
    return sig[:-1] + bytes([ht])


def flip(b, pos, mask=1):
    b = bytearray(b)
    b[pos % len(b)] ^= mask
    return bytes(b)


# ---------------------------------------------------------------- tx specs
def inp(t, amount=100000, height=10, seq=0xffffffff, **kw):
    d = dict(t=t, amount=amount, height=height, seq=seq)
    d.update(kw)
    return d


def spec(ins, outs, version=2, locktime=0, **kw):
    d = dict(ins=ins, outs=outs, version=version, locktime=locktime)
    d.update(kw)
    return d


def clone(sp):
    out = dict(sp, ins=[dict(d) for d in sp['ins']], outs=list(sp['outs']))
    for d in out['ins']:
        if 'items' in d:
            d['items'] = list(d['items'])
    return out


def default_spk(d):
    if d['t'] == 'wpkh':
        return b'\x00\x14' + hash160(d.get('pubitem', d['k'].pub))
    if d['t'] == 'tr':
        return b'\x51\x20' + d.get('xonly', d['k'].xonly)
    return b'\x00\x20' + sha256(d['script'])


def make(sp):
    """Build and sign a spec -> (raw bytes, prevouts as dicts)."""
    txins = []
    for n, d in enumerate(sp['ins']):
        prev, vout = d.get('outpoint') or (H('outpoint/%d' % n), n)
        txins.append(TxIn(prev, vout, d.get('script_sig', b''), d['seq']))
    tx = Tx(sp['version'], txins, list(sp['outs']), sp['locktime'])
    pv = [(d['amount'], d.get('spk') or default_spk(d), d['height']) for d in sp['ins']]
    sh = pyref._Sighash(tx, pv)
    wits = []
    for i, d in enumerate(sp['ins']):
        amount, spk, _ = pv[i]
        if d['t'] == 'wpkh':
            code = b'\x76\xa9\x14' + spk[2:] + b'\x88\xac'
            ht = d.get('ht', 1)
            w = [ecdsa_sign(d['k'], sh.bip143(i, code, amount, ht), ht), d.get('pubitem', d['k'].pub)]
        elif d['t'] == 'tr':
            ht = d.get('ht', 0)
            msg = sh.bip341(i, ht) or H('no-message')
            w = [schnorr_sign(d['k'], msg) + (bytes([ht]) if ht or d.get('force65') else b'')]
        else:
            w = []
            for it in d['items']:
                if isinstance(it, tuple):
                    key, ht = it[1], it[2]
                    s = ecdsa_sign(key, sh.bip143(i, d['script'], amount, ht), ht)
                    w.append(it[3](s) if len(it) > 3 else s)
                else:
                    w.append(it)
            w.append(d['script'])
        if 'post' in d:
            w = d['post'](w)
        wits.append(w)
    tx.witnesses = wits
    raw = tx.serialize()
    if 'raw_post' in sp:
        raw = sp['raw_post'](raw)
    prevouts = [dict(amount=a, script=s.hex(), height=h) for a, s, h in pv]
    if 'pv_post' in sp:
        prevouts = sp['pv_post'](prevouts)
    return raw, prevouts


# ---------------------------------------------------------------- writer
class Writer:
    def __init__(self, name):
        self.path = os.path.join(HERE, 'cases', name)
        self.f = open(self.path, 'w')
        self.seen = set()
        self.valid = self.invalid = self.skipped = 0
        self.mismatches = []

    def case(self, raw, prevouts, h, origin, expect=None):
        raw = raw.hex() if isinstance(raw, (bytes, bytearray)) else raw
        if any(p['height'] < 1 or p['height'] > h for p in prevouts) and h > 0:
            self.skipped += 1       # no such UTXO can exist; a harness can't build it
            return None
        key = (raw, json.dumps(prevouts), h)
        if key in self.seen:
            return None
        self.seen.add(key)
        ok, err, txid, fee = pyref.validate(raw, prevouts, h, GEN, IV)
        if expect is not None:
            good = (ok if expect is True else
                    (not ok and err in TIMELOCK_ERRORS) if expect == 'timelock' else
                    (not ok) if expect is False else (not ok and err == expect))
            if not good:
                self.mismatches.append('%s: expected %s, got %s' % (origin, expect, 'valid' if ok else err))
        self.f.write(json.dumps(dict(tx=raw, prevouts=prevouts, height=h, genesis_time=GEN, interval=IV,
                                     valid=ok, error=err, origin=origin, txid=txid, fee=fee)) + '\n')
        if ok:
            self.valid += 1
        else:
            self.invalid += 1
        return ok

    def close(self):
        self.f.close()
        print('%-22s %5d cases: %5d valid, %5d invalid%s' % (
            os.path.relpath(self.path, HERE), self.valid + self.invalid, self.valid, self.invalid,
            ', %d impossible prevout heights skipped' % self.skipped if self.skipped else ''))


def earliest(raw, prevouts):
    return analytic_earliest(pyref.parse_tx(raw), prevouts, GEN, IV)


def sweep(w, origin, raw, prevouts, extra=(), expect_valid=True):
    """Heights around the earliest valid one; returns that height."""
    e = earliest(raw, prevouts)
    lo = max([1] + [p['height'] for p in prevouts])
    exp = True if expect_valid else None
    if e > lo:
        w.case(raw, prevouts, e - 1, origin + '@E-1', 'timelock' if expect_valid else None)
    for dh, tag in ((0, 'E'), (1, 'E+1'), (1000, 'E+1000')):
        w.case(raw, prevouts, e + dh, origin + '@' + tag, exp)
    for h in extra:
        w.case(raw, prevouts, h, origin + '@%d' % h, None)
    return e


def random_mutations(w, origin, raw, prevouts, h, n, rng):
    raw = bytes.fromhex(raw) if isinstance(raw, str) else raw
    for k in range(n):
        b = bytearray(raw)
        kind = rng.choice(('bitflip', 'byte', 'delete', 'insert', 'bitflip2'))
        pos = rng.randrange(len(b))
        if kind == 'bitflip':
            b[pos] ^= 1 << rng.randrange(8)
        elif kind == 'bitflip2':
            b[pos] ^= 1 << rng.randrange(8)
            p2 = rng.randrange(len(b))
            b[p2] ^= 1 << rng.randrange(8)
        elif kind == 'byte':
            b[pos] = rng.randrange(256)
        elif kind == 'delete':
            del b[pos]
        else:
            b.insert(pos, rng.randrange(256))
        w.case(bytes(b), prevouts, h, '%s:random-%s@%d' % (origin, kind, pos))


# ---------------------------------------------------------------- encodings
def ser_nc(tx, field, prefix=0xfd):
    """Serialize with one varint written non-canonically."""
    def vi(n, f):
        if f != field:
            return varint(n)
        width = {0xfd: 2, 0xfe: 4, 0xff: 8}[prefix]
        return bytes([prefix]) + n.to_bytes(width, 'little')
    segwit = any(tx.witnesses)
    out = struct.pack('<I', tx.version) + (b'\x00\x01' if segwit else b'') + vi(len(tx.inputs), 'nin')
    for j, x in enumerate(tx.inputs):
        out += x.outpoint() + vi(len(x.script_sig), 'ss%d' % j) + x.script_sig + struct.pack('<I', x.sequence)
    out += vi(len(tx.outputs), 'nout')
    for j, (a, s) in enumerate(tx.outputs):
        out += struct.pack('<Q', a) + vi(len(s), 'spk%d' % j) + s
    if segwit:
        for j, wit in enumerate(tx.witnesses):
            out += vi(len(wit), 'wc%d' % j)
            for k, item in enumerate(wit):
                out += vi(len(item), 'wi%d_%d' % (j, k)) + item
    return out + struct.pack('<I', tx.locktime)


def encoding_mutations(w, origin, raw, prevouts, h, rng, big=False):
    tx = pyref.parse_tx(raw)
    w.case(raw + b'\x00', prevouts, h, origin + ':trailing-byte', 'TrailingBytes')
    w.case(raw + raw[-4:], prevouts, h, origin + ':trailing-4', 'TrailingBytes')
    for cut in (1, 4, 5, len(raw) // 2, len(raw) - 5):
        w.case(raw[:-cut], prevouts, h, origin + ':truncated-%d' % cut, False)
    fields = ['nin', 'nout', 'spk0', 'ss0', 'wc0', 'wi0_0']
    for f in fields:
        for p in (0xfd, 0xfe, 0xff):
            if f.startswith('w') and not any(tx.witnesses):
                continue
            w.case(ser_nc(tx, f, p), prevouts, h, '%s:noncanonical-%s-%02x' % (origin, f, p), 'NonCanonicalVarint')
    if any(tx.witnesses):
        w.case(raw[:4] + b'\x00\x02' + raw[6:], prevouts, h, origin + ':flag-02', 'BadWitnessFlag')
        w.case(raw[:4] + b'\x00\x00' + raw[6:], prevouts, h, origin + ':flag-00', 'BadWitnessFlag')
        w.case(tx.serialize(witness=False), prevouts, h, origin + ':witness-stripped', 'BadWitness')
        empty = Tx(tx.version, tx.inputs, tx.outputs, tx.locktime, [[] for _ in tx.inputs])
        sup = (struct.pack('<I', tx.version) + b'\x00\x01' + empty.serialize(False)[4:-4]
               + b'\x00' * len(tx.inputs) + struct.pack('<I', tx.locktime))
        w.case(sup, prevouts, h, origin + ':superfluous-witness', 'SuperfluousWitness')
    if big:
        w.case(raw + b'\x00' * (pyref.MAX_TX_SIZE + 1 - len(raw)), prevouts, h, origin + ':size-100001', 'TxTooLarge')
    nout = Tx(tx.version, tx.inputs, [], tx.locktime, tx.witnesses)
    w.case(nout.serialize(), prevouts, h, origin + ':no-outputs', 'NoOutputs')
    w.case(raw, prevouts, 0, origin + ':height-0', 'TooEarly')


# ---------------------------------------------------------------- CLN
OTHER_SCRIPTS = [
    ('exit', '6014' + '11' * 20), ('op_return', '6a'), ('op_return-data', '6a0401020304'),
    ('p2sh', 'a914' + '22' * 20 + '87'), ('p2pkh', '76a914' + '33' * 20 + '88ac'), ('empty', ''),
    ('p2wpkh-19', '0013' + '44' * 19), ('p2wpkh-21', '0015' + '44' * 21), ('p2wsh-31', '001f' + '55' * 31),
    ('p2tr-31', '511f' + '66' * 31), ('p2tr-33', '5121' + '66' * 33), ('v2-32', '5220' + '77' * 32),
    ('v1-20', '5114' + '88' * 20), ('v0-pushdata1', '004c20' + '99' * 32), ('bare-2of2', '5221' + '02' + '11' * 32
                                                                           + '21' + '03' + '22' * 32 + '52ae'),
]


def sig_types(tx, pv):
    """Per input, the hash types of its signatures ('tr' for a key path)."""
    out = []
    for i, wit in enumerate(tx.witnesses):
        if pv[i]['script'].startswith('5120'):
            out.append(['tr'])
        else:
            out.append([x[-1] for x in wit[:-1] if pyref.is_strict_der(x)])
    return out


def cln_mutations(w, d, idx, rng, full):
    raw = bytes.fromhex(d['tx'])
    pv = d['prevouts']
    H0 = d['height']
    o = 'cln:%d:%s' % (idx, d.get('kind') or 'tx')
    tx = pyref.parse_tx(raw)
    e = sweep(w, o + ':as-is', raw, pv, extra=(H0, H0 + 1, H0 + 10, H0 + 100000))
    hi = max(H0, e) + 1000

    def emit(t, label, expect=None, prevouts=None, h=None):
        w.case(t.serialize() if isinstance(t, Tx) else t, prevouts or pv, hi if h is None else h,
               '%s:%s' % (o, label), expect)

    def copy_tx():
        return Tx(tx.version, [TxIn(x.prev_txid, x.vout, x.script_sig, x.sequence) for x in tx.inputs],
                  list(tx.outputs), tx.locktime, [list(x) for x in tx.witnesses])

    # fee bumping, which only SINGLE/NONE and ANYONECANPAY signatures allow
    types = [t for ts in sig_types(tx, pv) for t in ts]
    acp_ok = all(t != 'tr' and t & 0x80 for t in types)
    out_ok = all(t != 'tr' and (t & 0x1f) in (2, 3) for t in types)
    for add_in, add_out in ((True, False), (False, True), (True, True)):
        t = copy_tx()
        q = [dict(x) for x in pv]
        if add_in:
            t.inputs.append(TxIn(H('bump-in/%d' % idx), 0, b'', 0xfffffffd))
            t.witnesses.append([])
            q.append(dict(amount=50000, script=(b'\x51\x20' + K['tr'].xonly).hex(), height=1))
        if add_out:
            t.outputs.append((49000 if add_in else 0, OUT2[1]))
        if add_in:
            npv = [(x['amount'], bytes.fromhex(x['script']), x['height']) for x in q]
            t.witnesses[-1] = [schnorr_sign(K['tr'], pyref._Sighash(t, npv).bip341(len(t.inputs) - 1, 0))]
        ok = (acp_ok or not add_in) and (out_ok or not add_out)
        emit(t, 'bump%s%s' % ('-add-input' if add_in else '', '-add-output' if add_out else ''),
             True if ok else False, q, e + 1)
    # prevout heights: not signed, only the age matters
    for i, p in enumerate(pv):
        for label, ph in (('1', 1), ('-1', p['height'] - 1), ('+1', p['height'] + 1), ('=h', e)):
            if 1 <= ph and (full or label in ('1', '+1')):
                q = [dict(x) for x in pv]
                q[i]['height'] = ph
                sweep(w, '%s:prevout%d-height%s' % (o, i, label), raw, q)
    if not full:
        w.case(raw, pv, 0, o + ':height-0', 'TooEarly')
        random_mutations(w, o, raw, pv, hi, 10, rng)
        return
    # witness items: flips, hash types, shapes
    did_ht = False
    for i, wit in enumerate(tx.witnesses):
        for j, item in enumerate(wit):
            if not item:
                continue
            for k in range(2 if len(item) > 33 else 1):
                t = copy_tx()
                pos = rng.randrange(len(item))
                t.witnesses[i][j] = flip(item, pos, 1 << rng.randrange(8))
                emit(t, 'in%d-wit%d-flip@%d' % (i, j, pos), False)
            is_der = pyref.is_strict_der(item)
            if not did_ht and (is_der or (pv[i]['script'].startswith('5120') and len(item) in (64, 65))):
                did_ht = True
                for ht in HASHTYPES_ALL:
                    t = copy_tx()
                    if len(item) == 64:
                        t.witnesses[i][j] = item + bytes([ht])
                    else:
                        t.witnesses[i][j] = set_ht(item, ht)
                    emit(t, 'in%d-wit%d-hashtype-%02x' % (i, j, ht))
                if len(item) == 65:
                    t = copy_tx()
                    t.witnesses[i][j] = item[:64]
                    emit(t, 'in%d-wit%d-drop-hashtype' % (i, j), False)
            if is_der:
                t = copy_tx()
                t.witnesses[i][j] = high_s(item)
                emit(t, 'in%d-wit%d-high-s' % (i, j), 'HighS')
                t = copy_tx()
                t.witnesses[i][j] = der_pad_r(item)
                emit(t, 'in%d-wit%d-der-pad' % (i, j), 'BadSigEncoding')
                t = copy_tx()
                t.witnesses[i][j] = b''
                emit(t, 'in%d-wit%d-empty-sig' % (i, j), False)
        t = copy_tx()
        t.witnesses[i] = wit[:-1]
        emit(t, 'in%d-drop-last-item' % i, False)
        t = copy_tx()
        t.witnesses[i] = wit[1:]
        emit(t, 'in%d-drop-first-item' % i, False)
        t = copy_tx()
        t.witnesses[i] = [b''] + wit
        emit(t, 'in%d-extra-item-front' % i, False)
        t = copy_tx()
        t.witnesses[i] = wit + [b'']
        emit(t, 'in%d-extra-item-back' % i, False)
        t = copy_tx()
        t.witnesses[i] = []
        emit(t, 'in%d-no-witness' % i, False)
        if len(wit) == 4 and pv[i]['script'].startswith('0020'):    # 2-of-2
            t = copy_tx()
            t.witnesses[i] = [wit[0], wit[2], wit[1], wit[3]]
            emit(t, 'in%d-2of2-swapped-sigs' % i, 'BadSignature')
            t = copy_tx()
            t.witnesses[i] = [b'\x00'] + wit[1:]
            emit(t, 'in%d-2of2-dummy-00' % i, 'NullDummy')
            t = copy_tx()
            t.witnesses[i] = [b'\x01'] + wit[1:]
            emit(t, 'in%d-2of2-dummy-01' % i, 'NullDummy')
        tm = pyref.match_template(wit[-1]) if wit else None
        if tm and tm[0] == 'to_local' and wit[1] == b'':
            for sel, exp in ((b'\x01', 'BadSignature'), (b'\x00', 'MinimalIf'), (b'\x02', 'MinimalIf'),
                             (b'\x00\x00', 'MinimalIf')):
                t = copy_tx()
                t.witnesses[i] = [wit[0], sel, wit[2]]
                emit(t, 'in%d-to_local-selector-%s' % (i, sel.hex() or 'empty'), exp)
        if tm and tm[0] == 'offered' and len(wit) == 5:
            for junk, exp, tag in ((H('junk')[:32], 'BadPreimage', 'preimage-32-wrong'),
                                   (b'\x01', 'BadTimeoutDummy', 'dummy-01'),
                                   (b'\x00' * 33, 'BadTimeoutDummy', 'dummy-33'),
                                   (b'\x07' * 520, 'BadTimeoutDummy', 'dummy-520'),
                                   (b'\x07' * 521, 'BadTimeoutDummy', 'dummy-521')):
                t = copy_tx()
                t.witnesses[i] = wit[:3] + [junk, wit[4]]
                emit(t, 'in%d-offered-timeout-%s' % (i, tag), exp)
            t = copy_tx()
            t.witnesses[i] = [wit[0], wit[2], wit[1], wit[3], wit[4]]
            emit(t, 'in%d-offered-timeout-swapped-sigs' % i, 'BadSignature')
            t = copy_tx()
            t.witnesses[i] = [b'\x00'] + wit[1:]
            emit(t, 'in%d-offered-timeout-dummy-00' % i, 'NullDummy')
            t = copy_tx()
            t.witnesses[i] = [b''] + wit[2:4] + [PRE, wit[4]]
            emit(t, 'in%d-offered-as-success-wrong-preimage' % i, False)
    # sequences, locktime, version (all signed: invalid, unless timelock-only)
    for i, x in enumerate(tx.inputs):
        for label, seq in (('-1', x.sequence - 1), ('+1', x.sequence + 1), ('final', 0xffffffff), ('zero', 0),
                           ('^disable', x.sequence ^ pyref.SEQ_DISABLE), ('^type', x.sequence ^ pyref.SEQ_TYPE)):
            t = copy_tx()
            t.inputs[i].sequence = seq & 0xffffffff
            emit(t, 'in%d-seq%s' % (i, label), False)
    for label, lt in (('-1', tx.locktime - 1), ('+1', tx.locktime + 1), ('0', 0), ('499999999', 499999999),
                      ('500000000', 500000000), ('max', 0xffffffff)):
        t = copy_tx()
        t.locktime = lt & 0xffffffff
        if t.locktime != tx.locktime:
            emit(t, 'locktime%s' % label, False)
    for v in (0, 1, 3, 0x7fffffff, 0x80000002, 0xffffffff):
        t = copy_tx()
        t.version = v
        if v != tx.version:
            emit(t, 'version-%x' % v, False)
    # outputs and amounts
    t = copy_tx()
    t.outputs[0] = (t.outputs[0][0] + 1, t.outputs[0][1])
    emit(t, 'out0-amount+1', False)
    t = copy_tx()
    t.outputs[0] = (MAXM + 1, t.outputs[0][1])
    emit(t, 'out0-amount-maxmoney+1', 'OutputTooLarge')
    t = copy_tx()
    t.outputs[0] = (2**64 - 1, t.outputs[0][1])
    emit(t, 'out0-amount-u64max', 'OutputTooLarge')
    t = copy_tx()
    t.outputs.append((0, b'\x6a'))
    emit(t, 'add-opreturn-output', True if out_ok else False)
    if len(tx.outputs) > 1:
        t = copy_tx()
        t.outputs.pop()
        emit(t, 'drop-last-output', False)
    t = copy_tx()
    t.outputs.append((0, b'\xae' * 200))
    emit(t, 'add-sigops-output', 'TooManySigops')
    # inputs
    t = copy_tx()
    t.inputs.append(TxIn(H('extra-in'), 0, b'', 0xffffffff))
    t.witnesses.append([])
    emit(t, 'add-input', False, pv + [dict(amount=1000, script='0014' + '12' * 20, height=1)])
    if len(tx.inputs) > 1:
        t = copy_tx()
        t.inputs.pop(0)
        t.witnesses.pop(0)
        emit(t, 'drop-input0', False, pv[1:])
    t = copy_tx()
    t.inputs.append(TxIn(tx.inputs[0].prev_txid, tx.inputs[0].vout, b'', tx.inputs[0].sequence))
    t.witnesses.append(list(tx.witnesses[0]))
    emit(t, 'duplicate-input0', 'DuplicatePrevout', pv + [pv[0]])
    t = copy_tx()
    t.inputs[0].prev_txid, t.inputs[0].vout = b'\x00' * 32, 0xffffffff
    emit(t, 'null-prevout', 'NullPrevout')
    t = copy_tx()
    t.inputs[0].vout ^= 1
    emit(t, 'vout^1', False)
    t = copy_tx()
    t.inputs[0].script_sig = b'\x00'
    emit(t, 'scriptsig-00', 'ScriptSigNotEmpty')
    # prevouts
    emit(raw, 'prevouts-one-short', 'PrevoutCountMismatch', pv[:-1] if len(pv) > 1 else [])
    emit(raw, 'prevouts-one-extra', 'PrevoutCountMismatch', pv + [pv[0]])
    for i, p in enumerate(pv):
        for label, a in (('+1', p['amount'] + 1), ('-1', p['amount'] - 1), ('0', 0),
                         ('maxmoney+1', MAXM + 1)):
            q = [dict(x) for x in pv]
            q[i]['amount'] = a
            emit(raw, 'prevout%d-amount%s' % (i, label), False, q)
        for name, s in (OTHER_SCRIPTS if i == 0 else OTHER_SCRIPTS[:4]):
            q = [dict(x) for x in pv]
            q[i]['script'] = s
            emit(raw, 'prevout%d-script-%s' % (i, name), 'UnknownScript' if i == 0 or len(pv) == 1 else False, q)
        q = [dict(x) for x in pv]
        q[i]['script'] = flip(bytes.fromhex(p['script']), 5).hex()
        emit(raw, 'prevout%d-script-program-flip' % i, False, q)
    encoding_mutations(w, o, raw, pv, hi, rng)
    random_mutations(w, o, raw, pv, hi, 25, rng)


# ---------------------------------------------------------------- synthetic
def run(w, name, base, variants, rng, nrandom=12):
    """Base spec (valid) swept over heights, then each re-signed variant.
    variant: (label, expect, mutate(spec)[, [heights or (height, expect)]])."""
    raw, pv = make(base)
    e = sweep(w, 'synth:%s:base' % name, raw, pv)
    hi = e + 1000
    for v in variants:
        label, expect, mut = v[:3]
        sp = clone(base)
        mut(sp)
        raw2, pv2 = make(sp)
        tag = ('ambiguous:' if label.startswith('~') else 'synth:') + '%s:%s' % (name, label.lstrip('~'))
        heights = v[3] if len(v) > 3 else [hi]
        for hh in heights:
            h, ex = hh if isinstance(hh, tuple) else (hh, expect)
            w.case(raw2, pv2, h, '%s@%d' % (tag, h), ex)
        if expect is True and len(v) <= 3:
            sweep(w, tag, raw2, pv2)
    random_mutations(w, 'synth:%s' % name, raw, pv, hi, nrandom, rng)
    return raw, pv


def setv(i, **kw):
    def f(sp):
        sp['ins'][i].update(kw)
    return f


def setitem(i, j, value):
    def f(sp):
        sp['ins'][i]['items'][j] = value
    return f


def sett(**kw):
    def f(sp):
        sp.update(kw)
    return f


def seq(i, s):
    return setv(i, seq=s)


def both(*fs):
    def f(sp):
        for g in fs:
            g(sp)
    return f


def sig_variants(i, j, key, base_ht=1):
    """Hash types, DER and S edits of item j (an ECDSA sig) of input i."""
    out = []
    for ht in (0x01, 0x02, 0x03, 0x81, 0x82, 0x83):
        if ht != base_ht:
            out.append(('sig%d-ht-%02x' % (j, ht), True, setitem(i, j, ('sig', key, ht))))
    for ht in (0x00, 0x04, 0x80, 0x84, 0xff):
        out.append(('sig%d-ht-%02x' % (j, ht), 'BadHashType', setitem(i, j, ('sig', key, ht))))
    out += [
        ('sig%d-high-s' % j, 'HighS', setitem(i, j, ('sig', key, base_ht, high_s))),
        ('sig%d-der-pad-r' % j, 'BadSigEncoding', setitem(i, j, ('sig', key, base_ht, der_pad_r))),
        ('sig%d-bad-der-len' % j, 'BadSigEncoding', setitem(i, j, ('sig', key, base_ht, lambda s: s[:1] + bytes([s[1] + 1]) + s[2:]))),
        ('sig%d-flip-r' % j, 'BadSignature', setitem(i, j, ('sig', key, base_ht, lambda s: flip(s, 10)))),
        ('sig%d-flip-s' % j, False, setitem(i, j, ('sig', key, base_ht, lambda s: flip(s, -3)))),
        ('sig%d-ht-swapped-after-signing' % j, 'BadSignature',
         setitem(i, j, ('sig', key, base_ht, lambda s: set_ht(s, 0x81 if s[-1] != 0x81 else 0x01)))),
        ('sig%d-wrong-key' % j, 'BadSignature', setitem(i, j, ('sig', K['other'], base_ht))),
    ]
    return out


def csv_variants(i, n, hi):
    """CSV n on input i, whose base sequence is n (version 2)."""
    out = [
        ('seq-n-1', 'CsvNotMet' if n >= 1 else None, seq(i, max(n - 1, 0))),
        ('version-1', 'CsvNotMet', sett(version=1)),
        ('version-0', 'CsvNotMet', sett(version=0)),
        ('version-u32-max', True, sett(version=0xffffffff)),
        ('version-0x80000002', True, sett(version=0x80000002)),
        ('seq-disable', 'CsvNotMet', seq(i, n | pyref.SEQ_DISABLE)),
        ('seq-final', 'CsvNotMet', seq(i, 0xffffffff)),
        ('seq-type-bit', 'RelativeTimeLockUnsupported', seq(i, n | pyref.SEQ_TYPE)),
        ('seq-type-bit-v1', 'CsvNotMet', both(seq(i, n | pyref.SEQ_TYPE), sett(version=1))),
        ('seq-bit16', True, seq(i, n | 0x10000)),
        ('seq-n+1', True, seq(i, n + 1)),
    ]
    if n >= 1:
        out.append(('seq-(n-1)|bit16', 'CsvNotMet', seq(i, (n - 1) | 0x10000)))
    return out


def gen_synth(w, rng):
    W, W2, T, T2 = K['wallet'], K['wallet2'], K['tr'], K['tr2']
    REV, DEL, RMT, LH, RH, ANC = K['rev'], K['delayed'], K['remote'], K['local_htlc'], K['remote_htlc'], K['anchor']

    # --- P2WPKH
    base = spec([inp('wpkh', k=W, ht=1)], [OUT1, OUT2])
    unc = W.priv.public_key.format(False)
    v = []
    for ht in (0x02, 0x03, 0x81, 0x82, 0x83):
        v.append(('ht-%02x' % ht, True, setv(0, ht=ht)))
    for ht in (0x00, 0x04, 0x80, 0x84, 0xff):
        v.append(('ht-%02x' % ht, 'BadHashType', setv(0, ht=ht)))
    v += [
        ('high-s', 'HighS', setv(0, post=lambda wt: [high_s(wt[0]), wt[1]])),
        ('der-pad', 'BadSigEncoding', setv(0, post=lambda wt: [der_pad_r(wt[0]), wt[1]])),
        ('empty-sig', 'BadSignature', setv(0, post=lambda wt: [b'', wt[1]])),
        ('sig-521', 'BadSigEncoding', setv(0, post=lambda wt: [b'\x30' * 521, wt[1]])),
        ('one-item', 'BadWitness', setv(0, post=lambda wt: wt[:1])),
        ('three-items', 'BadWitness', setv(0, post=lambda wt: wt + [b''])),
        ('other-key', 'KeyHashMismatch', setv(0, post=lambda wt: [wt[0], K['other'].pub])),
        ('uncompressed-key', 'BadPubKey', setv(0, pubitem=unc)),
        ('hybrid-prefix-04-33', 'BadPubKey', setv(0, pubitem=b'\x04' + W.pub[1:])),
        ('hybrid-prefix-05-33', 'BadPubKey', setv(0, pubitem=b'\x05' + W.pub[1:])),
        ('prefix-parity-flipped', 'BadSignature', setv(0, pubitem=bytes([W.pub[0] ^ 1]) + W.pub[1:])),
        ('offcurve-key', 'BadSignature', setv(0, pubitem=offcurve_key())),
        ('amount-not-signed', 'BadSignature', sett(pv_post=lambda p: [dict(p[0], amount=p[0]['amount'] - 1)])),
        ('fee-zero', True, sett(outs=[(100000, OUT1[1])])),
        ('outputs-exceed', 'OutputsExceedInputs', sett(outs=[(100001, OUT1[1])])),
        ('zero-value-output', True, sett(outs=[OUT1, (0, OUT2[1])])),
        ('op_return-only', True, sett(outs=[(0, b'\x6a')])),
        ('exit-output', True, sett(outs=[(5000, b'\x60\x14' + b'\xab' * 20), (90000, OUT1[1])])),
        ('no-outputs', 'NoOutputs', sett(outs=[])),
        ('scriptsig', 'ScriptSigNotEmpty', setv(0, script_sig=b'\x51')),
        ('~outpoint-zero-vout0', True, setv(0, outpoint=(b'\x00' * 32, 0))),
        ('~outpoint-ff-vout-ffffffff', True, setv(0, outpoint=(b'\xff' * 32, 0xffffffff))),
        ('outpoint-null', 'NullPrevout', setv(0, outpoint=(b'\x00' * 32, 0xffffffff))),
        ('same-block-spend', True, sett(), [(10, True)]),
        # MAX_MONEY
        ('maxmoney-in-out', True, both(setv(0, amount=MAXM), sett(outs=[(MAXM, OUT1[1])]))),
        ('maxmoney+1-in', 'InputsTooLarge', both(setv(0, amount=MAXM + 1), sett(outs=[OUT1]))),
        ('maxmoney+1-out', 'OutputTooLarge', both(setv(0, amount=MAXM + 1), sett(outs=[(MAXM + 1, OUT1[1])]))),
        ('u64max-in', 'InputsTooLarge', both(setv(0, amount=2**64 - 1), sett(outs=[OUT1]))),
        # finality (height locktimes)
        ('locktime-100', True, both(sett(locktime=100), seq(0, 0xfffffffe)),
         [(100, 'NonFinal'), (101, True), (102, True)]),
        ('locktime-100-final-seq', True, sett(locktime=100), [(10, True), (100, True)]),
        ('locktime-max-final-seq', True, sett(locktime=0xffffffff), [(10, True)]),
        ('locktime-499999999', 'NonFinal', both(sett(locktime=499999999), seq(0, 0)), [100000]),
        ('locktime-10-at-10', 'NonFinal', both(sett(locktime=10), seq(0, 0xfffffffe)), [10]),
        # finality (time locktimes, MTP(h) = T(max(h - 6, 0)))
        ('locktime-time-500000000', True, both(sett(locktime=500000000), seq(0, 0xfffffffe)), [(10, True)]),
        ('locktime-time-mtp50-1', True, both(sett(locktime=pyref.mtp(50) - 1), seq(0, 0xfffffffe)),
         [(49, 'NonFinal'), (50, True)]),
        ('locktime-time-mtp50', True, both(sett(locktime=pyref.mtp(50)), seq(0, 0xfffffffe)),
         [(50, 'NonFinal'), (51, True)]),
        ('locktime-time-genesis', True, both(sett(locktime=GEN), seq(0, 0xfffffffe)),
         [(10, True)]),
        ('locktime-time-genesis-1', True, both(sett(locktime=GEN - 1), seq(0, 0xfffffffe)),
         [(10, True)]),
        # BIP68 on a plain spend
        ('bip68-5', True, seq(0, 5), [(14, 'RelativeLockNotMet'), (15, True)]),
        ('bip68-5-bit16', True, seq(0, 5 | 0x10000), [(14, 'RelativeLockNotMet'), (15, True)]),
        ('bip68-ffff', True, seq(0, 0xffff), [(10 + 0xfffe, 'RelativeLockNotMet'), (10 + 0xffff, True)]),
        ('bip68-type', 'RelativeTimeLockUnsupported', seq(0, 5 | pyref.SEQ_TYPE)),
        ('bip68-type-v1', True, both(seq(0, 5 | pyref.SEQ_TYPE), sett(version=1)), [(10, True)]),
        ('bip68-type-disabled', True, seq(0, 5 | pyref.SEQ_TYPE | pyref.SEQ_DISABLE), [(10, True)]),
        ('bip68-v1', True, both(seq(0, 5), sett(version=1)), [(10, True)]),
        ('bip68-v0x80000000', True, both(seq(0, 5), sett(version=0x80000000)),
         [(14, 'RelativeLockNotMet'), (15, True)]),
    ]
    raw, pv = run(w, 'p2wpkh', base, v, rng)
    encoding_mutations(w, 'synth:p2wpkh', raw, pv, 1000, rng, big=True)

    # --- two inputs: SINGLE with no matching output, ACP, duplicates
    base = spec([inp('wpkh', k=W, ht=1), inp('wpkh', k=W2, ht=0x03)], [OUT1])
    v = [
        ('ecdsa-single-no-output-83', True, setv(1, ht=0x83)),
        ('ecdsa-single-both-inputs', True, setv(0, ht=0x83)),
        ('duplicate-outpoint', 'DuplicatePrevout', setv(1, outpoint=(H('outpoint/0'), 0))),
        ('maxmoney-each', 'InputsTooLarge', both(setv(0, amount=MAXM), setv(1, amount=MAXM))),
        ('maxmoney-split', True, both(setv(0, amount=MAXM - 1), setv(1, amount=1),
                                      sett(outs=[(MAXM, OUT1[1])]))),
        ('outputs-sum-over-maxmoney', 'OutputsTooLarge', both(setv(0, amount=MAXM - 1), setv(1, amount=1),
                                                             sett(outs=[(MAXM, OUT1[1]), (1, OUT2[1])]))),
        ('second-input-p2tr', True, setv(1, t='tr', k=T, ht=0)),
        ('second-input-p2tr-single-no-output', 'SighashSingleNoOutput', setv(1, t='tr', k=T, ht=0x03)),
        ('second-input-p2tr-acp-single-no-output', 'SighashSingleNoOutput', setv(1, t='tr', k=T, ht=0x83)),
        ('second-input-p2tr-none', True, setv(1, t='tr', k=T, ht=0x02)),
    ]
    run(w, 'two-in', base, v, rng, nrandom=6)

    # --- P2TR key path
    base = spec([inp('tr', k=T, ht=0)], [OUT1, OUT2])
    v = [('ht-%02x' % ht, True, setv(0, ht=ht)) for ht in (0x01, 0x02, 0x03, 0x81, 0x82, 0x83)]
    v += [('ht-%02x' % ht, 'BadHashType', setv(0, ht=ht)) for ht in (0x04, 0x80, 0x84, 0xff)]
    v += [
        ('65-bytes-ht-00', 'BadHashType', setv(0, force65=True)),
        ('sig-63', 'BadSigEncoding', setv(0, post=lambda wt: [wt[0][:63]])),
        ('sig-66', 'BadSigEncoding', setv(0, ht=1, post=lambda wt: [wt[0] + b'\x01'])),
        ('sig-flip', 'BadSignature', setv(0, post=lambda wt: [flip(wt[0], 40)])),
        ('sig-r-flip', 'BadSignature', setv(0, post=lambda wt: [flip(wt[0], 3)])),
        ('ht-changed-after-signing', 'BadSignature', setv(0, ht=1, post=lambda wt: [set_ht(wt[0], 0x81)])),
        ('ht-appended-after-signing', 'BadSignature', setv(0, post=lambda wt: [wt[0] + b'\x01'])),
        ('annex', 'TaprootAnnex', setv(0, post=lambda wt: wt + [b'\x50\x01'])),
        ('annex-empty', 'TaprootAnnex', setv(0, post=lambda wt: wt + [b'\x50'])),
        ('two-items', 'TaprootScriptPath', setv(0, post=lambda wt: wt + [b'\x51'])),
        ('two-items-empty', 'TaprootScriptPath', setv(0, post=lambda wt: wt + [b''])),
        ('empty-sig-item', 'BadSigEncoding', setv(0, post=lambda wt: [b''])),
        ('offcurve-output-key', 'BadSignature', setv(0, xonly=offcurve_key()[1:])),
        ('output-key-x-ge-p', 'BadSignature', setv(0, xonly=(pyref.SECP_P + 1).to_bytes(32, 'big'))),
        ('amount-not-signed', 'BadSignature', sett(pv_post=lambda p: [dict(p[0], amount=p[0]['amount'] + 1)])),
        ('prevout-height-not-signed', True, sett(pv_post=lambda p: [dict(p[0], height=1)])),
    ]
    raw, pv = run(w, 'p2tr', base, v, rng)
    encoding_mutations(w, 'synth:p2tr', raw, pv, 1000, rng)
    # sizes: pad an OP_RETURN so the whole tx is 100,000 or 100,001 bytes
    for target, exp in ((pyref.MAX_TX_SIZE, True), (pyref.MAX_TX_SIZE + 1, 'TxTooLarge')):
        pad = target - 200
        for _ in range(10):
            sp = clone(base)
            sp['outs'] = [OUT1, (0, b'\x6a' + b'\x00' * pad)]
            r, p = make(sp)
            if len(r) == target:
                break
            pad += target - len(r)
        w.case(r, p, 1000, 'synth:p2tr:size-%d@1000' % len(r), exp)
    # P2TR + P2WPKH: taproot commits to every prevout's amount and script
    base = spec([inp('tr', k=T, ht=0), inp('wpkh', k=W, ht=1)], [OUT1])
    v = [
        ('other-amount-not-signed', 'BadSignature', sett(pv_post=lambda p: [p[0], dict(p[1], amount=p[1]['amount'] + 1)])),
        ('tr-acp-other-amount', 'BadSignature', both(setv(0, ht=0x81),
                                                    sett(pv_post=lambda p: [p[0], dict(p[1], amount=p[1]['amount'] + 1)]))),
        ('tr-acp-wpkh-acp', True, both(setv(0, ht=0x81), setv(1, ht=0x81))),
        ('other-height-not-signed', True, sett(pv_post=lambda p: [p[0], dict(p[1], height=2)])),
        ('tr-empty-witness', 'BadWitness', setv(0, post=lambda wt: [])),
    ]
    run(w, 'p2tr+p2wpkh', base, v, rng, nrandom=6)

    # --- sigops (4 x legacy sigops in outputs + 4 x inputs <= 16000)
    base = spec([inp('tr', k=T, ht=0, amount=10**6)], [OUT1])
    sig_cases = [
        ('ae-199', True, b'\xae' * 199), ('ae-200', 'TooManySigops', b'\xae' * 200),
        ('af-200', 'TooManySigops', b'\xaf' * 200),
        ('ac-3999', True, b'\xac' * 3999), ('ac-4000', 'TooManySigops', b'\xac' * 4000),
        ('ad-3999', True, b'\xad' * 3999), ('ad-4000', 'TooManySigops', b'\xad' * 4000),
        ('op_return-ac-3999', True, b'\x6a' + b'\xac' * 3999),
        ('op_return-ac-4000', 'TooManySigops', b'\x6a' + b'\xac' * 4000),
        ('pushed-ac-5000', True, b'\x4d' + struct.pack('<H', 5000) + b'\xac' * 5000),
        ('pushdata1-ac', True, b'\xac' * 3990 + b'\x4c\xff' + b'\xac' * 255),
        ('truncated-push-stops-count', True, b'\xac' * 10 + b'\x4d\xff\xff' + b'\xac' * 5000),
        ('truncated-pushdata4-len', True, b'\xac' * 3999 + b'\x4e\x01'),
        ('ba-5000', True, b'\xba' * 5000),
        ('mixed-ae-ac', True, b'\xae' * 100 + b'\xac' * 1999),
        ('mixed-ae-ac+1', 'TooManySigops', b'\xae' * 100 + b'\xac' * 2000),
    ]
    v = [(lbl, exp, sett(outs=[OUT1, (0, s)])) for lbl, exp, s in sig_cases]
    v += [
        ('ae-199-outputs', True, sett(outs=[OUT1] + [(0, b'\xae')] * 198)),
        ('ae-200-outputs', 'TooManySigops', sett(outs=[OUT1] + [(0, b'\xae')] * 200)),
        ('two-inputs-ac-3998', True, both(sett(outs=[OUT1, (0, b'\xac' * 3998)]),
                                          lambda sp: sp['ins'].append(inp('wpkh', k=W, ht=1)))),
        ('two-inputs-ac-3999', 'TooManySigops', both(sett(outs=[OUT1, (0, b'\xac' * 3999)]),
                                                     lambda sp: sp['ins'].append(inp('wpkh', k=W, ht=1)))),
    ]
    run(w, 'sigops', base, v, rng, nrandom=0)

    # --- 2-of-2 (funding output; mutual close)
    a, b = (K['fund_a'], K['fund_b']) if K['fund_a'].pub < K['fund_b'].pub else (K['fund_b'], K['fund_a'])
    s2 = w_2of2(a, b)
    base = spec([inp('wsh', script=s2, items=[b'', ('sig', a, 1), ('sig', b, 1)])], [OUT1, OUT2])
    v = sig_variants(0, 1, a) + sig_variants(0, 2, b)
    v += [
        ('sigs-swapped', 'BadSignature', both(setitem(0, 1, ('sig', b, 1)), setitem(0, 2, ('sig', a, 1)))),
        ('dummy-00', 'NullDummy', setitem(0, 0, b'\x00')),
        ('dummy-01', 'NullDummy', setitem(0, 0, b'\x01')),
        ('no-dummy', 'StackUnderflow', setv(0, items=[('sig', a, 1), ('sig', b, 1)])),
        ('one-sig-empty', 'BadSignature', setitem(0, 1, b'')),
        ('both-sigs-empty', 'BadSignature', both(setitem(0, 1, b''), setitem(0, 2, b''))),
        ('same-sig-twice', 'BadSignature', setitem(0, 2, ('sig', a, 1))),
        ('extra-item', 'CleanStack', setv(0, items=[b'', b'', ('sig', a, 1), ('sig', b, 1)])),
        ('script-not-matching-program', 'WitnessScriptMismatch', setv(0, spk=b'\x00\x20' + sha256(b'x'))),
        ('unsorted-keys', 'UnknownScript', setv(0, script=w_2of2(b, a, sort=False), items=[b'', ('sig', b, 1), ('sig', a, 1)])),
        ('1-of-2', 'UnknownScript', setv(0, script=b'\x51' + s2[1:], items=[b'', ('sig', a, 1)])),
        ('2-of-3', 'UnknownScript', setv(0, script=b'\x52' + push(a.pub) + push(b.pub) + push(W.pub) + b'\x53\xae')),
        ('checkmultisigverify', 'UnknownScript', setv(0, script=s2[:-1] + b'\xaf')),
        ('uncompressed-key-slot', 'UnknownScript', setv(0, script=b'\x52\x41' + a.priv.public_key.format(False)
                                                        + push(b.pub) + b'\x52\xae')),
        ('seq-any', True, seq(0, 0)),
        ('version-1', True, sett(version=1)),
        ('key-slot-prefix-04', False, setv(0, script=b'\x52' + push(b'\x04' + a.pub[1:]) + push(b.pub) + b'\x52\xae')),
        ('key-slot-prefix-05', False, setv(0, script=b'\x52' + push(b'\x05' + a.pub[1:]) + push(b.pub) + b'\x52\xae')),
        ('key-slot-parity-flipped', False,
         setv(0, script=b'\x52' + push(bytes([a.pub[0] ^ 1]) + a.pub[1:]) + push(b.pub) + b'\x52\xae')),
    ]
    raw, pv = run(w, '2of2', base, v, rng)
    encoding_mutations(w, 'synth:2of2', raw, pv, 1000, rng)

    # --- to_local / HTLC-tx output, delayed path
    for d in (6, 144):
        sc = w_to_local(REV.pub, DEL.pub, d)
        base = spec([inp('wsh', script=sc, items=[('sig', DEL, 1), b''], seq=d)], [OUT1])
        e = 10 + d
        v = csv_variants(0, d, e + 1000) + sig_variants(0, 0, DEL)
        v += [
            ('selector-00', 'MinimalIf', setitem(0, 1, b'\x00')),
            ('selector-01-wrong-path', 'BadSignature', setitem(0, 1, b'\x01')),
            ('no-selector', 'MinimalIf', setv(0, items=[('sig', DEL, 1)])),
            ('empty-sig', 'BadSignature', setitem(0, 0, b'')),
            ('revocation-key-sig-on-delayed-path', 'BadSignature', setitem(0, 0, ('sig', REV, 1))),
            ('extra-item', 'CleanStack', setv(0, items=[b'', ('sig', DEL, 1), b''])),
            ('one-block-early-seq-ok', True, sett(), [(e - 1, 'RelativeLockNotMet'), (e, True)]),
        ]
        run(w, 'to_local-%d' % d, base, v, rng)
    # the CSV number's encodings (script.c's add_number) and edge values
    for d, exp in ((0, True), (1, True), (16, True), (17, True), (127, True), (128, True), (255, True),
                   (0x7fff, True), (0x8000, True), (0xffff, True)):
        sc = w_to_local(REV.pub, DEL.pub, d)
        sp = spec([inp('wsh', script=sc, items=[('sig', DEL, 1), b''], seq=d)], [OUT1])
        r, p = make(sp)
        sweep(w, 'synth:to_local-csv-%d' % d, r, p)
        if d >= 1:
            sp['ins'][0]['seq'] = d - 1
            r, p = make(sp)
            w.case(r, p, 10 + d + 10, 'synth:to_local-csv-%d:seq-n-1@%d' % (d, 20 + d), 'CsvNotMet')
    big = [
        # n >= 2^16 is never satisfied (draft 3; Bitcoin would mask n with type | 0xffff)
        ('csv-0x10000-seq-0', 0x10000, 0, None, 'CsvNotMet'),
        ('csv-0x10000-seq-ffff', 0x10000, 0xffff, None, 'CsvNotMet'),
        ('csv-0x10006-seq-6', 0x10006, 6, None, 'CsvNotMet'),
        ('csv-0x10006-seq-5', 0x10006, 5, None, 'CsvNotMet'),
        # type bit in n: a time lock, which BIP68 (rule 6) refuses in the sequence
        ('csv-type-bit-seq-blocks', 0x400006, 6, None, 'CsvNotMet'),
        ('csv-type-bit-seq-type', 0x400006, 0x400006, None, 'RelativeTimeLockUnsupported'),
        # disable bit in n (5-byte push): never satisfied (draft 3; a NOP in Bitcoin)
        ('csv-0x80000000-seq-final', 0x80000000, 0xffffffff, None, 'CsvNotMet'),
        ('csv-0x80000000-seq-0', 0x80000000, 0, None, 'CsvNotMet'),
        ('csv-0xffffffff', 0xffffffff, 0xffffffff, None, 'CsvNotMet'),
        ('csv-0x7fffffff', 0x7fffffff, 0xffff, None, 'CsvNotMet'),
        # encodings add_number never writes
        ('csv-6-nonminimal-push', 6, 6, b'\x01\x06', 'UnknownScript'),
        ('csv-6-two-bytes', 6, 6, b'\x02\x06\x00', 'UnknownScript'),
        ('csv-144-three-bytes', 144, 144, b'\x03\x90\x00\x00', 'UnknownScript'),
        ('csv-6-pushdata1', 6, 6, b'\x4c\x01\x06', 'UnknownScript'),
        ('csv-negative', 1, 1, b'\x01\x81', 'UnknownScript'),
        ('csv-1negate', 1, 1, b'\x4f', 'UnknownScript'),
        ('csv-6-bytes', 6, 6, b'\x06\x06\x00\x00\x00\x00\x00', 'UnknownScript'),
        ('csv-above-u32', 6, 6, b'\x05\x00\x00\x00\x00\x01', 'UnknownScript'),
    ]
    for lbl, n, sq, enc, exp in big:
        sc = w_to_local(REV.pub, DEL.pub, n, enc)
        sp = spec([inp('wsh', script=sc, items=[('sig', DEL, 1), b''], seq=sq)], [OUT1])
        r, p = make(sp)
        tag = ('ambiguous:' if lbl.startswith('~') else 'synth:') + 'to_local:' + lbl.lstrip('~')
        w.case(r, p, 10 + 0xffff + 10, tag + '@%d' % (10 + 0xffff + 10), exp)
        w.case(r, p, 11, tag + '@11', None)

    # --- to_local revocation path
    sc = w_to_local(REV.pub, DEL.pub, 144)
    base = spec([inp('wsh', script=sc, items=[('sig', REV, 1), b'\x01'])], [OUT1])
    v = [
        ('selector-02', 'MinimalIf', setitem(0, 1, b'\x02')),
        ('selector-0101', 'MinimalIf', setitem(0, 1, b'\x01\x01')),
        ('selector-81', 'MinimalIf', setitem(0, 1, b'\x81')),
        ('selector-empty-wrong-path', False, setitem(0, 1, b'')),
        ('delayed-key-sig', 'BadSignature', setitem(0, 0, ('sig', DEL, 1))),
        ('version-1-seq-0', True, both(sett(version=1), seq(0, 0))),
        ('seq-type-bit-v2', 'RelativeTimeLockUnsupported', seq(0, 5 | pyref.SEQ_TYPE)),
    ]
    run(w, 'to_local-revocation', base, v, rng)

    # --- to_remote (CSV 1)
    base = spec([inp('wsh', script=w_to_remote(RMT.pub), items=[('sig', RMT, 1)], seq=1)], [OUT1])
    v = csv_variants(0, 1, 1011) + sig_variants(0, 0, RMT)
    v += [
        ('seq-0', 'CsvNotMet', seq(0, 0)),
        ('empty-sig', 'BadSignature', setitem(0, 0, b'')),
        ('extra-item', 'CleanStack', setv(0, items=[b'', ('sig', RMT, 1)])),
        ('lease-csv-2', 'UnknownScript', setv(0, script=w_to_remote(RMT.pub, 2))),
        ('key-prefix-04', 'BadPubKey', setv(0, script=w_to_remote(b'\x04' + RMT.pub[1:]))),
        ('key-prefix-05', 'BadPubKey', setv(0, script=w_to_remote(b'\x05' + RMT.pub[1:]))),
        ('lease-csv-1000', 'UnknownScript', setv(0, script=w_to_remote(RMT.pub, 1000), seq=1000)),
        ('no-csv', 'UnknownScript', setv(0, script=push(RMT.pub) + b'\xac')),
    ]
    run(w, 'to_remote', base, v, rng)

    # --- anchors
    base = spec([inp('wsh', script=w_anchor(ANC.pub), items=[('sig', ANC, 1)], amount=330)], [(300, OUT1[1])])
    v = sig_variants(0, 0, ANC) + [
        ('version-1', True, sett(version=1)),
        ('same-block', True, sett(), [(10, True)]),
        ('extra-item', 'CleanStack', setv(0, items=[b'', ('sig', ANC, 1)])),
    ]
    run(w, 'anchor-sig', base, v, rng)
    base = spec([inp('wsh', script=w_anchor(ANC.pub), items=[b''], amount=330, seq=16)], [(300, OUT1[1])])
    v = csv_variants(0, 16, 1026) + [
        ('bad-nonempty-sig', 'BadSignature', setitem(0, 0, ('sig', K['other'], 1))),
        ('nonder-sig', 'BadSigEncoding', setitem(0, 0, b'\x01')),
        ('seq-15', 'CsvNotMet', seq(0, 15)),
        ('key-prefix-04', 'BadPubKey', setv(0, script=w_anchor(b'\x04' + ANC.pub[1:]))),
        ('key-prefix-05', 'BadPubKey', setv(0, script=w_anchor(b'\x05' + ANC.pub[1:]))),
        ('key-off-curve', 'BadPubKey', setv(0, script=w_anchor(offcurve_key()))),
        ('key-off-curve-with-sig', 'BadPubKey', setv(0, script=w_anchor(offcurve_key()),
                                                         items=[('sig', ANC, 1)], seq=0xffffffff)),
        ('no-items', 'StackUnderflow', setv(0, items=[])),
    ]
    run(w, 'anchor-nosig', base, v, rng)

    # --- offered HTLC
    cltv = 40
    so = w_offered(LH.pub, RH.pub, REV.pub, PHASH)
    base = spec([inp('wsh', script=so, items=[('sig', REV, 1), REV.pub])], [OUT1])
    v = sig_variants(0, 0, REV) + [
        ('wrong-revocation-key', False, setitem(0, 1, K['other'].pub)),
        ('revocation-key-uncompressed', False, setitem(0, 1, REV.priv.public_key.format(False))),
        ('seq-0-v1', True, both(seq(0, 0), sett(version=1))),
        ('empty-sig', 'BadSignature', setitem(0, 0, b'')),
    ]
    run(w, 'offered-revocation', base, v, rng)
    base = spec([inp('wsh', script=so, items=[('sig', RH, 1), PRE], seq=1)], [OUT1])
    v = csv_variants(0, 1, 1011) + sig_variants(0, 0, RH) + [
        ('wrong-preimage', 'BadPreimage', setitem(0, 1, H('wrong'))),
        ('preimage-31', False, setitem(0, 1, PRE[:31])),
        ('preimage-33', False, setitem(0, 1, PRE + b'\x00')),
        ('payment-hash-not-preimage', 'BadPreimage', setitem(0, 1, PHASH)),
        ('local-sig', 'BadSignature', setitem(0, 0, ('sig', LH, 1))),
        ('empty-sig', 'BadSignature', setitem(0, 0, b'')),
        ('items-swapped', False, setv(0, items=[PRE, ('sig', RH, 1)])),
    ]
    run(w, 'offered-remote-success', base, v, rng)
    base = spec([inp('wsh', script=so, items=[b'', ('sig', RH, 0x83), ('sig', LH, 1), b''], seq=1)],
                [OUT1], locktime=cltv)
    v = csv_variants(0, 1, 1041) + sig_variants(0, 1, RH, 0x83) + sig_variants(0, 2, LH) + [
        ('sigs-swapped', 'BadSignature', both(setitem(0, 1, ('sig', LH, 1)), setitem(0, 2, ('sig', RH, 0x83)))),
        ('dummy-00', 'NullDummy', setitem(0, 0, b'\x00')),
        ('junk-32', 'BadPreimage', setitem(0, 3, H('junk32'))),
        ('dummy-01', 'BadTimeoutDummy', setitem(0, 3, b'\x01')),
        ('dummy-00', 'BadTimeoutDummy', setitem(0, 3, b'\x00')),
        ('dummy-31', 'BadTimeoutDummy', setitem(0, 3, b'\x02' * 31)),
        ('dummy-33', 'BadTimeoutDummy', setitem(0, 3, b'\x02' * 33)),
        ('dummy-520', 'BadTimeoutDummy', setitem(0, 3, b'\x02' * 520)),
        ('dummy-521', 'BadTimeoutDummy', setitem(0, 3, b'\x02' * 521)),
        ('remote-sig-empty', 'BadSignature', setitem(0, 1, b'')),
        ('locktime-0', True, sett(locktime=0)),
        ('no-locktime-check-in-script', True, sett(locktime=5)),
        # anchors HTLC tx: the HTLC input SINGLE|ACP at index 1, no output 1
        ('htlc-at-index1-single-no-output', True,
         lambda sp: sp['ins'].insert(0, inp('tr', k=T, ht=0))),
        ('with-wallet-input', True,
         both(lambda sp: sp['ins'].append(inp('tr', k=T, ht=0)), sett(outs=[OUT1, OUT2]))),
    ]
    run(w, 'offered-local-timeout', base, v, rng)

    # --- received HTLC
    sr = w_received(LH.pub, RH.pub, REV.pub, PHASH, cltv)
    base = spec([inp('wsh', script=sr, items=[('sig', REV, 1), REV.pub])], [OUT1])
    v = [('wrong-revocation-key', False, setitem(0, 1, K['other'].pub)),
         ('sig-ht-83', True, setitem(0, 0, ('sig', REV, 0x83))),
         ('rev-sig-by-remote', 'BadSignature', setitem(0, 0, ('sig', RH, 1)))]
    run(w, 'received-revocation', base, v, rng)
    base = spec([inp('wsh', script=sr, items=[b'', ('sig', RH, 0x83), ('sig', LH, 1), PRE], seq=1)], [OUT1])
    v = csv_variants(0, 1, 1011) + sig_variants(0, 1, RH, 0x83) + sig_variants(0, 2, LH) + [
        ('wrong-preimage', 'BadPreimage', setitem(0, 3, H('wrong'))),
        ('preimage-31', False, setitem(0, 3, PRE[:31])),
        ('dummy-01', 'NullDummy', setitem(0, 0, b'\x01')),
        ('sigs-swapped', 'BadSignature', both(setitem(0, 1, ('sig', LH, 1)), setitem(0, 2, ('sig', RH, 0x83)))),
        ('locktime-ignored', True, both(sett(locktime=5), seq(0, 1))),
    ]
    run(w, 'received-local-success', base, v, rng)
    base = spec([inp('wsh', script=sr, items=[('sig', RH, 1), b''], seq=1)], [OUT1], locktime=cltv)
    hi = cltv + 1000
    v = [x if x[0] != 'seq-final' else ('seq-final', 'CltvNotMet', x[2]) for x in csv_variants(0, 1, hi)]
    v += sig_variants(0, 0, RH) + [
        ('locktime-cltv-1', 'CltvNotMet', sett(locktime=cltv - 1)),
        ('locktime-cltv+1', True, sett(locktime=cltv + 1)),
        ('locktime-time-type', 'CltvNotMet', sett(locktime=500000000)),
        ('seq-0', 'CsvNotMet', seq(0, 0)),
        ('seq-fffffffe', 'CsvNotMet', seq(0, 0xfffffffe)),
        ('dummy-01', 'BadTimeoutDummy', setitem(0, 1, b'\x01')),
        ('dummy-00', 'BadTimeoutDummy', setitem(0, 1, b'\x00')),
        ('dummy-33', 'BadTimeoutDummy', setitem(0, 1, b'\x02' * 33)),
        ('junk-revocation-pubkey', 'BadSignature', setitem(0, 1, REV.pub)),
        ('junk-32', False, setitem(0, 1, H('junk'))),
        ('empty-sig', 'BadSignature', setitem(0, 0, b'')),
        ('local-sig', 'BadSignature', setitem(0, 0, ('sig', LH, 1))),
        ('one-block-early', True, sett(), [(cltv, 'NonFinal'), (cltv + 1, True)]),
    ]
    run(w, 'received-remote-timeout', base, v, rng)
    # CLTV numbers: every add_number width, and time-based expiries
    for c in (0, 1, 16, 17, 127, 128, 0x7fff, 0x8000, 0x7fffff, 0x800000, 499999999):
        sp = spec([inp('wsh', script=w_received(LH.pub, RH.pub, REV.pub, PHASH, c),
                       items=[('sig', RH, 1), b''], seq=1)], [OUT1], locktime=c)
        r, p = make(sp)
        sweep(w, 'synth:received-remote-timeout-cltv-%d' % c, r, p) if c < 10**6 else \
            w.case(r, p, 100, 'synth:received-remote-timeout-cltv-%d@100' % c, 'NonFinal')
        if c >= 1:
            sp['locktime'] = c - 1
            r, p = make(sp)
            w.case(r, p, max(c, 11) + 1 if c < 10**6 else 100, 'synth:received-remote-timeout-cltv-%d:locktime-1' % c,
                   'CltvNotMet' if c < 10**6 else 'NonFinal')
    for lbl, c, lt, hs in (
            ('time-cltv-T100', pyref.time_of(100), pyref.time_of(100), [(106, 'NonFinal'), (107, True)]),
            ('time-cltv-T100-lt-below', pyref.time_of(100), pyref.time_of(100) - 1, [(200, 'CltvNotMet')]),
            ('time-cltv-500000000', 500000000, 500000000, [(11, True)]),
            ('time-cltv-0x80000000', 0x80000000, 0x80000000, [(11, 'NonFinal')]),
            ('time-cltv-height-locktime', pyref.time_of(100), 100, [(200, 'CltvNotMet')]),
            ('height-cltv-time-locktime', 100, pyref.time_of(1), [(200, 'CltvNotMet')]),
            ('cltv-u32-max', 0xffffffff, 0xffffffff, [(11, 'NonFinal')])):
        sp = spec([inp('wsh', script=w_received(LH.pub, RH.pub, REV.pub, PHASH, c),
                       items=[('sig', RH, 1), b''], seq=1)], [OUT1], locktime=lt)
        r, p = make(sp)
        for h, exp in hs:
            w.case(r, p, h, 'synth:received-remote-timeout:%s@%d' % (lbl, h), exp)
    # early-chain MTP: the SPEC's T(max(h - 6, 0)), not Bitcoin's median of h blocks
    for k in (1, 2, 3, 5):
        sp = spec([inp('wpkh', k=W, ht=1, height=1, seq=0xfffffffe)], [OUT1], locktime=pyref.time_of(k) - 1)
        r, p = make(sp)
        for h in range(max(1, k + 4), k + 8):
            w.case(r, p, h, 'synth:mtp-early:locktime-T%d-1@%d' % (k, h), True if h >= k + 6 else 'NonFinal')

    # --- everything in one tx: wallet, taproot, to_local, anchor, HTLC
    base = spec([inp('wpkh', k=W, ht=0x81),
                 inp('tr', k=T, ht=0x83),
                 inp('wsh', script=w_to_local(REV.pub, DEL.pub, 6), items=[('sig', DEL, 1), b''], seq=6),
                 inp('wsh', script=w_anchor(ANC.pub), items=[b''], amount=330, seq=16),
                 inp('wsh', script=so, items=[b'', ('sig', RH, 0x83), ('sig', LH, 0x01), b''], seq=1)],
                [OUT1, OUT2, (1000, b'\x00\x20' + sha256(b'x'))], locktime=cltv)
    v = [
        ('anchor-seq-15', 'CsvNotMet', seq(3, 15)),
        ('to_local-seq-5', 'CsvNotMet', seq(2, 5)),
        ('tr-single-no-output', 'SighashSingleNoOutput', both(setv(1, ht=0x03), sett(outs=[OUT1]))),
        ('drop-outputs-to-1', 'SighashSingleNoOutput', sett(outs=[OUT1])),
        ('drop-outputs-to-2', True, sett(outs=[OUT1, OUT2])),
        ('version-1', 'CsvNotMet', sett(version=1)),
        ('locktime-time', True, sett(locktime=500000001)),
    ]
    run(w, 'mixed-5-in', base, v, rng)


RANDOM_KINDS = ('wpkh', 'tr', '2of2', 'to_local', 'to_local_rev', 'to_remote', 'anchor', 'anchor_nosig',
                'off_rev', 'off_success', 'off_timeout', 'rcv_rev', 'rcv_success', 'rcv_timeout')
CSV_KINDS = {'to_local', 'to_remote', 'anchor_nosig', 'off_success', 'off_timeout', 'rcv_success', 'rcv_timeout'}


def random_synth(w, rng, count):
    """Random valid compositions of every input kind, hash type, sequence
    and locktime, swept around their earliest height and mutated."""
    REV, DEL, RMT, LH, RH, ANC = K['rev'], K['delayed'], K['remote'], K['local_htlc'], K['remote_htlc'], K['anchor']
    a, b = (K['fund_a'], K['fund_b']) if K['fund_a'].pub < K['fund_b'].pub else (K['fund_b'], K['fund_a'])
    so = w_offered(LH.pub, RH.pub, REV.pub, PHASH)
    for n in range(count):
        def ht():
            return rng.choice(pyref.ECDSA_HASHTYPES)

        def rseq():
            return rng.choice((0xffffffff, 0xfffffffe, 0xfffffffd, 0, rng.randint(0, 40),
                               rng.randint(0, 40) | pyref.SEQ_DISABLE, rng.randint(0, 40) | 0x10000))

        def junk():
            return b''          # draft 3: the timeout dummy is exactly <>
        ins, cltvs, kinds = [], [], []
        for j in range(rng.randint(1, 4)):
            kind = rng.choice(RANDOM_KINDS)
            kinds.append(kind)
            c = dict(amount=rng.randint(330, 2000000), height=rng.randint(1, 300))
            if kind == 'wpkh':
                d = inp('wpkh', k=K['wallet'], ht=ht(), seq=rseq(), **c)
            elif kind == 'tr':
                d = inp('tr', k=K['tr'], ht=rng.choice(pyref.TAPROOT_HASHTYPES), seq=rseq(), **c)
            elif kind == '2of2':
                d = inp('wsh', script=w_2of2(a, b), items=[b'', ('sig', a, ht()), ('sig', b, ht())], seq=rseq(), **c)
            elif kind == 'to_local':
                dl = rng.choice((0, 1, 6, 16, 17, 144, 1000))
                d = inp('wsh', script=w_to_local(REV.pub, DEL.pub, dl), items=[('sig', DEL, ht()), b''],
                        seq=dl + rng.choice((0, 0, 1, 5)) | rng.choice((0, 0, 0x10000)), **c)
            elif kind == 'to_local_rev':
                d = inp('wsh', script=w_to_local(REV.pub, DEL.pub, 144), items=[('sig', REV, ht()), b'\x01'],
                        seq=rseq(), **c)
            elif kind == 'to_remote':
                d = inp('wsh', script=w_to_remote(RMT.pub), items=[('sig', RMT, ht())], seq=rng.randint(1, 3), **c)
            elif kind == 'anchor':
                d = inp('wsh', script=w_anchor(ANC.pub), items=[('sig', ANC, ht())], seq=rseq(), **dict(c, amount=330))
            elif kind == 'anchor_nosig':
                d = inp('wsh', script=w_anchor(ANC.pub), items=[b''], seq=16 + rng.choice((0, 1, 10)),
                        **dict(c, amount=330))
            elif kind == 'off_rev':
                d = inp('wsh', script=so, items=[('sig', REV, ht()), REV.pub], seq=rseq(), **c)
            elif kind == 'off_success':
                d = inp('wsh', script=so, items=[('sig', RH, ht()), PRE], seq=rng.randint(1, 3), **c)
            elif kind == 'off_timeout':
                d = inp('wsh', script=so, items=[b'', ('sig', RH, ht()), ('sig', LH, ht()), junk()],
                        seq=rng.randint(1, 3), **c)
            else:
                cl = rng.randint(0, 400)
                sr = w_received(LH.pub, RH.pub, REV.pub, PHASH, cl)
                if kind == 'rcv_rev':
                    d = inp('wsh', script=sr, items=[('sig', REV, ht()), REV.pub], seq=rseq(), **c)
                elif kind == 'rcv_success':
                    d = inp('wsh', script=sr, items=[b'', ('sig', RH, ht()), ('sig', LH, ht()), PRE],
                            seq=rng.randint(1, 3), **c)
                else:
                    d = inp('wsh', script=sr, items=[('sig', RH, ht()), junk()], seq=rng.randint(1, 3), **c)
                    cltvs.append(cl)
            d['outpoint'] = (H('rand/%d/%d' % (n, j)), rng.randint(0, 3))
            ins.append(d)
        total = sum(d['amount'] for d in ins)
        fee = rng.randint(0, min(total, 5000))
        nout = rng.randint(1, 4)
        cuts = sorted(rng.randint(0, total - fee) for _ in range(nout - 1))
        amounts = [y - x for x, y in zip([0] + cuts, cuts + [total - fee])]
        scripts = (OUT1[1], OUT2[1], b'\x00\x20' + sha256(b'out'), b'\x60\x14' + b'\xee' * 20)
        outs = [(amt, rng.choice(scripts)) for amt in amounts]
        if rng.random() < 0.15:
            outs.append((0, b'\x6a' + bytes(rng.randint(0, 40))))
        if cltvs:
            lt = max(cltvs) + rng.choice((0, 0, 1, 7))
        else:
            lt = rng.choice((0, 0, rng.randint(1, 400), pyref.mtp(rng.randint(1, 400)) - rng.randint(1, 600)))
        version = rng.choice((2,) * 8 + (3, 0xffffffff))
        if not CSV_KINDS & set(kinds) and rng.random() < 0.3:
            version = rng.choice((0, 1))
        sp = spec(ins, outs, version=version, locktime=lt)
        raw, pv = make(sp)
        bad_tr = any(d['t'] == 'tr' and d['ht'] & 3 == 3 and j >= len(outs) for j, d in enumerate(ins))
        e = earliest(raw, pv)
        o = 'synth:random-%d:%s' % (n, '+'.join(kinds))
        lo = max(p['height'] for p in pv)
        if e > lo:
            w.case(raw, pv, e - 1, o + '@E-1', 'timelock' if not bad_tr else False)
        for dh in (0, 1, 50):
            w.case(raw, pv, e + dh, o + '@E+%d' % dh, 'SighashSingleNoOutput' if bad_tr else True)
        random_mutations(w, o, raw, pv, e + 1, 3, rng)


def gen_deposits(path):
    n = 0
    with open(path, 'w') as f:
        spks = [('p2wpkh', '0014' + '11' * 20), ('p2tr', '5120' + '22' * 32), ('p2wsh', '0020' + '33' * 32),
                ('exit', '6014' + '44' * 20), ('exit-like-19', '6013' + '44' * 19), ('exit-like-21', '6015' + '44' * 21),
                ('v16-32', '6020' + '55' * 32), ('op_return', '6a'), ('op_return-data', '6a0401020304'),
                ('empty', ''), ('10000-bytes', '51' * 10000), ('10001-bytes', '51' * 10001),
                ('starts-6a-long', '6a' + '00' * 50), ('p2pkh', '76a914' + '66' * 20 + '88ac')]
        values = [0, 1, 10**10 - 1, 10**10, 10**10 + 1, 10**18, MAXM * 10**10, (MAXM + 1) * 10**10]
        for chain_id, contract, nonce in ((31337, '0x5fbdb2315678afecb367f032d93f642f64180aa3', 0),
                                          (31337, '0x5fbdb2315678afecb367f032d93f642f64180aa3', 1),
                                          (84532, '0x' + 'ab' * 20, 255), (84532, '0x' + 'ab' * 20, 2**64)):
            for vn, (name, s) in enumerate(spks):
                for value in (values if vn < 2 else [10**10 * 1000]):
                    for height in ((0, 1) if value == 10**10 * 1000 and vn == 0 else (1,)):
                        ok, err = pyref.check_deposit(value, s, height)
                        amount = value // 10**10
                        raw, txid = pyref.deposit_tx(chain_id, contract, nonce, amount, s)
                        f.write(json.dumps(dict(
                            chain_id=chain_id, contract=contract, nonce=nonce, value_wei=value, script=s,
                            height=height, valid=ok, error=err, raw_tx=raw.hex(), txid=txid,
                            origin='deposit:%s:%d:%s' % (name, value, nonce))) + '\n')
                        n += 1
    print('%-22s %5d cases' % (os.path.relpath(path, HERE), n))


def main():
    rng = random.Random(SEED)
    os.makedirs(os.path.join(HERE, 'cases'), exist_ok=True)
    writers = []
    w = Writer('cln.jsonl')
    writers.append(w)
    idx = 0
    kinds = set()
    for d in load_cln():
        if any(isinstance(p, dict) and p.get('mint') for p in d['prevouts']):
            continue
        # every mutation for the first tx of each kind (unlabelled ones by
        # their input scripts), the height sweeps and a few for the rest
        kind = d.get('kind') or ','.join(p['script'][:4] for p in d['prevouts'])
        cln_mutations(w, d, idx, rng, full=kind not in kinds)
        kinds.add(kind)
        idx += 1
    w.close()
    w = Writer('synth.jsonl')
    writers.append(w)
    gen_synth(w, rng)
    random_synth(w, rng, 300)
    w.close()
    gen_deposits(os.path.join(HERE, 'cases', 'deposits.jsonl'))
    total_v = sum(x.valid for x in writers)
    total_i = sum(x.invalid for x in writers)
    print('total tx cases: %d (%d valid, %d invalid)' % (total_v + total_i, total_v, total_i))
    bad = [m for x in writers for m in x.mismatches]
    for m in bad:
        print('EXPECTATION FAILED', m)
    if bad:
        sys.exit(1)


if __name__ == '__main__':
    main()
