# SPDX-License-Identifier: MIT
"""Independent reference for contrib/evmchan/SPEC.md (draft 2):

- 4.3, the validity of a submitted transaction;
- 3.3, the synthetic deposit transaction (and 4.2's deposit checks);
- 4.4, how a valid transaction changes the UTXO set (class Chain).

Follows SPEC draft 3, including the conservative "readings fixed in draft
3" at the end of 4.3.7 (Bitcoin-exact alternatives are left in comments).
Written from the SPEC, BIP143/340/341/65/66/68/112/113, Bitcoin Core's script
semantics and the builders in bitcoin/script.c, without looking at the
contract.  It is meant to be obviously right rather than fast.

    validate(raw_tx, prevouts, height, genesis_time, interval)
        -> (ok, error, txid, fee)

`prevouts` holds one entry per input, in input order: {"amount", "script"
(hex), "height"} or [amount, script, height].  They stand for the UTXO set
entries the inputs spend (rule 2's commitment check is the caller's: these ARE
the committed values).  `txid` is in display (reversed) hex, `fee` in sats.
The README lists the error names and the readings chosen where the SPEC is
ambiguous.
"""
import hashlib
import struct

import coincurve

MAX_MONEY = 21_000_000 * 10**8
MAX_TX_SIZE = 100_000
MAX_SIGOPS_COST = 16_000
MAX_ELEMENT_SIZE = 520
LOCKTIME_THRESHOLD = 500_000_000
SEQUENCE_FINAL = 0xffffffff
SEQ_DISABLE = 1 << 31
SEQ_TYPE = 1 << 22
SEQ_MASK = 0x0000ffff
SAT_WEI = 10**10
MAX_DEPOSIT_SCRIPT = 10_000

GENESIS_TIME = 1700000000   # the toy chain's (spike S1), which the vectors use
INTERVAL = 600

SECP_P = 2**256 - 2**32 - 977
SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

SIGHASH_ALL, SIGHASH_NONE, SIGHASH_SINGLE, SIGHASH_ANYONECANPAY = 1, 2, 3, 0x80
ECDSA_HASHTYPES = (0x01, 0x02, 0x03, 0x81, 0x82, 0x83)
TAPROOT_HASHTYPES = (0x00,) + ECDSA_HASHTYPES


class Invalid(Exception):
    """The transaction is invalid; .args[0] is the error name."""

    @property
    def name(self):
        return self.args[0]


# ---------------------------------------------------------------- hashes
def sha256(b):
    return hashlib.sha256(b).digest()


def dsha(b):
    return sha256(sha256(b))


def tagged_hash(tag, data):
    t = sha256(tag.encode())
    return sha256(t + t + data)


# RIPEMD-160, in pure Python, used when hashlib's OpenSSL lacks it.
_RL = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
       7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
       3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
       1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
       4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
_RR = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
       6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
       15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
       8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
       12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
_SL = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
       7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
       11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
       11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
       9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
_SR = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
       9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
       9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
       15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
       8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
_KL = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
_KR = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]
_M32 = 0xffffffff


def _rmd_f(j, x, y, z):
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | (~x & z & _M32)
    if j < 48:
        return (x | (~y & _M32)) ^ z
    if j < 64:
        return (x & z) | (y & ~z & _M32)
    return x ^ (y | (~z & _M32))


def _rol32(x, n):
    return ((x << n) | (x >> (32 - n))) & _M32


