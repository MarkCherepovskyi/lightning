# SPDX-License-Identifier: MIT
"""Generate the Solidity test vectors of the UtxoChain contract (test/generated/
of the Foundry project in contracts/evm).

Usage (with a Python that has coincurve):
  python -I contrib/evmchan/gen_vectors.py [--contracts DIR] [--vectors DIR] [--reference DIR]

Reads:
  - every *.jsonl in --vectors (default tests/vectors): real CLN
    transactions, one JSON object per line: {tx, txid, height, prevouts: [{amount,
    script, height} | {mint: true}], kind?}. Each file is replayed in order through
    the real contract (RealVectors.sol).
  - test/data/bip340-vectors.json, test/data/bip341-keypath.json: the official
    BIP340/BIP341 vectors (BipVectors.sol).
  - every *.jsonl in --reference (default reference/cases), if the directory
    exists: differential cases {tx, prevouts, height, valid, error?} from an
    independent reference (ReferenceCases.sol; empty when there are none).
Writes test/generated/{RealVectors,Synthetic,BipVectors,ReferenceCases}.sol.

Everything the contract is asked to accept is first checked here by `model()`,
an implementation of SPEC 4.3 in Python (coincurve for the signatures) that also
predicts the contract's error for every rejected case, in the contract's order
of checks. Hand-written bad cases state their expected error and the generator
asserts the model agrees, so the two are checked against each other.
"""
import argparse
import glob
import hashlib
import importlib.util
import json
import os
import random
import struct

from coincurve import PrivateKey, PublicKey, PublicKeyXOnly

EVMCHAN = os.path.dirname(os.path.abspath(__file__))
# The Foundry project (--contracts); main() repoints these at it.
ROOT = os.path.normpath(os.path.join(EVMCHAN, "..", "..", "contracts", "evm"))
OUT = os.path.join(ROOT, "test", "generated")
DATA = os.path.join(ROOT, "test", "data")

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
MAX_MONEY = 21_000_000 * 100_000_000
FINAL = 0xFFFFFFFF
DISABLE = 1 << 31
TYPE = 1 << 22
THRESHOLD = 500_000_000

# The clock of spike S1's toy chain (chaind.py), which the real vectors were made
# on: block h has time GENESIS_TIME + h * INTERVAL. The tests deploy the
# contract at GENESIS_TIME with this INTERVAL.
GENESIS_TIME = 1700000000
INTERVAL = 600


def mtp(h, g=GENESIS_TIME, i=INTERVAL):
    """SPEC 4.3.5: MTP(h) = T(max(h - 6, 0))."""
    return g + max(h - 6, 0) * i


# ---------------------------------------------------------------- primitives

def sha256(b):
    return hashlib.sha256(b).digest()


def dsha(b):
    return sha256(sha256(b))


def hash160(b):
    return hashlib.new("ripemd160", sha256(b)).digest()


def tagged(tag, data):
    t = sha256(tag.encode())
    return sha256(t + t + data)


