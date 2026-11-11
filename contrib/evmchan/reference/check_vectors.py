#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Checks pyref against the real CLN transactions of spike S1, the official
BIP340 and BIP341 vectors, and known answers for its hashes.  Exits non-zero
on any failure.  With --cases, also re-checks every case file under cases/."""
import csv
import glob
import json
import os
import sys

import pyref
from pyref import Invalid

HERE = os.path.dirname(os.path.abspath(__file__))
CLN = os.path.join(HERE, '..', 'tests', 'vectors')
TIMELOCK_ERRORS = {'NonFinal', 'RelativeLockNotMet', 'CsvNotMet', 'CltvNotMet'}

failures = []


def check(cond, what):
    if not cond:
        failures.append(what)
        print('FAIL', what)


# ---------------------------------------------------------------- hashes
def check_hashes():
    check(pyref.keccak256(b'').hex() ==
          'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470', 'keccak("")')
    check(pyref.keccak256(b'abc').hex() ==
          '4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45', 'keccak("abc")')
    # multi-block: 200 bytes crosses the 136-byte rate
    check(pyref.keccak256(b'a' * 200) == pyref.keccak256(b'a' * 200), 'keccak determinism')
    for m in (b'', b'abc', b'message digest', b'a' * 1000):
        check(pyref.ripemd160_py(m) == pyref.ripemd160(m), 'ripemd160_py(%d bytes)' % len(m))
    check(pyref.ripemd160_py(b'').hex() == '9c1185a5c5e9fc54612808977ee8f548b2258d31', 'rmd("")')
    print('hashes: ok' if not failures else 'hashes: FAIL')


# ---------------------------------------------------------------- CLN vectors
def analytic_earliest(tx, prevouts, g, iv):
    """The earliest valid height from the tx's fields alone (5, 6, CSV)."""
    e = max([1] + [p['height'] for p in prevouts])
    if not all(x.sequence == pyref.SEQUENCE_FINAL for x in tx.inputs):
        if tx.locktime < pyref.LOCKTIME_THRESHOLD:
            e = max(e, tx.locktime + 1)
        else:
            h = e
            while not tx.locktime < pyref.mtp(h, g, iv):
                h += 1
            e = h
    if tx.version >= 2:
        for x, p in zip(tx.inputs, prevouts):
            if not x.sequence & pyref.SEQ_DISABLE:
                e = max(e, p['height'] + (x.sequence & pyref.SEQ_MASK))
    return e


def load_cln():
    out = []
    for path in sorted(glob.glob(os.path.join(CLN, '*.jsonl'))):
        for n, line in enumerate(open(path)):
            if line.strip():
                d = json.loads(line)
                d['_where'] = '%s:%d' % (os.path.basename(path), n + 1)
                out.append(d)
    return out


