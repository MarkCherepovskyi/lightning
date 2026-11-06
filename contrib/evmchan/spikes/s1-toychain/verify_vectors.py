#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Decode the vectors run_vectors.py captured and check that each tx's inputs
spend the script its "kind" claims (BOLT 3, anchors), by the path it claims.

  python3 verify_vectors.py [vectors/cln-regtest-txs-full.jsonl]

For each input: the prevout's scriptPubKey type, and for P2WSH the witness
script (checked against the program) matched to a BOLT 3 template, and the
path the witness stack takes.  Prints a table kind -> inputs, and exits 1 if a
kind's inputs don't match what it should spend."""
from btc import Tx

import collections
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OPS = {0x00: '0', 0x4f: '-1', 0x63: 'IF', 0x64: 'NOTIF', 0x67: 'ELSE', 0x68: 'ENDIF',
       0x73: 'IFDUP', 0x75: 'DROP', 0x76: 'DUP', 0x7c: 'SWAP', 0x82: 'SIZE',
       0x87: 'EQUAL', 0x88: 'EQUALVERIFY', 0xa9: 'HASH160', 0xac: 'CHECKSIG',
       0xad: 'CHECKSIGVERIFY', 0xae: 'CHECKMULTISIG', 0xb1: 'CLTV', 0xb2: 'CSV'}
OPS.update({0x50 + n: str(n) for n in range(1, 17)})

# BOLT 3 witness scripts (option_anchors), as token templates: <33> a pubkey,
# <20> a hash160, <n> a pushed number.
TEMPLATES = {
    'funding-2of2': '2 <33> <33> 2 CHECKMULTISIG',
    'to_local': 'IF <33> ELSE <n> CSV DROP <33> ENDIF CHECKSIG',
    'to_remote': '<33> CHECKSIGVERIFY 1 CSV',
    'anchor': '<33> CHECKSIG IFDUP NOTIF 16 CSV ENDIF',
    'offered-htlc': 'DUP HASH160 <20> EQUAL IF CHECKSIG ELSE <33> SWAP SIZE <n> EQUAL NOTIF DROP 2 SWAP'
                    ' <33> 2 CHECKMULTISIG ELSE HASH160 <20> EQUALVERIFY CHECKSIG ENDIF 1 CSV DROP ENDIF',
    'received-htlc': 'DUP HASH160 <20> EQUAL IF CHECKSIG ELSE <33> SWAP SIZE <n> EQUAL IF HASH160 <20>'
                     ' EQUALVERIFY 2 SWAP <33> 2 CHECKMULTISIG ELSE DROP <n> CLTV DROP CHECKSIG ENDIF'
                     ' 1 CSV DROP ENDIF',
}


def hash160(b):
    return hashlib.new('ripemd160', hashlib.sha256(b).digest()).digest()


def tokens(script):
    """[(token, data)]: pushes become '<33>', '<20>' or '<n>' (a number)."""
    out, i = [], 0
    while i < len(script):
        op = script[i]
        i += 1
        if 1 <= op <= 0x4b:
            data = script[i:i + op]
            i += op
            out.append(('<%d>' % op if op in (20, 33) else '<n>', data))
        elif op in OPS:
            out.append((OPS[op], None))
        else:
            out.append(('op%02x' % op, None))
    return out


def scriptnum(b):
    if isinstance(b, int):
        return b
    n = int.from_bytes(b, 'little')
    if b and b[-1] & 0x80:
        n = -(n & ~(0x80 << (8 * (len(b) - 1))))
    return n


def template(ws):
    """(name, the pushes the template's <..> slots matched).  A <n> slot
    matches a number push or a small-number opcode (OP_1..OP_16)."""
    toks = tokens(ws)
    for name, tpl in TEMPLATES.items():
        tpl = tpl.split()
        if len(tpl) != len(toks):
            continue
        pushes = []
        for want, (tok, data) in zip(tpl, toks):
            if want == '<n>' and tok.isdigit():
                pushes.append(int(tok))
            elif want == tok:
                if want.startswith('<'):
                    pushes.append(data)
            else:
                break
        else:
            return name, pushes
    return 'unknown(%s)' % ' '.join(k for k, _ in toks), []


def spk_type(spk):
    if len(spk) == 34 and spk[0] == 0x51 and spk[1] == 32:
        return 'p2tr'
    if len(spk) == 22 and spk[:2] == b'\x00\x14':
        return 'p2wpkh'
    if len(spk) == 34 and spk[:2] == b'\x00\x20':
        return 'p2wsh'
    return 'other(%s)' % spk.hex()


def sighash(sig):
    return {1: 'ALL', 0x83: 'SINGLE|ACP', 0x81: 'ALL|ACP', 3: 'SINGLE'}.get(sig[-1], hex(sig[-1]))


class Bad(Exception):
    pass


def check(cond, what):
    if not cond:
        raise Bad(what)


def classify_input(tx, n, prevout):
    """(spk type, template, path, details) for input n of tx."""
    txid_in, vout, ss, seq = tx.inputs[n]
    w = tx.witnesses[n]
    if prevout.get('mint'):
        return 'mint', '-', '-', ''
    spk = bytes.fromhex(prevout['script'])
    st = spk_type(spk)
    check(ss == b'', 'segwit input with a scriptSig')
    if st == 'p2tr':
        check(len(w) == 1 and len(w[0]) in (64, 65), 'p2tr: not a key-path spend')
        return st, 'key', 'key-path', 'sighash=%s' % ('DEFAULT' if len(w[0]) == 64 else hex(w[0][64]))
    if st == 'p2wpkh':
        check(len(w) == 2 and len(w[1]) == 33 and hash160(w[1]) == spk[2:], 'p2wpkh: bad witness')
        return st, 'pkh', 'sig+pubkey', 'sighash=%s' % sighash(w[0])
    check(st == 'p2wsh', 'unexpected prevout type ' + st)
    ws = w[-1]
    check(hashlib.sha256(ws).digest() == spk[2:], 'p2wsh: witness script does not hash to the program')
    name, pushes = template(ws)
    stack = w[:-1]
    csv = seq & 0xffff if not seq & (1 << 31) and tx.version >= 2 else None
    if name == 'funding-2of2':
        check(len(stack) == 3 and stack[0] == b'', 'funding: not 0 <sig> <sig>')
        commit = (seq >> 24) == 0x80 and (tx.locktime >> 24) == 0x20
        return st, name, '2-of-2', ('commitment (obscured number)' if commit else 'no commitment number') + \
            ', sighash=%s/%s' % (sighash(stack[1]), sighash(stack[2]))
    if name == 'to_local':
        revpk, delay, delayedpk = pushes[0], scriptnum(pushes[1]), pushes[2]
        check(len(stack) == 2, 'to_local: stack size')
        if stack[1] == b'\x01':
            return st, name, 'revocation', 'sighash=%s' % sighash(stack[0])
        check(stack[1] == b'', 'to_local: bad selector')
        check(csv is not None and csv >= delay, 'to_local delayed: nSequence %s < CSV %d' % (csv, delay))
        return st, name, 'delayed (CSV %d)' % delay, 'nSequence=%d' % csv
    if name == 'to_remote':
        check(len(stack) == 1 and csv is not None and csv >= 1, 'to_remote: stack or nSequence')
        return st, name, 'sig (CSV 1)', 'sighash=%s' % sighash(stack[0])
    if name == 'anchor':
        check(len(stack) == 1, 'anchor: stack size')
        if stack[0] == b'':
            check(csv is not None and csv >= 16, 'anchor: anyone-can-spend before CSV 16')
            return st, name, 'anyone (CSV 16)', ''
        return st, name, 'owner sig', 'sighash=%s' % sighash(stack[0])
    if name in ('offered-htlc', 'received-htlc'):
        revhash, payhash = pushes[0], pushes[4] if name == 'offered-htlc' else pushes[3]
        if len(stack) == 2 and len(stack[1]) == 33:
            check(hash160(stack[1]) == revhash, 'htlc revocation: pubkey does not match')
            return st, name, 'revocation', 'sighash=%s' % sighash(stack[0])
        if name == 'offered-htlc':
            if len(stack) == 2 and len(stack[1]) == 32:
                check(hash160(stack[1]) == payhash, 'offered: preimage does not match')
                check(csv is not None and csv >= 1, 'offered success: CSV 1')
                return st, name, 'preimage (remote success, CSV 1)', 'sighash=%s' % sighash(stack[0])
            check(len(stack) == 4 and stack[0] == b'' and stack[3] == b'', 'offered: unknown stack')
            return st, name, 'HTLC-timeout 2-of-2', 'remote sighash=%s, locktime=%d' % (
                sighash(stack[1]), tx.locktime)
        cltv = scriptnum(pushes[5])
        if len(stack) == 4:
            check(stack[0] == b'' and len(stack[3]) == 32 and hash160(stack[3]) == payhash,
                  'received: bad HTLC-success stack or preimage')
            return st, name, 'HTLC-success 2-of-2+preimage', 'remote sighash=%s' % sighash(stack[1])
        check(len(stack) == 2 and stack[1] == b'', 'received: unknown stack')
        check(tx.locktime >= cltv and tx.locktime < 500000000, 'received timeout: locktime < CLTV')
        check(seq != 0xffffffff and csv is not None and csv >= 1, 'received timeout: CSV 1 / final seq')
        return st, name, 'timeout (CLTV %d, CSV 1)' % cltv, 'locktime=%d' % tx.locktime
    raise Bad('unknown witness script ' + name)


WALLET = {('p2tr', 'key'), ('p2wpkh', 'pkh'), ('p2wsh', 'to_remote')}
# kind -> (the (template, path prefix) one input must have, parent kinds or None)
EXPECT = {
    'deposit': (('-', '-'), None),
    'funding': (None, None), 'wallet-mixed': (None, None), 'withdraw-exit': (None, None),
    'to_remote-spend': (('to_remote', 'sig'), ('commitment', 'revoked-commitment')),
    'mutual-close': (('funding-2of2', '2-of-2'), ('funding',)),
    'commitment': (('funding-2of2', '2-of-2'), ('funding',)),
    'revoked-commitment': (('funding-2of2', '2-of-2'), ('funding',)),
    'anchor-cpfp': (('anchor', 'owner sig'), ('commitment', 'revoked-commitment')),
    'to_local-sweep': (('to_local', 'delayed'), ('commitment', 'revoked-commitment')),
    'htlc-success': (('received-htlc', 'HTLC-success'), ('commitment', 'revoked-commitment')),
    'htlc-timeout': (('offered-htlc', 'HTLC-timeout'), ('commitment', 'revoked-commitment')),
    'htlc-success-sweep': (('to_local', 'delayed'), ('htlc-success',)),
    'htlc-timeout-sweep': (('to_local', 'delayed'), ('htlc-timeout',)),
    'remote-success': (('offered-htlc', 'preimage'), ('commitment', 'revoked-commitment')),
    'remote-timeout': (('received-htlc', 'timeout'), ('commitment', 'revoked-commitment')),
    'penalty-to_local': (('to_local', 'revocation'), ('revoked-commitment',)),
    'penalty-offered-htlc': (('offered-htlc', 'revocation'), ('revoked-commitment',)),
    'penalty-received-htlc': (('received-htlc', 'revocation'), ('revoked-commitment',)),
    'penalty-htlc-tx': (('to_local', 'revocation'), ('htlc-success', 'htlc-timeout')),
}


def main(path):
    rows = [json.loads(line) for line in open(path)]
    kind_of = {r['txid']: r.get('kind', '?') for r in rows}
    table = collections.defaultdict(lambda: [0, collections.Counter()])
    errors = []
    for r in rows:
        tx = Tx.parse(bytes.fromhex(r['tx']))
        assert tx.txid() == r['txid']
        kind = r.get('kind', '?')
        ins = []
        try:
            for n, prev in enumerate(r['prevouts']):
                st, tpl, p, det = classify_input(tx, n, prev)
                ins.append((st, tpl, p, kind_of.get(tx.inputs[n][0], '-'), det))
        except Bad as e:
            errors.append('%s %s input %d: %s' % (kind, r['txid'][:16], n, e))
            continue
        table[kind][0] += 1
        table[kind][1]['; '.join(sorted({'%s/%s: %s%s' % (st, tpl, p, ' [%s]' % det if 'SINGLE' in det
                                                          or 'commitment' in det else '')
                                         for st, tpl, p, _, det in ins}))] += 1
        want, parents = EXPECT.get(kind.split('+')[0], (None, None))
        if kind not in EXPECT and '+' not in kind:
            errors.append('%s %s: unexpected kind' % (kind, r['txid'][:16]))
            continue
        if want and not any(tpl == want[0] and p.startswith(want[1])
                            and (parents is None or par in parents) for _, tpl, p, par, _ in ins):
            errors.append('%s %s: no input spends %s by %s (from %s)' % (kind, r['txid'][:16], want[0],
                                                                                                       want[1], parents))
        if want is None and not all((st, tpl) in WALLET for st, tpl, _, _, _ in ins):
            errors.append('%s %s: a non-wallet input' % (kind, r['txid'][:16]))
    print('%-22s %3s  %s' % ('kind', 'n', 'inputs: prevout type / witness script: path'))
    for kind in sorted(table):
        n, pats = table[kind]
        for i, (pat, c) in enumerate(sorted(pats.items())):
            print('%-22s %3s  %s%s' % (kind if i == 0 else '', n if i == 0 else '', pat,
                                       '  (x%d)' % c if len(pats) > 1 else ''))
    for e in errors:
        print('ERROR', e)
    print('%d txs, %d kinds, %d errors' % (len(rows), len(table), len(errors)))
    return not errors


if __name__ == '__main__':
    sys.exit(0 if main(sys.argv[1] if len(sys.argv) > 1 else
                       os.path.join(HERE, 'vectors', 'cln-regtest-txs-full.jsonl')) else 1)