def cs(n):
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def _load_keccak():
    for path in (os.path.join(EVMCHAN, "evm.py"),):
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("evmchan_evm", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.keccak256
    raise SystemExit("no keccak256: need contrib/evmchan/evm.py")


keccak256 = _load_keccak()


class Tx:
    """ins: (txid in wire order, vout, scriptSig, sequence); outs: (amount, spk)."""

    def __init__(self, version, ins, outs, locktime, wit=None):
        self.version, self.ins, self.outs, self.locktime = version, ins, outs, locktime
        self.wit = wit if wit is not None else [[] for _ in ins]

    def ser(self, witness=True):
        witness = witness and any(self.wit)
        b = struct.pack("<I", self.version) + (b"\x00\x01" if witness else b"")
        b += cs(len(self.ins)) + b"".join(t + struct.pack("<I", v) + cs(len(ss)) + ss + struct.pack("<I", q)
                                          for t, v, ss, q in self.ins)
        b += cs(len(self.outs)) + b"".join(ser_out(a, s) for a, s in self.outs)
        if witness:
            b += b"".join(cs(len(w)) + b"".join(cs(len(i)) + i for i in w) for w in self.wit)
        return b + struct.pack("<I", self.locktime)

    def txid(self):
        """Internal byte order (as in an outpoint)."""
        return dsha(self.ser(False))

    def copy(self):
        return Tx(self.version, list(self.ins), list(self.outs), self.locktime, [list(w) for w in self.wit])


def ser_out(amt, spk):
    return struct.pack("<Q", amt) + cs(len(spk)) + spk


class Reject(Exception):
    """The contract's revert: an error name and, for input errors, the index."""

    def __init__(self, name, *args):
        super().__init__(name, *args)
        self.name, self.args_ = name, args

    def key(self):
        return (self.name,) + tuple(self.args_)


# ---------------------------------------------------------------- strict parser (BtcTx.parse)

def parse(raw):
    """BtcTx.parse in Python, raising Reject with the contract's error."""
    n_ = len(raw)
    if n_ > 100_000:
        raise Reject("TxTooLarge")

    def u8(o):
        if o >= n_:
            raise Reject("Truncated")
        return raw[o]

    def le(o, k):
        if o + k > n_:
            raise Reject("Truncated")
        return int.from_bytes(raw[o:o + k], "little")

    def csize(o):
        f = u8(o)
        if f < 0xFD:
            return f, o + 1
        k = {0xFD: 2, 0xFE: 4, 0xFF: 8}[f]
        v = le(o + 1, k)
        if v < {2: 0xFD, 4: 0x10000, 8: 0x100000000}[k]:
            raise Reject("NonCanonicalCompactSize")
        return v, o + 1 + k

    version = le(0, 4)
    off = 4
    segwit = False
    if u8(4) == 0:
        if u8(5) != 1:
            raise Reject("BadSegwitFlag")
        segwit, off = True, 6
    n, off = csize(off)
    if n == 0:
        raise Reject("NoInputs")
    if n > (n_ - off) // 41:
        raise Reject("Truncated")
    ins = []
    for i in range(n):
        if u8(off + 36) != 0:
            raise Reject("ScriptSigNotEmpty", i)
        ins.append((raw[off:off + 32], le(off + 32, 4), b"", le(off + 37, 4)))
        off += 41
    n, off = csize(off)
    if n == 0:
        raise Reject("NoOutputs")
    if n > (n_ - off) // 9:
        raise Reject("Truncated")
    outs = []
    for _ in range(n):
        amt = le(off, 8)
        sl, o = csize(off + 8)
        if sl > n_ - o:
            raise Reject("Truncated")
        outs.append((amt, raw[o:o + sl]))
        off = o + sl
    wit = [[] for _ in ins]
    if segwit:
        anyw = False
        for i in range(len(ins)):
            items, off = csize(off)
            if items > n_ - off:
                raise Reject("Truncated")
            anyw = anyw or items != 0
            for _ in range(items):
                il, off = csize(off)
                if il > n_ - off:
                    raise Reject("Truncated")
                wit[i].append(raw[off:off + il])
                off += il
        if not anyw:
            raise Reject("EmptyWitnesses")
    if off + 4 > n_:
        raise Reject("Truncated")
    if off + 4 != n_:
        raise Reject("TrailingBytes")
    return Tx(version, ins, outs, le(off, 4), wit)


def lenient_parse(raw):
    """The inputs of a tx the strict parser may reject, for seeding; None if unreadable."""
    try:
        return parse(raw)
    except Reject:
        pass
    try:
        o = 6 if raw[4] == 0 else 4
        n = raw[o]
        o += 1
        ins = []
        for _ in range(n):
            ins.append((raw[o:o + 32], int.from_bytes(raw[o + 32:o + 36], "little"), b"", 0))
            o += 41 + raw[o + 36]
        return Tx(0, ins, [], 0)
    except Exception:
        return None


# ---------------------------------------------------------------- sighashes

def bip143(tx, idx, script_code, amount, ht):
    """BIP143 sighash. SINGLE with idx >= len(outs) uses a zero hashOutputs (BIP143)."""
    base, acp = ht & 0x1F, ht & 0x80
    zero = b"\x00" * 32
    hp = zero if acp else dsha(b"".join(t + struct.pack("<I", v) for t, v, _, _ in tx.ins))
    hs = zero if acp or base in (2, 3) else dsha(b"".join(struct.pack("<I", q) for _, _, _, q in tx.ins))
    if base not in (2, 3):
        ho = dsha(b"".join(ser_out(a, s) for a, s in tx.outs))
    elif base == 3 and idx < len(tx.outs):
        ho = dsha(ser_out(*tx.outs[idx]))
    else:
        ho = zero
    t, v, _, q = tx.ins[idx]
    pre = (struct.pack("<I", tx.version) + hp + hs + t + struct.pack("<I", v) + cs(len(script_code)) + script_code
           + struct.pack("<Q", amount) + struct.pack("<I", q) + ho + struct.pack("<II", tx.locktime, ht))
    return dsha(pre)


def bip341_msg(tx, spent, idx, ht):
    """BIP341 key-path sigMsg with the epoch byte (ext_flag 0, no annex)."""
    base, acp = ht & 3, ht & 0x80
    m = bytes([0, ht]) + struct.pack("<II", tx.version, tx.locktime)
    if not acp:
        m += sha256(b"".join(t + struct.pack("<I", v) for t, v, _, _ in tx.ins))
        m += sha256(b"".join(struct.pack("<Q", a) for a, _ in spent))
        m += sha256(b"".join(cs(len(s)) + s for _, s in spent))
        m += sha256(b"".join(struct.pack("<I", q) for _, _, _, q in tx.ins))
    if base not in (2, 3):
        m += sha256(b"".join(ser_out(a, s) for a, s in tx.outs))
    m += b"\x00"
    if acp:
        t, v, _, q = tx.ins[idx]
        m += t + struct.pack("<I", v) + ser_out(*spent[idx]) + struct.pack("<I", q)
    else:
        m += struct.pack("<I", idx)
    if base == 3:
        m += sha256(ser_out(*tx.outs[idx]))
    return m


def bip341(tx, spent, idx, ht):
    return tagged("TapSighash", bip341_msg(tx, spent, idx, ht))


# ---------------------------------------------------------------- signatures

def parse_der(sig):
    """BIP66 strict DER (sig includes the hash type byte). Returns (r, s, hashtype) or None."""
    n = len(sig)
    if n < 9 or n > 73 or sig[0] != 0x30 or sig[1] != n - 3:
        return None
    lr = sig[3]
    if 5 + lr >= n:
        return None
    ls = sig[5 + lr]
    if lr + ls + 7 != n or sig[2] != 2 or lr == 0 or sig[4] & 0x80:
        return None
    if lr > 1 and sig[4] == 0 and not sig[5] & 0x80:
        return None
    if sig[lr + 4] != 2 or ls == 0 or sig[lr + 6] & 0x80:
        return None
    if ls > 1 and sig[lr + 6] == 0 and not sig[lr + 7] & 0x80:
        return None
    r = int.from_bytes(sig[4:4 + lr], "big")
    s = int.from_bytes(sig[6 + lr:6 + lr + ls], "big")
    return r, s, sig[-1]


def ecdsa_ok(pub33, digest, sig):
    try:
        return PublicKey(pub33).verify(sig[:-1], digest, hasher=None)
    except Exception:
        return False


def schnorr_ok(xonly, digest, sig64):
    try:
        return PublicKeyXOnly(xonly).verify(sig64, digest)
    except Exception:
        return False


def sign_ecdsa(key, digest, ht):
    return key.sign(digest, hasher=None) + bytes([ht])


def xonly(key):
    return key.public_key.format()[1:]


# ---------------------------------------------------------------- CLN scripts (bitcoin/script.c)

def push(b):
    assert len(b) < 76
    return bytes([len(b)]) + b


def num(n):
    """add_number() in bitcoin/script.c."""
    if n == 0:
        return b"\x00"
    if n <= 16:
        return bytes([0x50 + n])
    for k, lim in ((1, 0x7F), (2, 0x7FFF), (3, 0x7FFFFF), (4, 0x7FFFFFFF), (5, 0xFFFFFFFF)):
        if n <= lim:
            return push(n.to_bytes(8, "little")[:k])


def read_num(s, o):
    """Inverse of num() at s[o]: (value, next offset), or None if not as num() makes it."""
    if o >= len(s):
        return None
    op = s[o]
    if op == 0:
        return 0, o + 1
    if 0x51 <= op <= 0x60:
        return op - 0x50, o + 1
    if 1 <= op <= 5 and o + 1 + op <= len(s):
        v = int.from_bytes(s[o + 1:o + 1 + op], "little")
        if s[o + op] & 0x80:
            return None
        return (v, o + 1 + op) if num(v) == s[o:o + 1 + op] else None
    return None


def redeem_2of2(k1, k2):
    a, b = sorted([k1, k2])
    return b"\x52" + push(a) + push(b) + b"\x52\xae"


def wscript_to_local(delay, rev, delayed):
    return b"\x63" + push(rev) + b"\x67" + num(delay) + b"\xb2\x75" + push(delayed) + b"\x68\xac"


def wscript_to_remote_anchored(rk, csv=1):
    return push(rk) + b"\xad" + num(csv) + b"\xb2"


def wscript_anchor(fk):
    return push(fk) + b"\xac\x73\x64" + num(16) + b"\xb2\x68"


def wscript_htlc_offer(lk, rk, ph_ripemd, revh):
    return (b"\x76\xa9" + push(revh) + b"\x87\x63\xac\x67" + push(rk) + b"\x7c\x82" + num(32) + b"\x87"
            + b"\x64\x75" + num(2) + b"\x7c" + push(lk) + num(2) + b"\xae"
            + b"\x67\xa9" + push(ph_ripemd) + b"\x88\xac\x68" + num(1) + b"\xb2\x75\x68")


def wscript_htlc_receive(cltv, lk, rk, ph_ripemd, revh):
    return (b"\x76\xa9" + push(revh) + b"\x87\x63\xac\x67" + push(rk) + b"\x7c\x82" + num(32) + b"\x87"
            + b"\x63\xa9" + push(ph_ripemd) + b"\x88" + num(2) + b"\x7c" + push(lk) + num(2) + b"\xae"
            + b"\x67\x75" + num(cltv) + b"\xb1\x75\xac\x68" + num(1) + b"\xb2\x75\x68")


def classify(ws):
    """Recognise a template exactly as Templates.p2wsh does: (kind, fields) or (None, {})."""
    if len(ws) == 71 and ws[0] == 0x52:
        k1, k2 = ws[2:35], ws[36:69]
        if k1 <= k2 and b"\x52" + push(k1) + push(k2) + b"\x52\xae" == ws:
            return "funding", {"k1": k1, "k2": k2}
        return None, {}
    if len(ws) and ws[0] == 0x63:
        if len(ws) >= 75 and ws[1] == 33:
            r = read_num(ws, 36)
            if r and r[1] + 38 == len(ws):
                f = {"delay": r[0], "rev": ws[2:35], "delayed": ws[r[1] + 3:r[1] + 36]}
                if wscript_to_local(f["delay"], f["rev"], f["delayed"]) == ws:
                    return "to_local", f
        return None, {}
    if len(ws) == 40 and ws[0] == 0x21:
        return ("anchor", {"key": ws[1:34]}) if wscript_anchor(ws[1:34]) == ws else (None, {})
    if len(ws) and ws[0] == 0x21:
        if len(ws) == 37 and wscript_to_remote_anchored(ws[1:34], 1) == ws:
            return "to_remote", {"key": ws[1:34]}
        return None, {}
    if len(ws) and ws[0] == 0x76:
        if len(ws) == 136:
            f = {"revh": ws[3:23], "rk": ws[28:61], "lk": ws[71:104], "ph": ws[109:129]}
            if wscript_htlc_offer(f["lk"], f["rk"], f["ph"], f["revh"]) == ws:
                return "htlc_offered", f
        if len(ws) >= 139:
            r = read_num(ws, 130)
            if r and r[1] + 8 == len(ws):
                f = {"revh": ws[3:23], "rk": ws[28:61], "ph": ws[69:89], "lk": ws[93:126], "cltv": r[0]}
                if wscript_htlc_receive(f["cltv"], f["lk"], f["rk"], f["ph"], f["revh"]) == ws:
                    return "htlc_received", f
    return None, {}


# ---------------------------------------------------------------- the model of SPEC 4.3 (and the contract's order)

def legacy_sigops(s):
    n, i = 0, 0
    while i < len(s):
        op = s[i]
        i += 1
        if op in (0xAC, 0xAD):
            n += 1
        elif op in (0xAE, 0xAF):
            n += 20
        elif 0x01 <= op <= 0x4E:
            size = op
            if op >= 0x4C:
                k = {0x4C: 1, 0x4D: 2, 0x4E: 4}[op]
                if i + k > len(s):
                    break
                size = int.from_bytes(s[i:i + k], "little")
                i += k
            if size > len(s) - i:
                break
            i += size
    return n


def is_exit(spk):
    return len(spk) == 22 and spk[0] == 0x60 and spk[1] == 0x14


def is_op_return(spk):
    return len(spk) > 0 and spk[0] == 0x6A


def commitment(amount, spk, height):
    return keccak256(struct.pack(">Q", amount) + spk + struct.pack(">I", height))


def utxo_key(txid, vout):
    return keccak256(txid + struct.pack(">I", vout))


class Model:
    """Validates a tx as UtxoChain.submit does, raising Reject with its error."""

    def __init__(self, tx, pv, h, g=GENESIS_TIME, i=INTERVAL):
        self.tx, self.pv, self.h, self.mtp = tx, pv, h, mtp(h, g, i)
        self.spent = [(p["amount"], bytes.fromhex(p["script"])) for p in pv]

    def tx_level(self):
        tx, pv, h = self.tx, self.pv, self.h
        if len(pv) != len(tx.ins):
            raise Reject("PrevoutCountMismatch")
        for i, (t, v, _, _) in enumerate(tx.ins):
            if t == b"\x00" * 32 and v == FINAL:
                raise Reject("NullPrevout", i)
        total = 0
        for p in pv:
            if p["amount"] > MAX_MONEY:
                raise Reject("AmountTooLarge")
            total += p["amount"]
        out = 0
        for a, _ in tx.outs:
            if a > MAX_MONEY:
                raise Reject("AmountTooLarge")
            out += a
        if total > MAX_MONEY or out > MAX_MONEY:
            raise Reject("AmountTooLarge")
        if out > total:
            raise Reject("OutputsExceedInputs")
        if 4 * sum(legacy_sigops(s) for _, s in tx.outs) + 4 * len(tx.ins) > 16_000:
            raise Reject("TooManySigops")
        lt = tx.locktime
        if not (lt < h if lt < THRESHOLD else lt < self.mtp):
            if any(q != FINAL for _, _, _, q in tx.ins):
                raise Reject("NonFinal")
        if tx.version >= 2:
            for i, ((_, _, _, q), p) in enumerate(zip(tx.ins, pv)):
                if q & DISABLE:
                    continue
                if q & TYPE:
                    raise Reject("TimeRelativeLockUnsupported", i)
                if h < p["height"] + (q & 0xFFFF):
                    raise Reject("RelativeLockNotMet", i)
        return total - out

    def scripts(self):
        """Every input's script; returns per input (kind, path, [hash types])."""
        return [self.input(i) for i in range(len(self.tx.ins))]

    def input(self, i):
        tx = self.tx
        amount, spk = self.spent[i]
        w = tx.wit[i]
        if len(w) > 5:
            raise Reject("BadWitness", i)
        if len(spk) == 22 and spk[:2] == b"\x00\x14":
            if len(w) != 2 or len(w[1]) != 33:
                raise Reject("BadWitness", i)
            if hash160(w[1]) != spk[2:]:
                raise Reject("KeyHashMismatch", i)
            return "p2wpkh", "key", [self.sig(i, w[0], w[1], b"\x76\xa9\x14" + spk[2:] + b"\x88\xac")]
        if len(spk) == 34 and spk[:2] == b"\x00\x20":
            return self.p2wsh(i, spk[2:], w)
        if len(spk) == 34 and spk[:2] == b"\x51\x20":
            if len(w) != 1:
                raise Reject("BadWitness", i)
            sig = w[0]
            ht = 0
            if len(sig) == 65:
                ht = sig[64]
                if ht == 0 or not (ht <= 3 or 0x81 <= ht <= 0x83):
                    raise Reject("BadSignatureEncoding", i)
            elif len(sig) != 64:
                raise Reject("BadSignatureEncoding", i)
            if ht & 3 == 3 and i >= len(tx.outs):
                raise Reject("NoMatchingOutput", i)
            px = int.from_bytes(spk[2:], "big")
            rx = int.from_bytes(sig[:32], "big")
            s = int.from_bytes(sig[32:64], "big")
            # the contract's extra rejections (Schnorr.sol), none reachable by an honest signer
            if px == 0 or px >= N or rx >= P or s >= N or s == 0:
                raise Reject("BadSignature", i)
            if not schnorr_ok(spk[2:], bip341(tx, self.spent, i, ht), sig[:64]):
                raise Reject("BadSignature", i)
            return "p2tr", "key", [ht]
        raise Reject("UnsupportedScriptPubKey", i)

    def sig(self, i, sig, pub, code):
        d = parse_der(sig)
        if d is None or not 0 < d[0] < N or not 0 < d[1] <= N // 2 or d[2] not in (1, 2, 3, 0x81, 0x82, 0x83):
            raise Reject("BadSignatureEncoding", i)
        self.key(i, pub)
        digest = bip143(self.tx, i, code, self.spent[i][0], d[2])
        if not ecdsa_ok(pub, digest, sig):
            raise Reject("BadSignature", i)
        return d[2]

    def key(self, i, pub):
        try:
            ok = len(pub) == 33 and pub[0] in (2, 3) and PublicKey(pub) is not None
        except Exception:
            ok = False
        if not ok:
            raise Reject("BadPublicKey", i)

    def csv(self, i, n):
        q = self.tx.ins[i][3]
        if self.tx.version < 2 or q & DISABLE or q & TYPE or (q & 0xFFFF) < n:
            raise Reject("CsvNotMet", i)

    def cltv(self, i, x):
        lt = self.tx.locktime
        if (x < THRESHOLD) != (lt < THRESHOLD) or lt < x:
            raise Reject("CltvNotMet", i)
        if self.tx.ins[i][3] == FINAL:
            raise Reject("CltvNotMet", i)

    def p2wsh(self, i, program, w):
        if len(w) == 0:
            raise Reject("BadWitness", i)
        ws = w[-1]
        if sha256(ws) != program:
            raise Reject("WitnessScriptMismatch", i)
        kind, f = classify(ws)
        st = w[:-1]
        if kind == "funding":
            if len(st) != 3 or st[0] != b"":
                raise Reject("BadWitness", i)
            return kind, "2-of-2", [self.sig(i, st[1], f["k1"], ws), self.sig(i, st[2], f["k2"], ws)]
        if kind == "to_local":
            if len(st) != 2:
                raise Reject("BadWitness", i)
            if st[1] == b"":
                self.csv(i, f["delay"])
                return kind, "delayed (CSV %d)" % f["delay"], [self.sig(i, st[0], f["delayed"], ws)]
            if st[1] == b"\x01":
                return kind, "revocation", [self.sig(i, st[0], f["rev"], ws)]
            raise Reject("BadWitness", i)
        if kind == "to_remote":
            if len(st) != 1:
                raise Reject("BadWitness", i)
            hts = [self.sig(i, st[0], f["key"], ws)]
            self.csv(i, 1)
            return kind, "key (CSV 1)", hts
        if kind == "anchor":
            if len(st) != 1:
                raise Reject("BadWitness", i)
            if st[0] == b"":
                self.key(i, f["key"])
                self.csv(i, 16)
                return kind, "anyone (CSV 16)", []
            return kind, "key", [self.sig(i, st[0], f["key"], ws)]
        if kind in ("htlc_offered", "htlc_received"):
            offered = kind == "htlc_offered"
            if len(st) == 0:
                raise Reject("BadWitness", i)
            x = st[-1]
            if hash160(x) == f["revh"]:
                if len(st) != 2:
                    raise Reject("BadWitness", i)
                return kind, "revocation", [self.sig(i, st[0], x, ws)]
            preimage = len(x) == 32
            if not preimage and len(x) != 0:
                raise Reject("BadWitness", i)
            if preimage and hash160(x) != f["ph"]:
                raise Reject("BadPreimage", i)
            if preimage != offered:
                if len(st) != 4 or st[0] != b"":
                    raise Reject("BadWitness", i)
                hts = [self.sig(i, st[1], f["rk"], ws), self.sig(i, st[2], f["lk"], ws)]
                path = "HTLC-success tx" if preimage else "HTLC-timeout tx"
            else:
                if len(st) != 2:
                    raise Reject("BadWitness", i)
                if not offered:
                    self.cltv(i, f["cltv"])
                hts = [self.sig(i, st[0], f["rk"], ws)]
                path = "remote success" if preimage else "remote timeout (CLTV %d)" % f["cltv"]
            self.csv(i, 1)
            return kind, path, hts
        raise Reject("UnknownWitnessScript", i)


def model(raw, pv, h, seeds=None, g=GENESIS_TIME, i=INTERVAL):
    """The contract's verdict on submitting raw at height h: None (valid) or the
    Reject key. `seeds`: the UTXO set as {key: commitment} (default: the prevouts
    at the tx's outpoints, as the tests seed them)."""
    try:
        if h == 0:
            raise Reject("TooEarly")
        tx = parse(raw)
        if len(pv) != len(tx.ins):
            raise Reject("PrevoutCountMismatch")
        if seeds is None:
            seeds = {}
            for (t, v, _, _), p in zip(tx.ins, pv):
                seeds[utxo_key(t, v)] = commitment(p["amount"], bytes.fromhex(p["script"]), p["height"])
        utxo = dict(seeds)
        for j, ((t, v, _, _), p) in enumerate(zip(tx.ins, pv)):
            k = utxo_key(t, v)
            if utxo.get(k) != commitment(p["amount"], bytes.fromhex(p["script"]), p["height"]):
                if any(tx.ins[x][:2] == (t, v) for x in range(j)):
                    raise Reject("DuplicateInput", j)
                raise Reject("UnknownUtxo", j)
            del utxo[k]
        m = Model(tx, pv, h, g, i)
        m.tx_level()
        m.scripts()
        return None
    except Reject as e:
        return e.key()


def describe(tx, pv, h):
    m = Model(tx, pv, h)
    m.tx_level()
    return m.scripts()


def min_height(tx, pv, g=GENESIS_TIME, i=INTERVAL):
    """The lowest height >= 1 at which the tx's lock time and BIP68 locks are met."""
    h = 1
    if any(q != FINAL for _, _, _, q in tx.ins):
        lt = tx.locktime
        if lt < THRESHOLD:
            h = max(h, lt + 1)
        elif lt >= g:
            h = max(h, (lt - g) // i + 1 + 6)
    if tx.version >= 2:
        for (_, _, _, q), p in zip(tx.ins, pv):
            if not q & DISABLE:
                assert not q & TYPE, "time-based relative lock"
                h = max(h, p["height"] + (q & 0xFFFF))
    return h


def tx_level_error(tx, pv, h):
    try:
        Model(tx, pv, h).tx_level()
        return None
    except Reject as e:
        return e.key()


def tx_kind(tx, notes):
    kinds = [k for k, _, _ in notes]
    if kinds == ["funding"]:
        if tx.locktime >> 24 == 0x20:
            return "commitment"
        return "mutual-close"
    if "htlc_offered" in kinds or "htlc_received" in kinds:
        return "htlc"
    if "to_local" in kinds:
        return "to_local-sweep"
    return "wallet"


# ---------------------------------------------------------------- Solidity output helpers

def sol_err(key):
    name, args = key[0], key[1:]
    a = "".join(", uint256(%d)" % x for x in args)
    return "abi.encodeWithSelector(E.%s.selector%s)" % (name, a)


def sol_str(s):
    return s.replace("\\", "\\\\").replace('"', "'")


def hexlit(b):
    return 'hex"%s"' % b.hex()


def emit_prevouts(emit, var, pv):
    emit("            %s = new Prevout[](%d);" % (var, len(pv)))
    for i, p in enumerate(pv):
        emit('            %s[%d] = Prevout(%d, hex"%s", %d);' % (var, i, p["amount"], p["script"], p["height"]))


def emit_seeds(emit, prefix, seeds):
    """seeds: list of (txid internal, vout, prevout dict)."""
    emit("            %sPv = new Prevout[](%d);" % (prefix, len(seeds)))
    emit("            %sTxids = new bytes32[](%d);" % (prefix, len(seeds)))
    emit("            %sVouts = new uint32[](%d);" % (prefix, len(seeds)))
    for i, (t, v, p) in enumerate(seeds):
        emit('            %sPv[%d] = Prevout(%d, hex"%s", %d);' % (prefix, i, p["amount"], p["script"], p["height"]))
        emit("            %sTxids[%d] = 0x%s;" % (prefix, i, t.hex()))
        emit("            %sVouts[%d] = %d;" % (prefix, i, v))


HEADER = """// SPDX-License-Identifier: MIT
// Generated by contrib/evmchan/gen_vectors.py. Do not edit.
pragma solidity ^0.8.24;
"""


# ---------------------------------------------------------------- cases

class Case:
    """A valid tx: submitted at `height` after seeding the inputs marked in `seed`."""

    def __init__(self, name, kind, tx, pv, height, seed=None, require_early=False):
        self.name, self.kind, self.tx, self.pv, self.height = name, kind, tx, pv, height
        self.seed = seed if seed is not None else [True] * len(tx.ins)
        r = model(tx.ser(), pv, height)
        assert r is None, (name, r)
        self.notes = describe(tx, pv, height)
        self.min_h = min_height(tx, pv)
        assert self.min_h <= height, (name, self.min_h, height)
        self.early = tx_level_error(tx, pv, self.min_h - 1) if self.min_h >= 2 else None
        assert self.early is not None or self.min_h < 2, name
        if require_early:
            assert self.early is not None, name
        self.fee = sum(p["amount"] for p in pv) - sum(a for a, _ in tx.outs)


class Bad:
    """A tx the contract must reject with `err` at `height`."""

    def __init__(self, name, raw, pv, height, err, seeds=None):
        if isinstance(raw, Tx):
            raw = raw.ser()
        self.name, self.raw, self.pv, self.height = name, raw, pv, height
        if seeds is None:
            t = lenient_parse(raw)
            seeds = [(i[0], i[1], p) for i, p in zip(t.ins, pv)] if t else []
        self.seeds = seeds
        smap = {utxo_key(t, v): commitment(p["amount"], bytes.fromhex(p["script"]), p["height"]) for t, v, p in seeds}
        got = model(raw, pv, height, seeds=smap)
        if err is None:
            err = got
        assert got == err, (name, "model says", got, "expected", err)
        assert err is not None, name
        self.err = err


def pv_of(amount, spk, height):
    return {"amount": amount, "script": spk.hex(), "height": height}


# ---------------------------------------------------------------- real vectors

def load_jsonl(path):
    return [json.loads(line) for line in open(path) if line.strip()]


def real_files(vdir):
    files = []
    for path in sorted(glob.glob(os.path.join(vdir, "*.jsonl"))):
        rows = load_jsonl(path)
        created = {}
        cases = []
        for n, e in enumerate(rows):
            raw = bytes.fromhex(e["tx"])
            tx = parse(raw)
            assert tx.ser() == raw
            assert tx.txid()[::-1].hex() == e["txid"], (path, n)
            if any(p.get("mint") for p in e["prevouts"]):
                continue
            pv = [{"amount": p["amount"], "script": p["script"], "height": p["height"]} for p in e["prevouts"]]
            seed = []
            for (t, v, _, _), p in zip(tx.ins, pv):
                c = created.pop((t, v), None)
                if c is not None:
                    assert c == (p["amount"], p["script"], p["height"]), (path, n, "prevout differs from its parent")
                seed.append(c is None)
            notes = describe(tx, pv, e["height"])
            kind = e.get("kind") or tx_kind(tx, notes)
            c = Case("%s #%d %s" % (os.path.basename(path), n, kind), kind, tx, pv, e["height"], seed)
            cases.append(c)
            for j, (a, s) in enumerate(tx.outs):
                if not is_exit(s) and not is_op_return(s):
                    created[(tx.txid(), j)] = (a, s.hex(), e["height"])
        files.append((os.path.basename(path), cases))
    return files


# ---------------------------------------------------------------- tampered copies of real txs (from spike S3)

def first_sig_pos(tx, idx, pv):
    """Index in the witness of input idx of its first signature, or None."""
    spk = bytes.fromhex(pv[idx]["script"])
    w = tx.wit[idx]
    if spk[:2] == b"\x00\x20":
        kind, _ = classify(w[-1])
        if kind == "funding" or len(w) == 5:
            return 1
        if kind == "anchor" and w[0] == b"":
            return None
    return 0


def flip_sig(tx, idx, pos):
    t = tx.copy()
    sig = bytearray(t.wit[idx][pos])
    sig[63 if sig[0] != 0x30 else len(sig) - 2] ^= 1
    t.wit[idx][pos] = bytes(sig)
    return t


def der(r, s, ht):
    def i(v):
        b = v.to_bytes(33, "big").lstrip(b"\x00")
        return b"\x00" + b if b[0] & 0x80 else b
    body = b"\x02" + bytes([len(i(r))]) + i(r) + b"\x02" + bytes([len(i(s))]) + i(s)
    return b"\x30" + bytes([len(body)]) + body + bytes([ht])


def tampered_real(c, rng, first):
    """Tampered copies of a real tx. The expected errors come from the model;
    the comments say what each must be for the usual case."""
    out = []
    tx, pv, h = c.tx, c.pv, c.height
    nm = c.name
    pos = first_sig_pos(tx, 0, pv)
    spk0 = bytes.fromhex(pv[0]["script"])
    if pos is not None:
        out.append(Bad(nm + ": flipped signature byte", flip_sig(tx, 0, pos), pv, h, ("BadSignature", 0)))
    t = tx.copy()
    t.outs[0] = (t.outs[0][0] - 1, t.outs[0][1]) if t.outs[0][0] else (1, t.outs[0][1])
    out.append(Bad(nm + ": output 0 amount changed", t, pv, h, None))  # BadSignature of the first input covering it
    t = tx.copy()
    t.outs[0] = (t.outs[0][0] + sum(p["amount"] for p in pv), t.outs[0][1])
    out.append(Bad(nm + ": outputs exceed inputs", t, pv, h, ("OutputsExceedInputs",)))
    q = [dict(p) for p in pv]
    q[0]["amount"] += 1
    out.append(Bad(nm + ": prevout amount + 1 (seeded so)", tx.ser(), q, h, None))  # BadSignature (anchor <>: later)
    q = [dict(p) for p in pv]
    bad = bytearray(spk0)
    bad[-1] ^= 1
    q[0]["script"] = bad.hex()
    out.append(Bad(nm + ": prevout script changed (seeded so)", tx.ser(), q, h, None))
    q = [dict(p) for p in pv]
    q[0]["height"] += 1
    out.append(Bad(nm + ": prevout height not the UTXO's", tx.ser(), q, h + 1, ("UnknownUtxo", 0),
                   seeds=[(i[0], i[1], p) for i, p in zip(tx.ins, pv)]))
    # swap a key: a different key in the script (and the prevout committing to it)
    t, q = tx.copy(), [dict(p) for p in pv]
    w = t.wit[0]
    other = PrivateKey(rng.randbytes(32)).public_key.format()
    if len(spk0) == 22:
        w[1] = other
        q[0]["script"] = "0014" + hash160(other).hex()
        out.append(Bad(nm + ": other key (P2WPKH)", t, q, h, ("BadSignature", 0)))
    elif spk0[:2] == b"\x00\x20":
        ws = w[-1]
        k, f = classify(ws)
        nws = None
        if k == "funding":
            nws = b"\x52" + push(f["k2"]) + push(f["k1"]) + b"\x52\xae"  # key order swapped: not script.c's
        elif k == "to_local":
            nws = wscript_to_local(f["delay"], f["delayed"], f["rev"])  # revocation and delayed keys swapped
        elif k == "htlc_offered":
            nws = wscript_htlc_offer(f["rk"], f["lk"], f["ph"], f["revh"])  # local and remote swapped
        elif k == "htlc_received":
            nws = wscript_htlc_receive(f["cltv"], f["rk"], f["lk"], f["ph"], f["revh"])
        if nws is not None:
            w[-1] = nws
            q[0]["script"] = (b"\x00\x20" + sha256(nws)).hex()
            out.append(Bad(nm + ": keys swapped in the witness script", t, q, h, None))
        if k == "funding":
            t2 = tx.copy()
            t2.wit[0][0] = b"\x00"
            out.append(Bad(nm + ": non-empty CHECKMULTISIG dummy", t2, pv, h, ("BadWitness", 0)))
        if k == "to_local" and tx.wit[0][1] == b"":
            t2 = tx.copy()
            t2.ins[0] = t2.ins[0][:3] + (f["delay"] - 1,)
            out.append(Bad(nm + ": nSequence below the CSV delay (not re-signed)", t2, pv, h, ("CsvNotMet", 0)))
    # high S and non-strict DER for the first ECDSA signature
    if pos is not None and tx.wit[0][pos][:1] == b"\x30":
        sig = tx.wit[0][pos]
        r, s_, ht = parse_der(sig)
        t = tx.copy()
        t.wit[0][pos] = der(r, N - s_, ht)
        out.append(Bad(nm + ": high-S signature", t, pv, h, ("BadSignatureEncoding", 0)))
        t = tx.copy()
        lr = sig[3]
        t.wit[0][pos] = b"\x30" + bytes([sig[1] + 1]) + b"\x02" + bytes([lr + 1]) + b"\x00" + sig[4:]
        out.append(Bad(nm + ": R with a needless leading zero", t, pv, h, ("BadSignatureEncoding", 0)))
    t = tx.copy()
    t.ins[0] = (t.ins[0][0], t.ins[0][1], b"\x51", t.ins[0][3])
    out.append(Bad(nm + ": non-empty scriptSig", t, pv, h, ("ScriptSigNotEmpty", 0)))
    raw = tx.ser()
    out.append(Bad(nm + ": a trailing byte", raw + b"\x00", pv, h, ("TrailingBytes",)))
    out.append(Bad(nm + ": last byte missing", raw[:-1], pv, h, ("Truncated",)))
    out.append(Bad(nm + ": one prevout too few", raw, pv[:-1], h, ("PrevoutCountMismatch",)))
    if first:
        # consensus/standardness limits, checked before any script
        t = tx.copy()
        t.outs[0] = (t.outs[0][0], b"\xae" * 201)  # 201 CHECKMULTISIG = 4020 sigops, cost 16080 + 4
        out.append(Bad(nm + ": output with 201 OP_CHECKMULTISIG", t, pv, h, ("TooManySigops",)))
        t = tx.copy()
        t.outs[0] = (t.outs[0][0], b"\x4d\xff\xff" + b"\xae" * 199 + b"\xac" * 2)  # pushed: 0 sigops
        out.append(Bad(nm + ": sigop bytes after a truncated push (counted 0, so a signature error)", t, pv, h,
                       ("BadSignature", 0)))
        t = tx.copy()
        t.outs[0] = (t.outs[0][0], b"\x6a" + b"\x00" * 100_000)
        out.append(Bad(nm + ": over 100,000 bytes", t, pv, h, ("TxTooLarge",)))
        t = tx.copy()
        t.ins[0] = (b"\x00" * 32, 0xFFFFFFFF) + t.ins[0][2:]
        out.append(Bad(nm + ": null prevout", t, pv, h, ("NullPrevout", 0)))
        t = tx.copy()
        t.outs[0] = (MAX_MONEY + 1, t.outs[0][1])
        out.append(Bad(nm + ": output above MAX_MONEY", t, pv, h, ("AmountTooLarge",)))
        raw = tx.ser()
        assert raw[4:6] == b"\x00\x01"
        out.append(Bad(nm + ": segwit flag 02", raw[:5] + b"\x02" + raw[6:], pv, h, ("BadSegwitFlag",)))
        out.append(Bad(nm + ": input count as a 3-byte compact size",
                       raw[:6] + b"\xfd" + struct.pack("<H", len(tx.ins)) + raw[7:], pv, h,
                       ("NonCanonicalCompactSize",)))
        t = tx.copy()
        t.wit = [[] for _ in t.ins]
        wraw = t.ser(False)
        out.append(Bad(nm + ": serialized without witness", wraw, pv, h, ("BadWitness", 0)))
        out.append(Bad(nm + ": segwit marker with every witness empty",
                       wraw[:4] + b"\x00\x01" + wraw[4:-4] + b"\x00" * len(tx.ins) + wraw[-4:], pv, h,
                       ("EmptyWitnesses",)))
        no_outs = struct.pack("<I", tx.version) + b"\x00\x01" + cs(len(tx.ins)) + b"".join(
            i[0] + struct.pack("<I", i[1]) + b"\x00" + struct.pack("<I", i[3]) for i in tx.ins) + b"\x00"
        out.append(Bad(nm + ": no outputs", no_outs + b"\x01\x00" + struct.pack("<I", tx.locktime), pv, h,
                       ("NoOutputs",)))
        out.append(Bad(nm + ": no inputs", struct.pack("<I", tx.version) + b"\x00\x01\x00" + raw[7 + 41 * len(tx.ins):],
                       pv, h, ("NoInputs",)))
        q = [dict(p) for p in pv]
        q[0]["script"] = "76a914" + "11" * 20 + "88ac"
        out.append(Bad(nm + ": a P2PKH prevout (seeded so)", tx.ser(), q, h, ("UnsupportedScriptPubKey", 0)))
        out.append(Bad(nm + ": prevout not in the UTXO set", tx.ser(), pv, h, ("UnknownUtxo", 0), seeds=[]))
        out.append(Bad(nm + ": at height 0", tx.ser(), pv, 0, ("TooEarly",)))
    if len(tx.ins) > 1:
        q = [dict(p) for p in pv]
        for p_ in q:
            p_["amount"] = MAX_MONEY
        out.append(Bad(nm + ": inputs summing above MAX_MONEY", tx.ser(), q, h, ("AmountTooLarge",)))
        t = tx.copy()
        t.ins[1] = t.ins[0][:2] + t.ins[1][2:]
        out.append(Bad(nm + ": input 1 spends input 0's outpoint", t, pv, h, None))
        t = tx.copy()
        if len(t.outs) > 1:
            t.outs[1] = (t.outs[1][0] - 1 if t.outs[1][0] else 1, t.outs[1][1])
            out.append(Bad(nm + ": output 1 amount changed", t, pv, h, None))
    return out


# ---------------------------------------------------------------- synthetic spends (coincurve)

class Synth:
    """Builds and signs synthetic txs. Each input is (prevout dict, outpoint, sequence, signer);
    signer(tx, i, spent) returns the witness."""

    def __init__(self, rng):
        self.rng = rng

    def key(self):
        return PrivateKey(self.rng.randbytes(32))

    def outpoint(self):
        return self.rng.randbytes(32), self.rng.randrange(0, 4)

    def p2tr_spk(self):
        return b"\x51\x20" + xonly(self.key())

    def tx(self, inputs, outs, locktime=0, version=2):
        tx = Tx(version, [(op[0], op[1], b"", seq) for _, op, seq, _ in inputs], list(outs), locktime)
        spent = [(p["amount"], bytes.fromhex(p["script"])) for p, _, _, _ in inputs]
        tx.wit = [signer(tx, i, spent) for i, (_, _, _, signer) in enumerate(inputs)]
        return tx, [p for p, _, _, _ in inputs]


def p2wpkh_signer(key, ht=1):
    pub = key.public_key.format()

    def f(tx, i, spent):
        code = b"\x76\xa9\x14" + hash160(pub) + b"\x88\xac"
        return [sign_ecdsa(key, bip143(tx, i, code, spent[i][0], ht), ht), pub]
    return f


def p2tr_signer(key, ht=0, rng=None):
    def f(tx, i, spent):
        sig = key.sign_schnorr(bip341(tx, spent, i, ht), rng.randbytes(32) if rng else None)
        return [sig if ht == 0 else sig + bytes([ht])]
    return f


def p2wsh_signer(ws, make_stack):
    """make_stack(sign) returns the stack before the script; sign(key, ht) signs for this input."""
    def f(tx, i, spent):
        def sign(key, ht=1):
            return sign_ecdsa(key, bip143(tx, i, ws, spent[i][0], ht), ht)
        return make_stack(sign) + [ws]
    return f


def p2wpkh_spk(key):
    return b"\x00\x14" + hash160(key.public_key.format())


def p2wsh_spk(ws):
    return b"\x00\x20" + sha256(ws)


def synthetic(rng):
    """(valid cases, bad cases, worst-case commitment)."""
    S = Synth(rng)
    k = {n: S.key() for n in ("rev", "delayed", "remote", "local", "fund", "fund2", "wallet")}
    pub = {n: v.public_key.format() for n, v in k.items()}
    preimage = rng.randbytes(32)
    ph = hashlib.new("ripemd160", sha256(preimage)).digest()
    revh = hash160(pub["rev"])
    cases, bad = [], []
    h0 = 1000  # the height every synthetic prevout was created at

    def wsh_input(ws, seq, stack, amount=50_000):
        return (pv_of(amount, p2wsh_spk(ws), h0), S.outpoint(), seq, p2wsh_signer(ws, stack))

    def one(ws, seq, stack, lt=0, version=2, amount=50_000):
        return S.tx([wsh_input(ws, seq, stack, amount)], [(amount - 300, S.p2tr_spk())], lt, version)

    def add(name, kind, tx_pv, height=None):
        tx, pv = tx_pv
        c = Case("synthetic " + name, kind, tx, pv, height if height is not None else min_height(tx, pv))
        cases.append(c)
        return c

    def nope(name, tx_pv, height, err, **kw):
        tx, pv = tx_pv
        bad.append(Bad("synthetic " + name, tx, pv, height, err, **kw))

    # ---- to_local / HTLC-tx output
    tl = wscript_to_local(144, pub["rev"], pub["delayed"])
    add("to_local delayed, CSV 144", "to_local-sweep", one(tl, 144, lambda s: [s(k["delayed"]), b""]))
    c = add("to_local revocation", "penalty-to_local", one(tl, 0xFFFFFFFD, lambda s: [s(k["rev"]), b"\x01"]))
    t = c.tx.copy()
    t.wit[0][1] = b"\x02"
    nope("to_local revocation: IF argument 0x02 (MINIMALIF)", (t, c.pv), c.height, ("BadWitness", 0))
    nope("to_local delayed: nSequence 143, re-signed, age 144", one(tl, 143, lambda s: [s(k["delayed"]), b""]),
         h0 + 144, ("CsvNotMet", 0))
    nope("to_local delayed: disable bit set, re-signed",
         one(tl, DISABLE | 144, lambda s: [s(k["delayed"]), b""]), h0 + 144, ("CsvNotMet", 0))
    nope("to_local delayed: version 1, re-signed",
         one(tl, 144, lambda s: [s(k["delayed"]), b""], version=1), h0 + 144, ("CsvNotMet", 0))
    nope("to_local delayed: time-type sequence, re-signed",
         one(tl, TYPE | 144, lambda s: [s(k["delayed"]), b""]), h0 + 144, ("TimeRelativeLockUnsupported", 0))
    nope("to_local delayed: BIP68 age 143", one(tl, 144, lambda s: [s(k["delayed"]), b""]), h0 + 143,
         ("RelativeLockNotMet", 0))
    tl6 = wscript_to_local(6, pub["rev"], pub["delayed"])
    add("HTLC-tx output delayed, CSV 6", "htlc-tx-output-sweep", one(tl6, 6, lambda s: [s(k["delayed"]), b""]))
    nope("HTLC-tx output delayed: nSequence 5, re-signed, age 6", one(tl6, 5, lambda s: [s(k["delayed"]), b""]),
         h0 + 6, ("CsvNotMet", 0))

    # ---- to_remote
    trm = wscript_to_remote_anchored(pub["remote"], 1)
    add("to_remote, CSV 1", "to_remote-spend", one(trm, 1, lambda s: [s(k["remote"])]))
    nope("to_remote: nSequence 0, re-signed, age 1", one(trm, 0, lambda s: [s(k["remote"])]), h0 + 1,
         ("CsvNotMet", 0))
    trm2 = wscript_to_remote_anchored(pub["remote"], 2)
    nope("to_remote with CSV 2 (a lease's; not a template)", one(trm2, 2, lambda s: [s(k["remote"])]), h0 + 2,
         ("UnknownWitnessScript", 0))

    # ---- anchor
    an = wscript_anchor(pub["fund"])
    add("anchor, key", "anchor-spend", one(an, 0xFFFFFFFD, lambda s: [s(k["fund"])]))
    c = add("anchor, anyone after 16", "anchor-sweep", one(an, 16, lambda s: [b""]))
    t = c.tx.copy()
    t.ins[0] = t.ins[0][:3] + (15,)
    nope("anchor anyone: nSequence 15, age 16", (t, c.pv), h0 + 16, ("CsvNotMet", 0))
    t = c.tx.copy()
    t.wit[0][0] = b"\x30\x01"
    nope("anchor: garbage signature (NULLFAIL)", (t, c.pv), c.height, ("BadSignatureEncoding", 0))

    # ---- offered HTLC
    off = wscript_htlc_offer(pub["local"], pub["remote"], ph, revh)
    c = add("offered HTLC, revocation", "penalty-offered-htlc", one(off, 0xFFFFFFFD,
                                                                   lambda s: [s(k["rev"]), pub["rev"]]))
    t = c.tx.copy()
    t.wit[0][1] = pub["local"]
    nope("offered HTLC revocation: a key that is not the revocation key", (t, c.pv), c.height, ("BadWitness", 0))
    c = add("offered HTLC, remote success", "remote-success", one(off, 1, lambda s: [s(k["remote"]), preimage]))
    t = c.tx.copy()
    t.wit[0][1] = bytes([preimage[0] ^ 1]) + preimage[1:]
    nope("offered HTLC remote success: wrong preimage", (t, c.pv), c.height, ("BadPreimage", 0))
    nope("offered HTLC remote success: nSequence 0, re-signed",
         one(off, 0, lambda s: [s(k["remote"]), preimage]), h0 + 1, ("CsvNotMet", 0))
    nope("offered HTLC remote success: disable bit set, re-signed",
         one(off, DISABLE | 1, lambda s: [s(k["remote"]), preimage]), h0 + 1, ("CsvNotMet", 0))
    add("offered HTLC, HTLC-timeout tx", "htlc-timeout",
        one(off, 1, lambda s: [b"", s(k["remote"], 0x83), s(k["local"]), b""]))
    nope("offered HTLC HTLC-timeout: nSequence 0, re-signed",
         one(off, 0, lambda s: [b"", s(k["remote"], 0x83), s(k["local"]), b""]), h0 + 1, ("CsvNotMet", 0))
    nope("offered HTLC HTLC-timeout: last item 0x01, not empty",
         one(off, 1, lambda s: [b"", s(k["remote"], 0x83), s(k["local"]), b"\x01"]), h0 + 1, ("BadWitness", 0))
    nope("offered HTLC HTLC-timeout: signatures in the wrong order",
         one(off, 1, lambda s: [b"", s(k["local"]), s(k["remote"], 0x83), b""]), h0 + 1, ("BadSignature", 0))

    # ---- received HTLC
    rcv = wscript_htlc_receive(70_000, pub["local"], pub["remote"], ph, revh)
    add("received HTLC, revocation", "penalty-received-htlc",
        one(rcv, 0xFFFFFFFD, lambda s: [s(k["rev"]), pub["rev"]]))
    c = add("received HTLC, HTLC-success tx", "htlc-success",
            one(rcv, 1, lambda s: [b"", s(k["remote"], 0x83), s(k["local"]), preimage]))
    t = c.tx.copy()
    t.wit[0][3] = rng.randbytes(32)
    nope("received HTLC HTLC-success: wrong preimage", (t, c.pv), c.height, ("BadPreimage", 0))
    nope("received HTLC HTLC-success: nSequence 0, re-signed",
         one(rcv, 0, lambda s: [b"", s(k["remote"], 0x83), s(k["local"]), preimage]), h0 + 1, ("CsvNotMet", 0))
    c = add("received HTLC, remote timeout after CLTV 70000", "remote-timeout",
            one(rcv, 1, lambda s: [s(k["remote"]), b""], lt=70_000))
    t = c.tx.copy()
    t.locktime = 69_999
    nope("received HTLC remote timeout: nLockTime below the CLTV (not re-signed)", (t, c.pv), c.height,
         ("CltvNotMet", 0))
    nope("received HTLC remote timeout: nLockTime 69999, re-signed, height 70001",
         one(rcv, 1, lambda s: [s(k["remote"]), b""], lt=69_999), 70_001, ("CltvNotMet", 0))
    nope("received HTLC remote timeout: nSequence 0, re-signed, CLTV met",
         one(rcv, 0, lambda s: [s(k["remote"]), b""], lt=70_000), 70_001, ("CsvNotMet", 0))
    nope("received HTLC remote timeout: time-based nLockTime, re-signed",
         one(rcv, 1, lambda s: [s(k["remote"]), b""], lt=THRESHOLD), 70_001, ("CltvNotMet", 0))
    t = c.tx.copy()
    t.ins[0] = t.ins[0][:3] + (FINAL,)
    nope("received HTLC remote timeout: final nSequence", (t, c.pv), c.height, ("CltvNotMet", 0))
    nope("received HTLC remote timeout: one block before the CLTV",
         one(rcv, 1, lambda s: [s(k["remote"]), b""], lt=70_000), 70_000, ("NonFinal",))

    # ---- 2-of-2
    f2 = redeem_2of2(pub["fund"], pub["fund2"])
    lo, hi = sorted([("fund", pub["fund"]), ("fund2", pub["fund2"])], key=lambda x: x[1])
    add("2-of-2, mutual close", "mutual-close",
        one(f2, FINAL, lambda s: [b"", s(k[lo[0]]), s(k[hi[0]])]))
    nope("2-of-2: signatures in the wrong order", one(f2, FINAL, lambda s: [b"", s(k[hi[0]]), s(k[lo[0]])]), 1,
         ("BadSignature", 0))
    unsorted = b"\x52" + push(hi[1]) + push(lo[1]) + b"\x52\xae"
    nope("2-of-2 with the keys not in script.c's order",
         one(unsorted, FINAL, lambda s: [b"", s(k[hi[0]]), s(k[lo[0]])]), 1, ("UnknownWitnessScript", 0))

    # ---- P2WPKH: every BIP143 hash type. 6 inputs, 2 outputs, so inputs 4 and 5
    # sign SINGLE without a matching output (zero hashOutputs).
    hts = [0x02, 0x03, 0x81, 0x82, 0x03, 0x83]
    keys = [S.key() for _ in hts]
    ins = [(pv_of(rng.randrange(10_000, 100_000), p2wpkh_spk(kk), h0 - 10), S.outpoint(), 0xFFFFFFFD,
            p2wpkh_signer(kk, ht)) for ht, kk in zip(hts, keys)]
    c = add("P2WPKH, hash types NONE SINGLE ALL|ACP NONE|ACP SINGLE SINGLE|ACP", "wallet",
            S.tx(ins, [(30_000, S.p2tr_spk()), (20_000, S.p2tr_spk())], h0 - 1))
    t = c.tx.copy()
    t.outs[0] = (t.outs[0][0] - 1, t.outs[0][1])
    nope("P2WPKH hash types: output 0 amount - 1", (t, c.pv), c.height, ("BadSignature", 2))
    t = c.tx.copy()
    t.outs[1] = (t.outs[1][0] - 1, t.outs[1][1])
    nope("P2WPKH hash types: output 1 amount - 1", (t, c.pv), c.height, ("BadSignature", 1))
    wk = S.key()
    ins = [(pv_of(40_000, p2wpkh_spk(wk), h0), S.outpoint(), FINAL, p2wpkh_signer(wk))]
    tx_, pv_ = S.tx(ins, [(39_000, S.p2tr_spk())])
    t = tx_.copy()
    t.wit[0][1] = wk.public_key.format(compressed=False)
    nope("P2WPKH: uncompressed key", (t, pv_), 1, ("BadWitness", 0))
    offcurve = b"\x02" + (5).to_bytes(32, "big")  # x = 5: 125 + 7 is not a square mod p
    assert pow((125 + 7) % P, (P - 1) // 2, P) != 1
    t = tx_.copy()
    t.wit[0][1] = offcurve
    q = [pv_of(40_000, b"\x00\x14" + hash160(offcurve), h0)]
    nope("P2WPKH: key not on the curve", (t, q), 1, ("BadPublicKey", 0))
    for name, fk in (("prefix 04", b"\x04" + pub["fund"][1:]), ("off the curve", offcurve)):
        an_bad = wscript_anchor(fk)
        nope("anchor anyone with a key %s" % name, one(an_bad, 16, lambda s: [b""]), h0 + 16, ("BadPublicKey", 0))
    # duplicate input, validly signed: caught as the second occurrence finds no UTXO
    op = S.outpoint()
    dup = [(pv_of(40_000, p2wpkh_spk(wk), h0), op, FINAL, p2wpkh_signer(wk)) for _ in range(2)]
    nope("P2WPKH: the same outpoint twice, both signed", S.tx(dup, [(79_000, S.p2tr_spk())]), 1,
         ("DuplicateInput", 1))

    # ---- P2TR: every BIP341 hash type, each SINGLE with its output
    tks = [S.key() for _ in range(7)]
    t_hts = [0x00, 0x01, 0x02, 0x03, 0x81, 0x82, 0x83]
    ins = [(pv_of(100_000 + j, b"\x51\x20" + xonly(kk), h0 - 5), S.outpoint(), 0xFFFFFFFD,
            p2tr_signer(kk, ht, rng)) for j, (kk, ht) in enumerate(zip(tks, t_hts))]
    outs = [(90_000 + j, S.p2tr_spk()) for j in range(7)]
    c = add("P2TR, hash types DEFAULT ALL NONE SINGLE ALL|ACP NONE|ACP SINGLE|ACP", "wallet", S.tx(ins, outs, h0))
    t = c.tx.copy()
    t.outs[3] = (t.outs[3][0] - 1, t.outs[3][1])
    nope("P2TR hash types: output 3 amount - 1", (t, c.pv), c.height, ("BadSignature", 0))
    t = c.tx.copy()
    t.wit[0] = [t.wit[0][0], b"\x50" + rng.randbytes(8)]
    nope("P2TR: an annex", (t, c.pv), c.height, ("BadWitness", 0))
    t = c.tx.copy()
    t.wit[0] = [t.wit[0][0] + b"\x00"]
    nope("P2TR: 65-byte signature with hash type 0x00", (t, c.pv), c.height, ("BadSignatureEncoding", 0))
    t = c.tx.copy()
    t.wit[1] = [t.wit[1][0][:64] + b"\x04"]
    nope("P2TR: hash type 0x04", (t, c.pv), c.height, ("BadSignatureEncoding", 1))
    # SINGLE with no matching output: invalid under BIP341 (no signature can be made)
    for ht in (0x03, 0x83):
        ins = [(pv_of(60_000, b"\x51\x20" + xonly(tks[j]), h0), S.outpoint(), 0xFFFFFFFD,
                p2tr_signer(tks[j], 0, rng) if j == 0 else (lambda tx, i, sp, ht=ht: [rng.randbytes(64) + bytes([ht])]))
               for j in range(2)]
        nope("P2TR: SINGLE (%#x) on input 1 of a 1-output tx" % ht, S.tx(ins, [(100_000, S.p2tr_spk())]), h0,
             ("NoMatchingOutput", 1))

    # ---- lock times: heights, and times around MTP(h) = T(max(h - 6, 0))
    tk = tks[0]

    def p2tr_lt(lt, seq=0xFFFFFFFE, amount=70_000):
        ins = [(pv_of(amount, b"\x51\x20" + xonly(tk), 1), S.outpoint(), seq, p2tr_signer(tk, 0, rng))]
        return S.tx(ins, [(amount - 500, S.p2tr_spk())], lt)

    hh = 1000
    add("nLockTime height h - 1 at h", "wallet", p2tr_lt(hh - 1), hh)
    nope("nLockTime height h at h", p2tr_lt(hh), hh, ("NonFinal",))
    add("nLockTime MTP(h) - 1 at h", "wallet", p2tr_lt(mtp(hh) - 1), hh)
    nope("nLockTime MTP(h) at h", p2tr_lt(mtp(hh)), hh, ("NonFinal",))
    nope("nLockTime T(h) - 1 at h (would pass against the block's own time)", p2tr_lt(GENESIS_TIME + hh * INTERVAL - 1),
         hh, ("NonFinal",))
    add("nLockTime MTP(3) - 1 = GENESIS_TIME - 1 at 3", "wallet", p2tr_lt(GENESIS_TIME - 1), 3)
    nope("nLockTime MTP(3) = GENESIS_TIME at 3", p2tr_lt(GENESIS_TIME), 3, ("NonFinal",))
    add("nLockTime MTP(7) - 1 at 7", "wallet", p2tr_lt(mtp(7) - 1), 7)
    nope("nLockTime MTP(7) at 7", p2tr_lt(mtp(7)), 7, ("NonFinal",))
    add("future nLockTime with every sequence final", "wallet", p2tr_lt(0xFFFFFFFF, FINAL), 2)

    # ---- sigops and size limits, at the boundary
    def p2tr_out(script, amount=70_000):
        ins = [(pv_of(amount, b"\x51\x20" + xonly(tk), 1), S.outpoint(), FINAL, p2tr_signer(tk, 0, rng))]
        return S.tx(ins, [(amount - 500, script)])

    add("sigop cost exactly 16,000 (3,999 in an output, 1 input)", "wallet", p2tr_out(b"\xae" * 199 + b"\xac" * 19), 2)
    nope("sigop cost 16,004", p2tr_out(b"\xae" * 199 + b"\xac" * 20), 2, ("TooManySigops",))
    base = len(p2tr_out(b"\x6a")[0].ser())  # with a 1-byte script; one over 65,535 bytes has a 5-byte size
    script = b"\x6a" + b"\x00" * (100_000 - base - 4)
    add("exactly 100,000 bytes", "wallet", p2tr_out(script), 2)
    assert len(cases[-1].tx.ser()) == 100_000, len(cases[-1].tx.ser())
    nope("100,001 bytes", p2tr_out(script + b"\x00"), 2, ("TxTooLarge",))

    # ---- outputs that create no UTXO: exits and OP_RETURN, and an unspendable one
    ins = [(pv_of(500_000, b"\x51\x20" + xonly(tk), 1), S.outpoint(), FINAL, p2tr_signer(tk, 0, rng))]
    exit_addr = bytes.fromhex("00000000000000000000000000000000000e417a")
    add("exit, OP_RETURN and a P2PKH output", "withdraw-exit",
        S.tx(ins, [(200_000, b"\x60\x14" + exit_addr), (1_000, b"\x6a\x04test"),
                   (100_000, b"\x76\xa9\x14" + b"\x22" * 20 + b"\x88\xac"), (150_000, S.p2tr_spk())]), 2)
    return cases, bad


def worst_case(rng):
    """A commitment tx with 30 offered and 30 received HTLCs, to_local, to_remote
    and both anchors: byte-exact CLN scripts, signed by both funding keys."""
    S = Synth(rng)
    k1, k2 = S.key(), S.key()
    keys = {n: S.key().public_key.format() for n in ("rev", "local", "remote", "delayed", "remotepay")}
    revh = hash160(keys["rev"])
    outs = []
    for j in range(30):
        ph = hashlib.new("ripemd160", sha256(rng.randbytes(32))).digest()
        outs.append((10_000 + j, p2wsh_spk(wscript_htlc_offer(keys["local"], keys["remote"], ph, revh))))
    for j in range(30):
        ph = hashlib.new("ripemd160", sha256(rng.randbytes(32))).digest()
        outs.append((20_000 + j, p2wsh_spk(wscript_htlc_receive(800_000 + j, keys["local"], keys["remote"], ph, revh))))
    outs.append((330, p2wsh_spk(wscript_anchor(k1.public_key.format()))))
    outs.append((330, p2wsh_spk(wscript_anchor(k2.public_key.format()))))
    outs.append((3_000_000, p2wsh_spk(wscript_to_local(144, keys["rev"], keys["delayed"]))))
    outs.append((2_000_000, p2wsh_spk(wscript_to_remote_anchored(keys["remotepay"]))))
    outs.sort(key=lambda o: (o[0], o[1]))  # BOLT 3 output order
    total = sum(a for a, _ in outs) + 5_000
    f2 = redeem_2of2(k1.public_key.format(), k2.public_key.format())
    lo, hi = sorted([k1, k2], key=lambda kk: kk.public_key.format())
    signer = p2wsh_signer(f2, lambda s: [b"", s(lo), s(hi)])
    # CLN's commitment: nLockTime 0x20 << 24 | obscured number, sequence 0x80 << 24 | the rest
    tx, pv = S.tx([(pv_of(total, p2wsh_spk(f2), 900), S.outpoint(), 0x80123456, signer)], outs, 0x20654321)
    return Case("worst-case commitment: 30 + 30 HTLCs, 4 more outputs", "commitment", tx, pv, 1000)


# ---------------------------------------------------------------- Solidity: cases

def script_offset(tx, j):
    """Offset of output j's script in tx.ser()."""
    o = (6 if any(tx.wit) else 4) + len(cs(len(tx.ins))) + 41 * len(tx.ins) + len(cs(len(tx.outs)))
    for a, s in tx.outs[:j]:
        o += len(ser_out(a, s))
    return o + 8 + len(cs(len(tx.outs[j][1])))


def emit_case(emit, c, fn_var="c"):
    raw = c.tx.ser()
    emit('            %s.name = "%s";' % (fn_var, sol_str(c.name)))
    emit('            %s.kind = "%s";' % (fn_var, sol_str(c.kind)))
    emit("            %s.rawTx = %s;" % (fn_var, hexlit(raw)))
    emit_prevouts(emit, fn_var + ".prevouts", c.pv)
    n = len(c.tx.ins)
    emit("            %s.inTxids = new bytes32[](%d);" % (fn_var, n))
    emit("            %s.inVouts = new uint32[](%d);" % (fn_var, n))
    emit("            %s.seed = new bool[](%d);" % (fn_var, n))
    for i, (t, v, _, _) in enumerate(c.tx.ins):
        emit("            %s.inTxids[%d] = 0x%s;" % (fn_var, i, t.hex()))
        emit("            %s.inVouts[%d] = %d;" % (fn_var, i, v))
        emit("            %s.seed[%d] = %s;" % (fn_var, i, "true" if c.seed[i] else "false"))
    emit("            %s.height = %d;" % (fn_var, c.height))
    emit("            %s.minHeight = %d;" % (fn_var, c.min_h))
    if c.early is not None:
        emit("            %s.earlyError = %s;" % (fn_var, sol_err(c.early)))
    emit("            %s.txid = 0x%s;" % (fn_var, c.tx.txid().hex()))
    emit("            %s.fee = %d;" % (fn_var, c.fee))
    emit("            %s.outAmounts = new uint64[](%d);" % (fn_var, len(c.tx.outs)))
    emit("            %s.outScripts = new bytes[](%d);" % (fn_var, len(c.tx.outs)))
    for j, (a, s) in enumerate(c.tx.outs):
        emit("            %s.outAmounts[%d] = %d;" % (fn_var, j, a))
        if len(s) <= 2000:
            emit("            %s.outScripts[%d] = %s;" % (fn_var, j, hexlit(s)))
        else:  # the 100,000-byte case: read it from the raw tx instead
            emit("            %s.outScripts[%d] = Slice.cut(%s.rawTx, %d, %d);" % (
                fn_var, j, fn_var, script_offset(c.tx, j), len(s)))


def emit_cases_fn(emit, fname, cases):
    emit("    function %s(uint256 i) internal pure returns (Case memory c) {" % fname)
    for j, c in enumerate(cases):
        emit("        if (i == %d) {" % j)
        emit_case(emit, c)
        emit("            return c;\n        }")
    emit('        revert("no such vector");\n    }\n')


def emit_bad_fn(emit, fname, bads):
    emit("    function %s(uint256 i) internal pure returns (Bad memory b) {" % fname)
    for j, b in enumerate(bads):
        emit("        if (i == %d) {" % j)
        emit('            b.name = "%s";' % sol_str(b.name))
        emit("            b.rawTx = %s;" % hexlit(b.raw))
        emit_prevouts(emit, "b.prevouts", b.pv)
        emit_seeds(emit, "b.seed", b.seeds)
        emit("            b.height = %d;" % b.height)
        emit("            b.err = %s;" % sol_err(b.err))
        emit("            return b;\n        }")
    emit('        revert("no such vector");\n    }\n')


IMPORTS = """import {Prevout} from "../../src/BtcTx.sol";
import "../../src/Errors.sol" as E;
import {Case, Bad, Slice} from "../Types.sol";
"""


def write(name, lines):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w") as f:
        f.write("\n".join(lines) + "\n")


def gen_real(files):
    out = [HEADER, IMPORTS]
    emit = out.append
    emit("/// Real CLN transactions, one library function per vector file, in the")
    emit("/// order the toy chain applied them (tests/vectors).")
    emit("library RealVectors {")
    emit("    uint256 internal constant GENESIS_TIME = %d;" % GENESIS_TIME)
    emit("    uint256 internal constant INTERVAL = %d;" % INTERVAL)
    emit("    uint256 internal constant FILE_COUNT = %d;\n" % len(files))
    emit("    function fileName(uint256 f) internal pure returns (string memory) {")
    for i, (fn, _) in enumerate(files):
        emit('        if (f == %d) return "%s";' % (i, fn))
    emit('        revert("no such file");\n    }\n')
    emit("    function count(uint256 f) internal pure returns (uint256) {")
    for i, (_, cases) in enumerate(files):
        emit("        if (f == %d) return %d;" % (i, len(cases)))
    emit('        revert("no such file");\n    }\n')
    emit("    function get(uint256 f, uint256 i) internal pure returns (Case memory) {")
    for i in range(len(files)):
        emit("        if (f == %d) return file%d(i);" % (i, i))
    emit('        revert("no such file");\n    }\n')
    for i, (_, cases) in enumerate(files):
        emit_cases_fn(emit, "file%d" % i, cases)
    emit("}")
    write("RealVectors.sol", out)


def gen_synthetic(cases, bads, worst, deposit_vectors):
    out = [HEADER, IMPORTS]
    emit = out.append
    emit("/// Synthetic spends signed with coincurve, tampered copies of the real txs,")
    emit("/// and the worst-case commitment.")
    emit("library Synthetic {")
    emit("    uint256 internal constant GENESIS_TIME = %d;" % GENESIS_TIME)
    emit("    uint256 internal constant INTERVAL = %d;" % INTERVAL)
    emit("    uint256 internal constant VALID_COUNT = %d;" % len(cases))
    emit("    uint256 internal constant BAD_COUNT = %d;\n" % len(bads))
    emit_cases_fn(emit, "valid", cases)
    emit("    function worstCase() internal pure returns (Case memory c) {")
    emit("        {")
    emit_case(emit, worst)
    emit("        }\n    }\n")
    # bad cases split in chunks so no function is too large
    chunk = 64
    for s in range(0, len(bads), chunk):
        emit_bad_fn(emit, "bad%d" % (s // chunk), bads[s:s + chunk])
    emit("    function bad(uint256 i) internal pure returns (Bad memory) {")
    for s in range(0, len(bads), chunk):
        emit("        if (i < %d) return bad%d(i - %d);" % (s + chunk, s // chunk, s))
    emit('        revert("no such vector");\n    }\n')
    emit("    // Deposit txs (SPEC 3.3) built independently, for a contract at DEPOSIT_ADDRESS")
    emit("    // on chain id 31337.")
    emit("    address internal constant DEPOSIT_ADDRESS = address(uint160(%d));" % int(deposit_vectors["address"], 16))
    emit("    uint256 internal constant DEPOSIT_COUNT = %d;\n" % len(deposit_vectors["txs"]))
    emit("    function depositTx(uint256 i) internal pure returns (uint64 amount, bytes memory spk, bytes memory raw,"
         " bytes32 txid) {")
    for j, d in enumerate(deposit_vectors["txs"]):
        emit("        if (i == %d) return (%d, %s, %s, 0x%s);" % (j, d["amount"], hexlit(d["spk"]), hexlit(d["raw"]),
                                                              d["txid"].hex()))
    emit('        revert("no such vector");\n    }\n')
    emit("    function depositSpend() internal pure returns (Case memory c) {")
    emit("        {")
    emit_case(emit, deposit_vectors["spend"])
    emit("        }\n    }\n")
    exits = [j for j, c in enumerate(cases) if c.kind == "withdraw-exit"]
    emit("    /// The valid case with an exit, an OP_RETURN and an unspendable output.")
    emit("    uint256 internal constant EXIT_CASE = %d;" % exits[0])
    emit("}")
    write("Synthetic.sol", out)


def deposit_vectors(rng):
    """Deposit txs for a contract at `address` on chain 31337 (nonces 0, 1, ...),
    and a tx spending deposit 0 (made at height 5) at height 6."""
    addr = bytes.fromhex("000000000000000000000000000000000e7c4a01")
    key = PrivateKey(rng.randbytes(32))
    txs = []
    for nonce, (amount, spk) in enumerate([
            (1_000_000, b"\x51\x20" + xonly(key)),
            (1, b"\x00\x14" + b"\x33" * 20),
            (MAX_MONEY, b""),
            (5, b"\x00\x20" + b"\x44" * 32 + b"\x51" * 300)]):
        prev = keccak256(b"evmchan deposit" + (31337).to_bytes(32, "big") + addr + nonce.to_bytes(32, "big"))
        tx = Tx(2, [(prev, 0, b"", FINAL)], [(amount, spk)], 0)
        txs.append({"amount": amount, "spk": spk, "raw": tx.ser(False), "txid": tx.txid()})
    S = Synth(rng)
    d0 = txs[0]
    spend, pv = S.tx([(pv_of(d0["amount"], d0["spk"], 5), (d0["txid"], 0), 0xFFFFFFFD, p2tr_signer(key, 0, rng))],
                     [(600_000, S.p2tr_spk()), (399_000, b"\x60\x14" + b"\x77" * 20)], 5)
    c = Case("spend of deposit 0", "wallet", spend, pv, 6, seed=[False])
    return {"address": addr.hex(), "txs": txs, "spend": c}


def gen_bip():
    b340 = json.load(open(os.path.join(DATA, "bip340-vectors.json")))["vectors"]
    b341 = json.load(open(os.path.join(DATA, "bip341-keypath.json")))
    out = [HEADER]
    emit = out.append
    emit('import {Prevout} from "../../src/BtcTx.sol";\n')
    emit("struct SchnorrVector { uint256 index; bytes32 pubkey; bytes message; bytes sig; bool ok; }")
    emit("struct KeyPathInput { uint256 index; uint8 hashType; bytes sigMsg; bytes32 sigHash; bytes sig; }\n")
    emit("/// The official BIP340 and BIP341 (key path) test vectors, from test/data.")
    emit("library BipVectors {")
    emit("    uint256 internal constant BIP340_COUNT = %d;\n" % len(b340))
    emit("    function bip340(uint256 i) internal pure returns (SchnorrVector memory) {")
    for v in b340:
        pk, msg, sig = (bytes.fromhex(v[k]) for k in ("public key", "message", "signature"))
        ok = v["verification result"]
        assert schnorr_ok(pk, msg, sig) == ok, v["index"]
        emit("        if (i == %d) return SchnorrVector(%d, 0x%s, %s, %s, %s);" % (
            v["index"], v["index"], pk.hex(), hexlit(msg), hexlit(sig), str(ok).lower()))
    emit('        revert("no such vector");\n    }\n')
    raw = bytes.fromhex(b341["rawUnsignedTx"])
    tx = parse(raw)
    spent = [(u["amountSats"], bytes.fromhex(u["scriptPubKey"])) for u in b341["utxosSpent"]]
    emit("    bytes internal constant BIP341_RAW_UNSIGNED_TX = %s;" % hexlit(raw))
    for k_, v in b341["intermediary"].items():
        emit("    bytes32 internal constant BIP341_%s = 0x%s;" % (k_.upper(), v))
    emit("\n    function bip341Spent() internal pure returns (Prevout[] memory s) {")
    emit("        s = new Prevout[](%d);" % len(spent))
    for i, (a, s) in enumerate(spent):
        emit("        s[%d] = Prevout(%d, %s, 0);" % (i, a, hexlit(s)))
    emit("    }\n")
    ins = b341["inputSpending"]
    emit("    uint256 internal constant BIP341_INPUT_COUNT = %d;\n" % len(ins))
    emit("    function bip341Input(uint256 i) internal pure returns (KeyPathInput memory) {")
    for j, v in enumerate(ins):
        m = bip341_msg(tx, spent, v["txinIndex"], v["hashType"])
        assert m.hex() == v["sigMsg"], j
        sh = tagged("TapSighash", m)
        assert sh.hex() == v["sigHash"], j
        sig = bytes.fromhex(v["witness"][0])
        assert schnorr_ok(spent[v["txinIndex"]][1][2:], sh, sig[:64])
        emit("        if (i == %d) return KeyPathInput(%d, %d, %s, 0x%s, %s);" % (
            j, v["txinIndex"], v["hashType"], hexlit(m), sh.hex(), hexlit(sig)))
    emit('        revert("no such vector");\n    }')
    emit("}")
    write("BipVectors.sol", out)
    return len(b340), len(ins)


# ---------------------------------------------------------------- differential cases
# The reference's cases are many (thousands), so they are not compiled into
# Solidity: each becomes one line of hex ABI encoding in a data file, which the
# test reads with vm.readLine and abi.decode. The first line is the count. No
# reference directory: no files, and the test skips.

def _dynamic(t):
    if t in ("bytes", "string"):
        return True
    if isinstance(t, tuple):
        return t[0] == "array" or any(_dynamic(x) for x in t[1])
    return False


def _enc(t, v):
    if t in ("uint", "bool"):
        return int(v).to_bytes(32, "big")
    if t == "bytes32":
        assert len(v) == 32
        return v
    if t in ("bytes", "string"):
        b = v.encode() if t == "string" else v
        return len(b).to_bytes(32, "big") + b + b"\x00" * (-len(b) % 32)
    if t[0] == "array":
        return len(v).to_bytes(32, "big") + _enc_tuple([t[1]] * len(v), v)
    return _enc_tuple(t[1], v)


def _enc_tuple(types, values):
    heads, tails = [], b""
    size = 32 * len(types)  # every static type used here is one word
    for t, v in zip(types, values):
        e = _enc(t, v)
        if _dynamic(t):
            heads.append((size + len(tails)).to_bytes(32, "big"))
            tails += e
        else:
            heads.append(e)
    return b"".join(heads) + tails


PREVOUT_T = ("tuple", ["uint", "bytes", "uint"])
REF_T = ("tuple", ["string", "bytes", ("array", PREVOUT_T), ("array", PREVOUT_T), ("array", "bytes32"),
                   ("array", "uint"), "uint", "bool", "uint", "uint", "string", "bool"])
DEPOSIT_T = ("tuple", ["string", "uint", "uint", "uint", "uint", "bytes", "uint", "bool", "bytes32"])


def ref_prevouts(pvs):
    out = []
    for p in pvs:
        if isinstance(p, dict):
            if p.get("mint"):
                return None
            out.append({"amount": int(p["amount"]), "script": p["script"], "height": int(p["height"])})
        else:
            out.append({"amount": int(p[0]), "script": p[1], "height": int(p[2])})
    return out


REF_PARTS = 16  # files, one per test function (test/Reference.t.sol)


def gen_reference(rdir):
    """Returns (tx cases, deposit cases, cases where the model disagrees with the reference)."""
    txs, deps, disagree = [], [], []
    if rdir and os.path.isdir(rdir):
        for path in sorted(glob.glob(os.path.join(rdir, "*.jsonl"))):
            for n, e in enumerate(load_jsonl(path)):
                name = "%s #%d %s" % (os.path.basename(path), n, e.get("origin", e.get("name", "")))
                if "value_wei" in e:  # a deposit case
                    deps.append((name, int(e["chain_id"]), int(e["contract"], 16), int(e["nonce"]),
                                 int(e["value_wei"]), bytes.fromhex(e["script"]), int(e["height"]), bool(e["valid"]),
                                 bytes.fromhex(e["txid"])[::-1] if e.get("txid") else b"\x00" * 32))
                    continue
                pv = ref_prevouts(e["prevouts"])
                if pv is None:
                    continue
                raw = bytes.fromhex(e["tx"])
                t = lenient_parse(raw)
                seeds = [(i[0], i[1], p) for i, p in zip(t.ins, pv)] if t else []
                g = int(e.get("genesis_time") or GENESIS_TIME)
                iv = int(e.get("interval") or INTERVAL)
                ours = model(raw, pv, int(e["height"]),
                             seeds={utxo_key(a, b): commitment(p["amount"], bytes.fromhex(p["script"]), p["height"])
                                    for a, b, p in seeds}, g=g, i=iv)
                if (ours is None) != bool(e["valid"]):
                    disagree.append((name, ours, e.get("error")))
                to_pv = lambda q: [(x["amount"], bytes.fromhex(x["script"]), x["height"]) for x in q]
                txs.append((name, raw, to_pv(pv), to_pv([p for _, _, p in seeds]), [a for a, _, _ in seeds],
                            [b for _, b, _ in seeds], int(e["height"]), bool(e["valid"]), g, iv,
                            str(e.get("error") or ""), "ambiguous" in name))
    for old in glob.glob(os.path.join(OUT, "reference-*.hex")):
        os.remove(old)
    parts = [("reference-txs-%02d.hex" % k, txs[len(txs) * k // REF_PARTS:len(txs) * (k + 1) // REF_PARTS], REF_T)
             for k in range(REF_PARTS)]
    for fname, rows, typ in parts + [("reference-deposits.hex", deps, DEPOSIT_T)]:
        if not rows:
            continue
        with open(os.path.join(OUT, fname), "w") as f:
            f.write("%d\n" % len(rows))
            for r in rows:
                f.write("0x" + _enc_tuple([typ], [r]).hex() + "\n")
    return len(txs), len(deps), disagree


# ---------------------------------------------------------------- main

def main():
    global ROOT, OUT, DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--contracts", default=ROOT, help="the Foundry project (default contracts/evm)")
    ap.add_argument("--vectors", default=os.path.join(EVMCHAN, "tests", "vectors"))
    ap.add_argument("--reference", default=os.path.join(EVMCHAN, "reference", "cases"))
    a = ap.parse_args()
    ROOT = os.path.abspath(a.contracts)
    OUT = os.path.join(ROOT, "test", "generated")
    DATA = os.path.join(ROOT, "test", "data")
    rng = random.Random(4243)
    files = real_files(a.vectors)
    gen_real(files)
    real = [c for _, cs_ in files for c in cs_]
    bads = []
    for n, c in enumerate(real):
        bads += tampered_real(c, rng, n == 0)
    synth, synth_bad = synthetic(rng)
    worst = worst_case(rng)
    bads += synth_bad
    gen_synthetic(synth, bads, worst, deposit_vectors(rng))
    n340, n341 = gen_bip()
    nref, ndep, disagree = gen_reference(a.reference)
    kinds = sorted({c.kind for c in real})
    print("real: %d txs in %d files (%s)" % (len(real), len(files), ", ".join(kinds)))
    print("synthetic: %d valid, %d bad (%d from real txs); worst case %d outputs, %d bytes" % (
        len(synth), len(bads), len(bads) - len(synth_bad), len(worst.tx.outs), len(worst.tx.ser())))
    print("bip340: %d, bip341 key-path inputs: %d" % (n340, n341))
    print("reference: %d tx cases, %d deposit cases; the model disagrees on %d (%d of them 'ambiguous')" % (
        nref, ndep, len(disagree), sum("ambiguous" in d[0] for d in disagree)))
    for d in disagree:
        if "ambiguous" not in d[0]:
            print("  model disagrees with the reference:", d)


if __name__ == "__main__":
    main()