def check_cln():
    g, iv = pyref.GENESIS_TIME, pyref.INTERVAL
    vecs = load_cln()
    chains = {}     # one chain per vector file
    n_valid = n_lock = 0
    for d in vecs:
        chain = chains.setdefault(d['_where'].split(':')[0], pyref.Chain(g, iv))
        tx = pyref.parse_tx(bytes.fromhex(d['tx']))
        where = '%s (h %d, %s)' % (d['_where'], d['height'], d.get('kind', '-'))
        if any(isinstance(p, dict) and p.get('mint') for p in d['prevouts']):
            # A toy-chain deposit: it must have 3.3's shape (only the fake
            # prevout's derivation differs), and its output enters the chain.
            check(tx.version == 2 and len(tx.inputs) == 1 and tx.inputs[0].vout == 0
                  and tx.inputs[0].script_sig == b'' and tx.inputs[0].sequence == 0xffffffff
                  and len(tx.outputs) == 1 and tx.locktime == 0 and not any(tx.witnesses),
                  where + ': deposit shape')
            check(tx.txid() == d['txid'], where + ': deposit txid')
            chain.add_utxo(d['txid'], 0, tx.outputs[0][0], tx.outputs[0][1].hex(), d['height'])
            continue
        ok, err, txid, fee = pyref.validate(d['tx'], d['prevouts'], d['height'], g, iv)
        check(ok, '%s: invalid at its height: %s' % (where, err))
        check(txid == d['txid'], where + ': txid')
        n_valid += ok
        # earliest height, by search and from the fields
        lo = max(p['height'] for p in d['prevouts'])
        e = next(h for h in range(lo, d['height'] + 1) if pyref.validate(d['tx'], d['prevouts'], h, g, iv)[0])
        check(e == analytic_earliest(tx, d['prevouts'], g, iv), where + ': earliest height')
        if e > max(1, lo):
            n_lock += 1
            ok1, err1, _, _ = pyref.validate(d['tx'], d['prevouts'], e - 1, g, iv)
            check(not ok1 and err1 in TIMELOCK_ERRORS, '%s: one block early gives %s' % (where, err1))
        for h in (e, e + 1, d['height'] + 1000):
            check(pyref.validate(d['tx'], d['prevouts'], h, g, iv)[0], '%s: valid at %d' % (where, h))
        # replay on the stateful chain: prevouts must be what the chain holds
        try:
            pv = chain.prevouts_for(d['tx'])
            check(pv == d['prevouts'], where + ': prevouts differ from the chain state')
            chain.submit(d['tx'], d['height'])
        except Invalid as e2:
            check(False, '%s: chain replay: %s' % (where, e2.name))
        print('  %-28s %-8s fee %-5d earliest %-4d %s' % (where, 'ok' if ok else err, fee or 0, e,
                                                         'timelocked' if e > max(1, lo) else ''))
    # 4.4: the exit output never became a UTXO, and was credited
    for name, chain in chains.items():
        check(sum(chain.withdrawable.values()) > 0, name + ': exit/fees credited')
    print('CLN: %d txs valid, %d timelocked and failing one block early' % (n_valid, n_lock))


# ---------------------------------------------------------------- BIP341
def check_bip341():
    d = json.load(open(os.path.join(HERE, 'vectors', 'bip341-wallet-test-vectors.json')))
    n = 0
    for kp in d['keyPathSpending']:
        tx = pyref.parse_tx(bytes.fromhex(kp['given']['rawUnsignedTx']))
        pv = [(u['amountSats'], bytes.fromhex(u['scriptPubKey']), 1) for u in kp['given']['utxosSpent']]
        signed = pyref.parse_tx(bytes.fromhex(kp['auxiliary']['fullySignedTx']))
        sh = pyref._Sighash(tx, pv)
        for sp in kp['inputSpending']:
            i, ht = sp['given']['txinIndex'], sp['given']['hashType']
            msg = sh.bip341_msg(i, ht)
            check(msg is not None and msg.hex() == sp['intermediary']['sigMsg'], 'bip341 sigMsg %d' % i)
            check(sh.bip341(i, ht).hex() == sp['intermediary']['sigHash'], 'bip341 sigHash %d' % i)
            wit = [bytes.fromhex(w) for w in sp['expected']['witness']]
            check(signed.witnesses[i] == wit, 'bip341 fullySignedTx witness %d' % i)
            tx.witnesses[i] = wit
            try:
                pyref.check_input(tx, pv, i, pyref._Sighash(tx, pv))
                n += 1
            except Invalid as e:
                check(False, 'bip341 input %d: %s' % (i, e.name))
            bad = bytearray(wit[0])
            bad[5] ^= 1
            tx.witnesses[i] = [bytes(bad)]
            try:
                pyref.check_input(tx, pv, i, pyref._Sighash(tx, pv))
                check(False, 'bip341 input %d: flipped sig accepted' % i)
            except Invalid:
                pass
            tx.witnesses[i] = wit
        # The other inputs: one P2PKH (out of scope) and one P2WPKH, whose
        # BIP143 signature makes an official check of that path too.
        for i, (x, w) in enumerate(zip(signed.inputs, signed.witnesses)):
            if pv[i][1][:2] == b'\x51\x20':
                continue
            tx.inputs[i].script_sig, tx.witnesses[i] = x.script_sig, w
            if pv[i][1][:2] == b'\x00\x14':
                try:
                    pyref.check_input(tx, pv, i, pyref._Sighash(tx, pv))
                    n += 1
                except Invalid as e:
                    check(False, 'bip341 P2WPKH input %d: %s' % (i, e.name))
        check(tx.serialize() == signed.serialize(), 'bip341 fullySignedTx')
    print('BIP341 keyPathSpending: %d inputs verified (incl. its P2WPKH one), sigMsg/sigHash match' % n)