def ripemd160_py(msg):
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    ml = len(msg) * 8
    msg = msg + b'\x80' + b'\x00' * ((55 - len(msg)) % 64) + struct.pack('<Q', ml)
    for off in range(0, len(msg), 64):
        X = struct.unpack('<16I', msg[off:off + 64])
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for j in range(80):
            t = _rol32((al + _rmd_f(j, bl, cl, dl) + X[_RL[j]] + _KL[j // 16]) & _M32, _SL[j])
            t = (t + el) & _M32
            al, el, dl, cl, bl = el, dl, _rol32(cl, 10), bl, t
            t = _rol32((ar + _rmd_f(79 - j, br, cr, dr) + X[_RR[j]] + _KR[j // 16]) & _M32, _SR[j])
            t = (t + er) & _M32
            ar, er, dr, cr, br = er, dr, _rol32(cr, 10), br, t
        t = (h[1] + cl + dr) & _M32
        h[1] = (h[2] + dl + er) & _M32
        h[2] = (h[3] + el + ar) & _M32
        h[3] = (h[4] + al + br) & _M32
        h[4] = (h[0] + bl + cr) & _M32
        h[0] = t
    return struct.pack('<5I', *h)


def ripemd160(b):
    try:
        return hashlib.new('ripemd160', b).digest()
    except ValueError:
        return ripemd160_py(b)


def hash160(b):
    return ripemd160(sha256(b))


# Keccak-256 (Ethereum's, padding 0x01), in pure Python.
def _rol64(a, n):
    n %= 64
    return ((a << n) | (a >> (64 - n))) & 0xffffffffffffffff


def _keccak_f(lanes):
    R = 1
    for _ in range(24):
        C = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)]
        D = [C[(x + 4) % 5] ^ _rol64(C[(x + 1) % 5], 1) for x in range(5)]
        lanes = [[lanes[x][y] ^ D[x] for y in range(5)] for x in range(5)]
        x, y = 1, 0
        cur = lanes[x][y]
        for t in range(24):
            x, y = y, (2 * x + 3 * y) % 5
            cur, lanes[x][y] = lanes[x][y], _rol64(cur, (t + 1) * (t + 2) // 2)
        for y in range(5):
            T = [lanes[x][y] for x in range(5)]
            for x in range(5):
                lanes[x][y] = T[x] ^ ((~T[(x + 1) % 5]) & T[(x + 2) % 5])
        for j in range(7):
            R = ((R << 1) ^ ((R >> 7) * 0x71)) % 256
            if R & 2:
                lanes[0][0] ^= 1 << ((1 << j) - 1)
    return lanes


def keccak256(data):
    rate = 136
    padded = bytearray(data) + b'\x01' + b'\x00' * ((rate - 1 - len(data) % rate) % rate)
    padded[-1] |= 0x80
    lanes = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            x, y = i % 5, i // 5
            lanes[x][y] ^= int.from_bytes(block[8 * i:8 * i + 8], 'little')
        lanes = _keccak_f(lanes)
    out = b''.join(lanes[i % 5][i // 5].to_bytes(8, 'little') for i in range(4))
    return out


# ---------------------------------------------------------------- tx format
def varint(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b'\xfd' + struct.pack('<H', n)
    if n <= 0xffffffff:
        return b'\xfe' + struct.pack('<I', n)
    return b'\xff' + struct.pack('<Q', n)


class _Reader:
    def __init__(self, b):
        self.b, self.i = b, 0

    def take(self, n):
        if self.i + n > len(self.b):
            raise Invalid('Truncated')
        r = self.b[self.i:self.i + n]
        self.i += n
        return r

    def peek(self):
        if self.i >= len(self.b):
            raise Invalid('Truncated')
        return self.b[self.i]

    def u32(self):
        return struct.unpack('<I', self.take(4))[0]

    def u64(self):
        return struct.unpack('<Q', self.take(8))[0]

    def varint(self):
        n = self.take(1)[0]
        if n < 0xfd:
            return n
        size, floor = {0xfd: (2, 0xfd), 0xfe: (4, 0x10000), 0xff: (8, 0x100000000)}[n]
        v = int.from_bytes(self.take(size), 'little')
        if v < floor:
            raise Invalid('NonCanonicalVarint')
        return v

    def varbytes(self):
        return self.take(self.varint())


class TxIn:
    __slots__ = ('prev_txid', 'vout', 'script_sig', 'sequence')

    def __init__(self, prev_txid, vout, script_sig, sequence):
        self.prev_txid = prev_txid      # 32 bytes, internal byte order
        self.vout, self.script_sig, self.sequence = vout, script_sig, sequence

    def outpoint(self):
        return self.prev_txid + struct.pack('<I', self.vout)


class Tx:
    def __init__(self, version, inputs, outputs, locktime, witnesses=None):
        self.version, self.inputs, self.outputs, self.locktime = version, inputs, outputs, locktime
        self.witnesses = witnesses if witnesses is not None else [[] for _ in inputs]

    def serialize(self, witness=True):
        witness = witness and any(self.witnesses)
        out = [struct.pack('<I', self.version)]
        if witness:
            out.append(b'\x00\x01')
        out.append(varint(len(self.inputs)))
        for i in self.inputs:
            out += [i.outpoint(), varint(len(i.script_sig)), i.script_sig, struct.pack('<I', i.sequence)]
        out.append(varint(len(self.outputs)))
        for amount, spk in self.outputs:
            out += [struct.pack('<Q', amount), varint(len(spk)), spk]
        if witness:
            for w in self.witnesses:
                out.append(varint(len(w)))
                for item in w:
                    out += [varint(len(item)), item]
        out.append(struct.pack('<I', self.locktime))
        return b''.join(out)

    def txid_bytes(self):
        return dsha(self.serialize(witness=False))

    def txid(self):
        return self.txid_bytes()[::-1].hex()


def parse_tx(raw):
    """Strict parse (rule 1): canonical varints, no trailing bytes, the
    marker and flag 00 01 present exactly when some witness is non-empty."""
    if len(raw) > MAX_TX_SIZE:
        raise Invalid('TxTooLarge')
    r = _Reader(raw)
    version = r.u32()
    segwit = False
    if r.peek() == 0:
        # A zero input count is never valid, so 00 can only be the marker.
        r.take(1)
        if r.take(1)[0] != 1:
            raise Invalid('BadWitnessFlag')
        segwit = True
    inputs = []
    for _ in range(r.varint()):
        prev = r.take(32)
        vout = r.u32()
        inputs.append(TxIn(prev, vout, r.varbytes(), r.u32()))
    outputs = []
    for _ in range(r.varint()):
        amount = r.u64()
        outputs.append((amount, r.varbytes()))
    witnesses = [[] for _ in inputs]
    if segwit:
        for i in range(len(inputs)):
            witnesses[i] = [r.varbytes() for _ in range(r.varint())]
    locktime = r.u32()
    if r.i != len(raw):
        raise Invalid('TrailingBytes')
    if segwit and not any(witnesses):
        raise Invalid('SuperfluousWitness')
    return Tx(version, inputs, outputs, locktime, witnesses)


# ---------------------------------------------------------------- scripts
OP_0, OP_PUSHDATA1, OP_PUSHDATA2, OP_PUSHDATA4 = 0x00, 0x4c, 0x4d, 0x4e
OP_1, OP_16 = 0x51, 0x60
OP_IFDUP, OP_IF, OP_NOTIF, OP_ELSE, OP_ENDIF = 0x73, 0x63, 0x64, 0x67, 0x68
OP_DROP, OP_DUP, OP_SWAP, OP_SIZE = 0x75, 0x76, 0x7c, 0x82
OP_EQUAL, OP_EQUALVERIFY, OP_HASH160 = 0x87, 0x88, 0xa9
OP_CHECKSIG, OP_CHECKSIGVERIFY, OP_CHECKMULTISIG, OP_CHECKMULTISIGVERIFY = 0xac, 0xad, 0xae, 0xaf
OP_CLTV, OP_CSV = 0xb1, 0xb2


def legacy_sigops(script):
    """Bitcoin's CScript::GetSigOpCount(false)."""
    n, i, end = 0, 0, len(script)
    while i < end:
        op = script[i]
        i += 1
        if op <= OP_PUSHDATA4:
            if op < OP_PUSHDATA1:
                size = op
            else:
                width = {OP_PUSHDATA1: 1, OP_PUSHDATA2: 2, OP_PUSHDATA4: 4}[op]
                if end - i < width:
                    break
                size = int.from_bytes(script[i:i + width], 'little')
                i += width
            if end - i < size:
                break
            i += size
        elif op in (OP_CHECKSIG, OP_CHECKSIGVERIFY):
            n += 1
        elif op in (OP_CHECKMULTISIG, OP_CHECKMULTISIGVERIFY):
            n += 20
    return n


def script_ops(script):
    """[(opcode, pushed data or None)]; the templates are always well formed."""
    ops, i = [], 0
    while i < len(script):
        op = script[i]
        i += 1
        if op <= OP_PUSHDATA4:
            if op < OP_PUSHDATA1:
                size = op
            else:
                width = {OP_PUSHDATA1: 1, OP_PUSHDATA2: 2, OP_PUSHDATA4: 4}[op]
                size = int.from_bytes(script[i:i + width], 'little')
                i += width
            ops.append((op, script[i:i + size]))
            i += size
        else:
            ops.append((op, None))
    return ops


def scriptnum_encode(v):
    if v == 0:
        return b''
    neg, a, out = v < 0, abs(v), bytearray()
    while a:
        out.append(a & 0xff)
        a >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if neg else 0)
    elif neg:
        out[-1] |= 0x80
    return bytes(out)


def scriptnum_decode(b, max_len=4):
    """CScriptNum with fRequireMinimal."""
    if len(b) > max_len:
        raise Invalid('ScriptNumOverflow')
    if b and (b[-1] & 0x7f) == 0 and (len(b) == 1 or not (b[-2] & 0x80)):
        raise Invalid('NonMinimalNumber')
    if not b:
        return 0
    v = int.from_bytes(b, 'little')
    if b[-1] & 0x80:
        return -(v & ~(0x80 << (8 * (len(b) - 1))))
    return v


def cast_to_bool(b):
    for i, c in enumerate(b):
        if c:
            return not (i == len(b) - 1 and c == 0x80)
    return False


def add_number(num):
    """bitcoin/script.c's add_number(u32)."""
    if num == 0:
        return b'\x00'
    if num <= 16:
        return bytes([0x50 + num])
    for width, top in ((1, 0x7f), (2, 0x7fff), (3, 0x7fffff), (4, 0x7fffffff), (5, 0xffffffff)):
        if num <= top:
            return bytes([width]) + num.to_bytes(8, 'little')[:width]
    raise ValueError('add_number takes a u32')


# The anchors-channel witness scripts, byte for byte as bitcoin/script.c
# builds them. 'K' is a 33-byte push (a key), 'H' a 20-byte push (a hash),
# 'N' a number as add_number() writes it; ints are literal bytes.
TEMPLATES = {
    '2of2': [0x52, 'K', 'K', 0x52, 0xae],
    # bitcoin_wscript_to_local without a lease == bitcoin_wscript_htlc_tx
    'to_local': [0x63, 'K', 0x67, 'N', 0xb2, 0x75, 'K', 0x68, 0xac],
    # bitcoin_wscript_to_remote_anchored with csv_lock 1 (no lease)
    'to_remote': ['K', 0xad, 0x51, 0xb2],
    'anchor': ['K', 0xac, 0x73, 0x64, 0x60, 0xb2, 0x68],
    'offered': [0x76, 0xa9, 'H', 0x87, 0x63, 0xac, 0x67, 'K', 0x7c, 0x82, 0x01, 0x20, 0x87,
                0x64, 0x75, 0x52, 0x7c, 'K', 0x52, 0xae, 0x67, 0xa9, 'H', 0x88, 0xac, 0x68,
                0x51, 0xb2, 0x75, 0x68],
    'received': [0x76, 0xa9, 'H', 0x87, 0x63, 0xac, 0x67, 'K', 0x7c, 0x82, 0x01, 0x20, 0x87,
                 0x63, 0xa9, 'H', 0x88, 0x52, 0x7c, 'K', 0x52, 0xae, 0x67, 0x75, 'N', 0xb1,
                 0x75, 0xac, 0x68, 0x51, 0xb2, 0x75, 0x68],
}


def match_template(script):
    """(name, [slot values]) if script is one of TEMPLATES, else None."""
    for name, pattern in TEMPLATES.items():
        p, slots = 0, []
        for tok in pattern:
            if tok == 'K' or tok == 'H':
                n = 33 if tok == 'K' else 20
                if script[p:p + 1] != bytes([n]) or len(script) < p + 1 + n:
                    break
                slots.append(script[p + 1:p + 1 + n])
                p += 1 + n
            elif tok == 'N':
                if p >= len(script):
                    break
                b = script[p]
                if b == 0:
                    slots.append(0)
                    p += 1
                elif OP_1 <= b <= OP_16:
                    slots.append(b - 0x50)
                    p += 1
                elif 1 <= b <= 5 and len(script) >= p + 1 + b:
                    v = int.from_bytes(script[p + 1:p + 1 + b], 'little')
                    if v > 0xffffffff or add_number(v) != script[p:p + 1 + b]:
                        break
                    slots.append(v)
                    p += 1 + b
                else:
                    break
            else:
                if script[p:p + 1] != bytes([tok]):
                    break
                p += 1
        else:
            if p == len(script):
                return name, slots
    return None


# ---------------------------------------------------------------- signatures
def is_strict_der(sig):
    """BIP66 IsValidSignatureEncoding, on the signature with its hash type."""
    if len(sig) < 9 or len(sig) > 73:
        return False
    if sig[0] != 0x30 or sig[1] != len(sig) - 3:
        return False
    len_r = sig[3]
    if 5 + len_r >= len(sig):
        return False
    len_s = sig[5 + len_r]
    if len_r + len_s + 7 != len(sig):
        return False
    if sig[2] != 0x02 or len_r == 0 or sig[4] & 0x80:
        return False
    if len_r > 1 and sig[4] == 0 and not sig[5] & 0x80:
        return False
    if sig[len_r + 4] != 0x02 or len_s == 0 or sig[len_r + 6] & 0x80:
        return False
    if len_s > 1 and sig[len_r + 6] == 0 and not sig[len_r + 7] & 0x80:
        return False
    return True


def der_rs(sig):
    """(r, s) of a strict-DER signature (hash type byte included)."""
    len_r = sig[3]
    r = int.from_bytes(sig[4:4 + len_r], 'big')
    len_s = sig[5 + len_r]
    s = int.from_bytes(sig[6 + len_r:6 + len_r + len_s], 'big')
    return r, s


def der_encode(r, s):
    def enc(v):
        b = v.to_bytes(max(1, (v.bit_length() + 7) // 8), 'big')
        if b[0] & 0x80:
            b = b'\x00' + b
        return b'\x02' + bytes([len(b)]) + b
    body = enc(r) + enc(s)
    return b'\x30' + bytes([len(body)]) + body


def check_sig_encoding(sig):
    """CheckSignatureEncoding with DERSIG, LOW_S and STRICTENC."""
    if not sig:
        return
    if not is_strict_der(sig):
        raise Invalid('BadSigEncoding')
    r, s = der_rs(sig)
    # libsecp256k1's lax parse zeroes an out-of-range (r, s); only then is it low.
    if r < SECP_N and SECP_N // 2 < s < SECP_N:
        raise Invalid('HighS')
    if sig[-1] not in ECDSA_HASHTYPES:
        raise Invalid('BadHashType')


def check_pubkey_encoding(key):
    """STRICTENC + WITNESS_PUBKEYTYPE: a 33-byte compressed encoding."""
    if len(key) != 33 or key[0] not in (2, 3):
        raise Invalid('BadPubKey')


def ecdsa_verify(pub33, sig_der_ht, msg32):
    r, s = der_rs(sig_der_ht)
    if not (1 <= r < SECP_N and 1 <= s < SECP_N):
        return False
    try:
        pk = coincurve.PublicKey(pub33)
    except Exception:
        return False        # not on the curve
    return pk.verify(der_encode(r, s), msg32, hasher=None)


def schnorr_verify(xonly32, msg, sig64):
    try:
        pk = coincurve.PublicKeyXOnly(xonly32)
    except Exception:
        return False        # x not on the curve, or >= p
    return pk.verify(sig64, msg)


class _Sighash:
    """BIP143 and BIP341 sighashes of one tx, with its prevouts."""

    def __init__(self, tx, prevouts):
        self.tx, self.prevouts = tx, prevouts
        self._c = {}

    def _memo(self, key, fn):
        if key not in self._c:
            self._c[key] = fn()
        return self._c[key]

    def _outputs_ser(self):
        return [struct.pack('<Q', a) + varint(len(s)) + s for a, s in self.tx.outputs]

    def bip143(self, i, script_code, amount, hashtype):
        tx = self.tx
        base, acp = hashtype & 0x1f, hashtype & SIGHASH_ANYONECANPAY
        zero = b'\x00' * 32
        hp = zero if acp else self._memo('hp', lambda: dsha(b''.join(x.outpoint() for x in tx.inputs)))
        if acp or base in (SIGHASH_SINGLE, SIGHASH_NONE):
            hs = zero
        else:
            hs = self._memo('hs', lambda: dsha(b''.join(struct.pack('<I', x.sequence) for x in tx.inputs)))
        if base not in (SIGHASH_SINGLE, SIGHASH_NONE):
            ho = self._memo('ho', lambda: dsha(b''.join(self._outputs_ser())))
        elif base == SIGHASH_SINGLE and i < len(tx.outputs):
            ho = dsha(self._outputs_ser()[i])
        else:
            ho = zero
        txin = tx.inputs[i]
        pre = (struct.pack('<I', tx.version) + hp + hs + txin.outpoint()
               + varint(len(script_code)) + script_code + struct.pack('<Q', amount)
               + struct.pack('<I', txin.sequence) + ho + struct.pack('<I', tx.locktime)
               + struct.pack('<I', hashtype))
        return dsha(pre)

    def bip341_msg(self, i, hashtype):
        """SigMsg with the epoch byte in front; None if SINGLE has no output."""
        tx, pv = self.tx, self.prevouts
        out_type = SIGHASH_ALL if hashtype == 0 else hashtype & 3
        acp = hashtype & SIGHASH_ANYONECANPAY
        m = [b'\x00', bytes([hashtype]), struct.pack('<I', tx.version), struct.pack('<I', tx.locktime)]
        if not acp:
            m.append(self._memo('sp', lambda: sha256(b''.join(x.outpoint() for x in tx.inputs))))
            m.append(self._memo('sa', lambda: sha256(b''.join(struct.pack('<Q', p[0]) for p in pv))))
            m.append(self._memo('ss', lambda: sha256(b''.join(varint(len(p[1])) + p[1] for p in pv))))
            m.append(self._memo('sq', lambda: sha256(b''.join(struct.pack('<I', x.sequence)
                                                              for x in tx.inputs))))
        if out_type not in (SIGHASH_NONE, SIGHASH_SINGLE):
            m.append(self._memo('so', lambda: sha256(b''.join(self._outputs_ser()))))
        m.append(b'\x00')   # spend_type: key path, no annex
        if acp:
            txin = tx.inputs[i]
            m += [txin.outpoint(), struct.pack('<Q', pv[i][0]), varint(len(pv[i][1])), pv[i][1],
                  struct.pack('<I', txin.sequence)]
        else:
            m.append(struct.pack('<I', i))
        if out_type == SIGHASH_SINGLE:
            if i >= len(tx.outputs):
                return None
            m.append(sha256(self._outputs_ser()[i]))
        return b''.join(m)

    def bip341(self, i, hashtype):
        msg = self.bip341_msg(i, hashtype)
        return None if msg is None else tagged_hash('TapSighash', msg)


# ---------------------------------------------------------------- interpreter
class _Script:
    """Bitcoin Script, segwit v0, for the opcodes the templates use; flags:
    DERSIG, LOW_S, STRICTENC, WITNESS_PUBKEYTYPE, NULLFAIL, NULLDUMMY,
    MINIMALIF, MINIMALDATA, CLEANSTACK, CHECKLOCKTIMEVERIFY,
    CHECKSEQUENCEVERIFY."""

    def __init__(self, tx, i, amount, sighash):
        self.tx, self.i, self.amount, self.sh = tx, i, amount, sighash

    # CHECKSIG of one (sig, key); raises on encoding errors and NULLFAIL.
    def checksig(self, sig, key, script_code):
        check_sig_encoding(sig)
        check_pubkey_encoding(key)
        if not sig:
            return False
        msg = self.sh.bip143(self.i, script_code, self.amount, sig[-1])
        ok = ecdsa_verify(key, sig, msg)
        if not ok:
            raise Invalid('BadSignature')   # NULLFAIL: a non-empty failing sig
        return True

    def _check_sequence(self, n):
        """SPEC draft 3: n is compared whole with sequence & 0xffff, so
        n >= 2^16 (or with bit 22 or 31 set) is never satisfied.  Bitcoin's
        CheckSequence masks n with (type | 0xffff) instead, and OP_CSV is a
        NOP when n has bit 31 set:
            if n & SEQ_DISABLE: nop
            a, b = seq & (SEQ_TYPE | SEQ_MASK), n & (SEQ_TYPE | SEQ_MASK)
            types must match; return b <= a"""
        seq = self.tx.inputs[self.i].sequence
        if self.tx.version < 2:          # version is a uint32
            return False
        if seq & SEQ_DISABLE:
            return False
        if n > SEQ_MASK:                 # also covers bits 22 and 31 of n
            return False
        if seq & SEQ_TYPE:               # n is always "blocks": types differ
            return False
        return n <= seq & SEQ_MASK

    def _check_locktime(self, n):
        lt = self.tx.locktime
        if not ((lt < LOCKTIME_THRESHOLD and n < LOCKTIME_THRESHOLD)
                or (lt >= LOCKTIME_THRESHOLD and n >= LOCKTIME_THRESHOLD)):
            return False
        if n > lt:
            return False
        return self.tx.inputs[self.i].sequence != SEQUENCE_FINAL

    def run(self, script, stack):
        st = list(stack)
        vexec = []

        def need(n):
            if len(st) < n:
                raise Invalid('StackUnderflow')

        for op, data in script_ops(script):
            fexec = all(vexec)
            if op <= OP_PUSHDATA4:
                if fexec:
                    st.append(data)
                continue
            if not fexec and not (OP_IF <= op <= OP_ENDIF):
                continue
            if OP_1 <= op <= OP_16:
                st.append(scriptnum_encode(op - 0x50))
            elif op in (OP_IF, OP_NOTIF):
                val = False
                if fexec:
                    need(1)
                    top = st[-1]
                    if len(top) > 1 or (len(top) == 1 and top[0] != 1):
                        raise Invalid('MinimalIf')
                    val = cast_to_bool(top)
                    if op == OP_NOTIF:
                        val = not val
                    st.pop()
                vexec.append(val)
            elif op == OP_ELSE:
                if not vexec:
                    raise Invalid('StackUnderflow')
                vexec[-1] = not vexec[-1]
            elif op == OP_ENDIF:
                if not vexec:
                    raise Invalid('StackUnderflow')
                vexec.pop()
            elif op == OP_IFDUP:
                need(1)
                if cast_to_bool(st[-1]):
                    st.append(st[-1])
            elif op == OP_DROP:
                need(1)
                st.pop()
            elif op == OP_DUP:
                need(1)
                st.append(st[-1])
            elif op == OP_SWAP:
                need(2)
                st[-1], st[-2] = st[-2], st[-1]
            elif op == OP_SIZE:
                need(1)
                st.append(scriptnum_encode(len(st[-1])))
            elif op in (OP_EQUAL, OP_EQUALVERIFY):
                need(2)
                eq = st.pop() == st.pop()
                if op == OP_EQUALVERIFY:
                    if not eq:
                        raise Invalid('BadPreimage')    # only the payment-hash check uses it
                else:
                    st.append(b'\x01' if eq else b'')
            elif op == OP_HASH160:
                need(1)
                st.append(hash160(st.pop()))
            elif op in (OP_CHECKSIG, OP_CHECKSIGVERIFY):
                need(2)
                key, sig = st.pop(), st.pop()
                ok = self.checksig(sig, key, script)
                if op == OP_CHECKSIGVERIFY:
                    if not ok:
                        raise Invalid('BadSignature')
                else:
                    st.append(b'\x01' if ok else b'')
            elif op == OP_CHECKMULTISIG:
                st.append(b'\x01' if self._checkmultisig(st, script) else b'')
            elif op == OP_CSV:
                need(1)
                n = scriptnum_decode(st[-1], 5)
                if n < 0:
                    raise Invalid('NegativeLocktime')
                # (Bitcoin: an operand with bit 31 set makes CSV a NOP; draft 3: never met.)
                if not self._check_sequence(n):
                    raise Invalid('CsvNotMet')
            elif op == OP_CLTV:
                need(1)
                n = scriptnum_decode(st[-1], 5)
                if n < 0:
                    raise Invalid('NegativeLocktime')
                if not self._check_locktime(n):
                    raise Invalid('CltvNotMet')
            else:
                raise AssertionError('opcode %02x is in no template' % op)
        if vexec:
            raise Invalid('StackUnderflow')
        if len(st) != 1:
            raise Invalid('CleanStack')
        if not cast_to_bool(st[-1]):
            raise Invalid('BadSignature')   # every signature was empty
        return True

    def _checkmultisig(self, st, script):
        """Bitcoin Core's OP_CHECKMULTISIG loop, segwit v0 (no FindAndDelete)."""
        def top(k):
            return st[-k]
        i = 1
        if len(st) < i:
            raise Invalid('StackUnderflow')
        nkeys = scriptnum_decode(top(i))
        if nkeys < 0 or nkeys > 20:
            raise Invalid('PubKeyCount')
        i += 1
        ikey = i
        ikey2 = nkeys + 2
        i += nkeys
        if len(st) < i:
            raise Invalid('StackUnderflow')
        nsigs = scriptnum_decode(top(i))
        if nsigs < 0 or nsigs > nkeys:
            raise Invalid('SigCount')
        i += 1
        isig = i
        i += nsigs
        if len(st) < i:
            raise Invalid('StackUnderflow')
        success = True
        while success and nsigs > 0:
            sig, key = top(isig), top(ikey)
            check_sig_encoding(sig)
            check_pubkey_encoding(key)
            ok = bool(sig) and ecdsa_verify(key, sig, self.sh.bip143(self.i, script, self.amount, sig[-1]))
            if ok:
                isig += 1
                nsigs -= 1
            ikey += 1
            nkeys -= 1
            if nsigs > nkeys:
                success = False
        while i > 1:
            i -= 1
            if not success and not ikey2 and len(top(1)):
                raise Invalid('BadSignature')       # NULLFAIL
            if ikey2 > 0:
                ikey2 -= 1
            st.pop()
        if len(st) < 1:
            raise Invalid('StackUnderflow')
        if len(top(1)):
            raise Invalid('NullDummy')
        st.pop()
        return success


# ---------------------------------------------------------------- validation
def time_of(h, genesis_time=GENESIS_TIME, interval=INTERVAL):
    return genesis_time + h * interval


def mtp(h, genesis_time=GENESIS_TIME, interval=INTERVAL):
    """MTP(h) = T(max(h - 6, 0)), as SPEC 4.3.5 defines it."""
    return time_of(max(h - 6, 0), genesis_time, interval)


def is_final(tx, h, genesis_time=GENESIS_TIME, interval=INTERVAL):
    if all(x.sequence == SEQUENCE_FINAL for x in tx.inputs):
        return True
    if tx.locktime < LOCKTIME_THRESHOLD:
        return tx.locktime < h
    return tx.locktime < mtp(h, genesis_time, interval)


def _norm_prevouts(prevouts):
    out = []
    for p in prevouts:
        if isinstance(p, dict):
            a, s, h = p['amount'], p['script'], p['height']
        else:
            a, s, h = p
        out.append((int(a), bytes.fromhex(s) if isinstance(s, str) else bytes(s), int(h)))
    return out


def _spend_p2wpkh(tx, i, prog, amount, wit, sh):
    if len(wit) != 2:
        raise Invalid('BadWitness')
    # Bitcoin's 520-byte stack item limit can't change the verdict here (draft
    # 3: "never reached"): a longer sig fails DER, a longer key the hash.
    sig, key = wit
    if hash160(key) != prog:
        raise Invalid('KeyHashMismatch')
    script_code = b'\x76\xa9\x14' + prog + b'\x88\xac'
    if not _Script(tx, i, amount, sh).checksig(sig, key, script_code):
        raise Invalid('BadSignature')       # empty signature


def _spend_p2tr(tx, i, prog, wit, sh):
    if not wit:
        raise Invalid('BadWitness')
    if len(wit) >= 2 and wit[-1][:1] == b'\x50':
        raise Invalid('TaprootAnnex')
    if len(wit) != 1:
        raise Invalid('TaprootScriptPath')
    sig = wit[0]
    if len(sig) == 64:
        ht = 0
    elif len(sig) == 65:
        ht = sig[64]
        if ht == 0:
            raise Invalid('BadHashType')
    else:
        raise Invalid('BadSigEncoding')
    if ht not in TAPROOT_HASHTYPES:
        raise Invalid('BadHashType')
    msg = sh.bip341(i, ht)
    if msg is None:
        raise Invalid('SighashSingleNoOutput')
    if not schnorr_verify(prog, msg, sig[:64]):
        raise Invalid('BadSignature')


def _template_rules(name, slots, stack):
    """SPEC draft 3's readings on top of the byte pattern."""
    keys = [x for x in slots if isinstance(x, bytes) and len(x) == 33]
    if name == '2of2' and keys[0] > keys[1]:
        # bitcoin_redeem_2of2 sorts the keys; an unsorted script isn't the template
        raise Invalid('UnknownScript')
    # Keys are checked when a signature is verified against them (as Bitcoin
    # does), plus the anchor's key on its <> path, which verifies none
    # (SPEC 4.3.7, draft 3, "Keys").
    for k in (keys if name == 'anchor' else []):
        if k[0] not in (2, 3):
            raise Invalid('BadPubKey')
        try:
            coincurve.PublicKey(k)
        except Exception:
            raise Invalid('BadPubKey')
    if name in ('offered', 'received') and stack:
        top = stack[-1]
        # The HTLC timeout paths drop the top item when it is neither the
        # revocation key nor 32 bytes.  Bitcoin takes any such item; draft 3
        # requires exactly <>.
        if hash160(top) != slots[0] and len(top) != 32 and top != b'':
            raise Invalid('BadTimeoutDummy')


def _spend_p2wsh(tx, i, prog, amount, wit, sh):
    if not wit:
        raise Invalid('BadWitness')
    script, stack = wit[-1], wit[:-1]
    if sha256(script) != prog:
        raise Invalid('WitnessScriptMismatch')
    m = match_template(script)
    if m is None:
        raise Invalid('UnknownScript')
    _template_rules(m[0], m[1], stack)
    # (Bitcoin's 520-byte stack item limit: never reached, draft 3.)
    _Script(tx, i, amount, sh).run(script, stack)


def check_input(tx, prevouts, i, sh=None):
    """Rule 7 for input i (prevouts normalised: [(amount, script, height)])."""
    sh = sh or _Sighash(tx, prevouts)
    amount, spk, _ = prevouts[i]
    wit = tx.witnesses[i]
    if len(spk) == 22 and spk[:2] == b'\x00\x14':
        _spend_p2wpkh(tx, i, spk[2:], amount, wit, sh)
    elif len(spk) == 34 and spk[:2] == b'\x00\x20':
        _spend_p2wsh(tx, i, spk[2:], amount, wit, sh)
    elif len(spk) == 34 and spk[:2] == b'\x51\x20':
        _spend_p2tr(tx, i, spk[2:], wit, sh)
    else:
        raise Invalid('UnknownScript')


def _validate(raw, prevouts, h, genesis_time, interval, res):
    if h <= 0:
        raise Invalid('TooEarly')
    tx = parse_tx(raw)
    res['txid'] = tx.txid()
    if not tx.inputs:
        raise Invalid('NoInputs')
    if not tx.outputs:
        raise Invalid('NoOutputs')
    # 2. inputs
    if len(prevouts) != len(tx.inputs):
        raise Invalid('PrevoutCountMismatch')
    seen = set()
    for x in tx.inputs:
        op = x.outpoint()
        if x.prev_txid == b'\x00' * 32 and x.vout == 0xffffffff:
            raise Invalid('NullPrevout')
        if op in seen:
            raise Invalid('DuplicatePrevout')
        seen.add(op)
    for x in tx.inputs:
        if x.script_sig:
            raise Invalid('ScriptSigNotEmpty')
    for _, _, ph in prevouts:
        # No UTXO is ever stored at height 0 (3.1) or above the current one.
        if ph < 1 or ph > h:
            raise Invalid('BadPrevoutHeight')
    # 3. amounts
    for amount, _ in tx.outputs:
        if amount > MAX_MONEY:
            raise Invalid('OutputTooLarge')
    sum_in = sum(p[0] for p in prevouts)
    sum_out = sum(a for a, _ in tx.outputs)
    if sum_in > MAX_MONEY:
        raise Invalid('InputsTooLarge')
    if sum_out > MAX_MONEY:
        raise Invalid('OutputsTooLarge')
    if sum_in < sum_out:
        raise Invalid('OutputsExceedInputs')
    res['fee'] = sum_in - sum_out
    # 4. sigops
    if 4 * sum(legacy_sigops(s) for _, s in tx.outputs) + 4 * len(tx.inputs) > MAX_SIGOPS_COST:
        raise Invalid('TooManySigops')
    # 5. lock time
    if not is_final(tx, h, genesis_time, interval):
        raise Invalid('NonFinal')
    # 6. relative lock (BIP68), version as a uint32
    if tx.version >= 2:
        for x, (_, _, ph) in zip(tx.inputs, prevouts):
            if x.sequence & SEQ_DISABLE:
                continue
            if x.sequence & SEQ_TYPE:
                raise Invalid('RelativeTimeLockUnsupported')
            if h - ph < (x.sequence & SEQ_MASK):
                raise Invalid('RelativeLockNotMet')
    # 7-9. scripts and signatures
    sh = _Sighash(tx, prevouts)
    for i in range(len(tx.inputs)):
        check_input(tx, prevouts, i, sh)
    return tx


def validate(raw_tx, prevouts, height, genesis_time=GENESIS_TIME, interval=INTERVAL):
    """-> (ok, error name or None, txid (display hex) or None, fee or None)."""
    raw = bytes.fromhex(raw_tx) if isinstance(raw_tx, str) else bytes(raw_tx)
    res = {'txid': None, 'fee': None}
    try:
        _validate(raw, _norm_prevouts(prevouts), int(height), genesis_time, interval, res)
    except Invalid as e:
        return False, e.name, res['txid'], res['fee']
    return True, None, res['txid'], res['fee']


# ---------------------------------------------------------------- 3.3, 4.1, 4.4
def is_exit_script(spk):
    return len(spk) == 22 and spk[0] == 0x60 and spk[1] == 0x14


def deposit_prevout_txid(chain_id, contract, nonce, nonce_bytes=32):
    """keccak256(abi.encodePacked("evmchan deposit", block.chainid,
    address(this), depositNonce)).  depositNonce's width is not in the SPEC;
    uint256 (32 bytes) is assumed."""
    contract = bytes.fromhex(contract[2:] if contract.startswith('0x') else contract) \
        if isinstance(contract, str) else bytes(contract)
    assert len(contract) == 20
    return keccak256(b'evmchan deposit' + chain_id.to_bytes(32, 'big') + contract
                     + nonce.to_bytes(nonce_bytes, 'big'))


def deposit_tx(chain_id, contract, nonce, amount, script, nonce_bytes=32):
    """The synthetic deposit tx of 3.3 -> (raw bytes, txid display hex)."""
    script = bytes.fromhex(script) if isinstance(script, str) else bytes(script)
    prev = deposit_prevout_txid(chain_id, contract, nonce, nonce_bytes)
    tx = Tx(2, [TxIn(prev, 0, b'', SEQUENCE_FINAL)], [(amount, script)], 0)
    raw = tx.serialize(witness=False)
    return raw, tx.txid()


def check_deposit(value_wei, script, height):
    """4.2's checks on deposit(); -> (ok, error name or None)."""
    script = bytes.fromhex(script) if isinstance(script, str) else bytes(script)
    if height <= 0:
        return False, 'TooEarly'
    if value_wei % SAT_WEI:
        return False, 'ValueNotWholeSats'
    if value_wei < SAT_WEI:
        return False, 'ZeroDeposit'
    if value_wei // SAT_WEI > MAX_MONEY:
        return False, 'AmountTooLarge'
    if len(script) > MAX_DEPOSIT_SCRIPT:
        return False, 'ScriptTooLong'
    if is_exit_script(script):
        return False, 'ExitScript'
    if script[:1] == b'\x6a':
        return False, 'OpReturnScript'
    return True, None


def utxo_key(txid_internal, vout):
    return keccak256(txid_internal + struct.pack('>I', vout))


def utxo_commitment(amount, script, height):
    return keccak256(struct.pack('>Q', amount) + script + struct.pack('>I', height))


class Chain:
    """The contract's state (4.1) under 4.4, keyed as the contract keys it.
    For replaying sequences of txs; validate() is the stateless core."""

    def __init__(self, genesis_time=GENESIS_TIME, interval=INTERVAL, chain_id=31337,
                 contract=b'\xee' * 20):
        self.genesis_time, self.interval = genesis_time, interval
        self.chain_id, self.contract = chain_id, contract
        self.utxo = {}              # utxo_key -> commitment
        self.values = {}            # utxo_key -> (amount, script, height), for building prevouts
        self.withdrawable = {}      # address bytes -> wei
        self.deposit_nonce = 0

    def _store(self, txid_internal, vout, amount, script, h):
        k = utxo_key(txid_internal, vout)
        self.utxo[k] = utxo_commitment(amount, script, h)
        self.values[k] = (amount, script, h)

    def add_utxo(self, txid_hex, vout, amount, script, h):
        self._store(bytes.fromhex(txid_hex)[::-1], vout, amount, bytes.fromhex(script), h)

    def deposit(self, value_wei, script, h):
        ok, err = check_deposit(value_wei, script, h)
        if not ok:
            raise Invalid(err)
        script = bytes.fromhex(script) if isinstance(script, str) else bytes(script)
        raw, txid = deposit_tx(self.chain_id, self.contract, self.deposit_nonce,
                               value_wei // SAT_WEI, script)
        self.deposit_nonce += 1
        self._store(bytes.fromhex(txid)[::-1], 0, value_wei // SAT_WEI, script, h)
        return raw, txid

    def prevouts_for(self, raw):
        tx = parse_tx(bytes.fromhex(raw) if isinstance(raw, str) else raw)
        out = []
        for x in tx.inputs:
            v = self.values.get(utxo_key(x.prev_txid, x.vout))
            if v is None:
                raise Invalid('UnknownPrevout')
            out.append({'amount': v[0], 'script': v[1].hex(), 'height': v[2]})
        return out

    def submit(self, raw, h, sender=b'\x00' * 20, prevouts=None):
        """Validate against the stored commitments, then apply (4.4)."""
        raw = bytes.fromhex(raw) if isinstance(raw, str) else bytes(raw)
        tx = parse_tx(raw)
        if prevouts is None:
            prevouts = self.prevouts_for(raw)
        pv = _norm_prevouts(prevouts)
        if len(pv) == len(tx.inputs):
            for x, (a, s, ph) in zip(tx.inputs, pv):
                if self.utxo.get(utxo_key(x.prev_txid, x.vout)) != utxo_commitment(a, s, ph):
                    raise Invalid('UnknownPrevout')
        ok, err, txid, fee = validate(raw, prevouts, h, self.genesis_time, self.interval)
        if not ok:
            raise Invalid(err)
        for x in tx.inputs:
            k = utxo_key(x.prev_txid, x.vout)
            del self.utxo[k]
            del self.values[k]
        tid = tx.txid_bytes()
        for vout, (amount, spk) in enumerate(tx.outputs):
            if is_exit_script(spk):
                addr = spk[2:]
                self.withdrawable[addr] = self.withdrawable.get(addr, 0) + amount * SAT_WEI
            elif spk[:1] == b'\x6a':
                pass
            else:
                self._store(tid, vout, amount, spk, h)
        self.withdrawable[sender] = self.withdrawable.get(sender, 0) + fee * SAT_WEI
        return txid, fee