# ---------------------------------------------------------------- BIP340
P, N = pyref.SECP_P, pyref.SECP_N
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return None
    if p1 == p2:
        lam = 3 * p1[0] * p1[0] * pow(2 * p1[1], P - 2, P) % P
    else:
        lam = (p2[1] - p1[1]) * pow(p2[0] - p1[0], P - 2, P) % P
    x = (lam * lam - p1[0] - p2[0]) % P
    return x, (lam * (p1[0] - x) - p1[1]) % P


def _mul(pt, k):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def bip340_verify_py(pk, msg, sig):
    """BIP340's Verify, from the BIP's text (an independent cross-check)."""
    x = int.from_bytes(pk, 'big')
    if x >= P:
        return False
    c = (pow(x, 3, P) + 7) % P
    y = pow(c, (P + 1) // 4, P)
    if y * y % P != c:
        return False
    Pt = (x, y if y % 2 == 0 else P - y)
    r, s = int.from_bytes(sig[:32], 'big'), int.from_bytes(sig[32:], 'big')
    if r >= P or s >= N:
        return False
    e = int.from_bytes(pyref.tagged_hash('BIP0340/challenge', sig[:32] + pk + msg), 'big') % N
    R = _add(_mul(G, s), _mul(Pt, N - e))
    return R is not None and R[1] % 2 == 0 and R[0] == r


def check_bip340():
    n = 0
    with open(os.path.join(HERE, 'vectors', 'bip340-test-vectors.csv')) as f:
        for row in csv.DictReader(f):
            pk, msg, sig = (bytes.fromhex(row[k]) for k in ('public key', 'message', 'signature'))
            want = row['verification result'] == 'TRUE'
            check(pyref.schnorr_verify(pk, msg, sig) == want, 'bip340 #%s (coincurve)' % row['index'])
            check(bip340_verify_py(pk, msg, sig) == want, 'bip340 #%s (python)' % row['index'])
            n += 1
    print('BIP340: %d vectors agree (coincurve and pure Python)' % n)


# ---------------------------------------------------------------- deposits
def check_deposit():
    raw, txid = pyref.deposit_tx(31337, '0x' + '11' * 20, 0, 1000, '0014' + '22' * 20)
    tx = pyref.parse_tx(raw)
    pre = pyref.keccak256(b'evmchan deposit' + (31337).to_bytes(32, 'big') + b'\x11' * 20 + bytes(32))
    check(tx.inputs[0].prev_txid == pre, 'deposit prevout is the keccak, unreversed')
    check(raw[5:37] == pre and raw[:4] == b'\x02\x00\x00\x00', 'deposit bytes')
    check(txid == pyref.dsha(raw)[::-1].hex(), 'deposit txid')
    check(pyref.check_deposit(10**10, '51', 1) == (True, None), 'deposit 1 sat')
    check(pyref.check_deposit(10**10 + 1, '51', 1)[1] == 'ValueNotWholeSats', 'deposit wei')
    check(pyref.check_deposit(10**10, '6014' + '00' * 20, 1)[1] == 'ExitScript', 'deposit exit')
    print('deposit tx: ok')


def check_cases():
    n = bad = 0
    for path in sorted(glob.glob(os.path.join(HERE, 'cases', '*.jsonl'))):
        for line in open(path):
            c = json.loads(line)
            if 'tx' not in c:
                continue
            ok, err, _, _ = pyref.validate(c['tx'], c['prevouts'], c['height'], c['genesis_time'], c['interval'])
            n += 1
            if ok != c['valid'] or err != c['error']:
                bad += 1
                check(False, 'case %s: now %s %s' % (c['origin'], ok, err))
    print('cases: %d re-checked, %d differ' % (n, bad))


if __name__ == '__main__':
    check_hashes()
    check_cln()
    check_bip341()
    check_bip340()
    check_deposit()
    if '--cases' in sys.argv:
        check_cases()
    print('FAILED: %d' % len(failures) if failures else 'all ok')
    sys.exit(1 if failures else 0)
