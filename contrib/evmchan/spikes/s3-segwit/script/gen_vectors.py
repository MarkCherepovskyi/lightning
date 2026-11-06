# SPDX-License-Identifier: MIT
"""Decode the CLN regtest transactions of spike S1 and generate test/Vectors.sol.

Usage:
  python -I gen_vectors.py describe <cln-regtest-txs.jsonl>
  python -I gen_vectors.py solidity <cln-regtest-txs.jsonl> > ../test/Vectors.sol

`describe` lists every transaction and what each input spends (template and
spend path). `solidity` writes the Solidity vectors: every non-mint real tx with
its prevouts and the hints the contract needs, plus synthetic spends (signed here
with coincurve) for the template paths and sighash types the real txs lack.

Everything the contract will be asked to accept is first checked here with an
independent implementation: BIP143 and BIP341 sighashes, strict DER, and
coincurve (libsecp256k1) verification of every signature.

Hints, consumed by the contract in the order it checks signatures (inputs in
order; within an input, signatures in witness order):
  ECDSA:   1 byte recovery id (0 or 1), then the 32-byte Y of the public key
  Schnorr: the 32-byte even Y of the signature's R
"""
import hashlib
import json
import random
import struct
import sys

from coincurve import PrivateKey, PublicKey, PublicKeyXOnly

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# The virtual clock of the S1 toy chain (chaind.py): block h has time GENESIS + h * INTERVAL.
GENESIS_TIME = 1700000000
INTERVAL = 600


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


class Reader:
    def __init__(self, b):
        self.b, self.o = b, 0

    def take(self, n):
        assert self.o + n <= len(self.b), "truncated"
        r = self.b[self.o:self.o + n]
        self.o += n
        return r

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8))[0]

    def cs(self):
        f = self.take(1)[0]
        if f < 0xFD:
            return f
        n = int.from_bytes(self.take({0xFD: 2, 0xFE: 4, 0xFF: 8}[f]), "little")
        assert cs(n)[0] == f, "non-canonical compact size"
        return n

    def var(self):
        return self.take(self.cs())


class Tx:
    """ins: (txid in wire order, vout, scriptSig, sequence); outs: (amount, spk)."""

    def __init__(self, version, ins, outs, locktime, wit=None):
        self.version, self.ins, self.outs, self.locktime = version, ins, outs, locktime
        self.wit = wit if wit is not None else [[] for _ in ins]

    @classmethod
    def parse(cls, raw):
        r = Reader(raw)
        version = r.u32()
        segwit = raw[4] == 0
        if segwit:
            assert r.take(2) == b"\x00\x01"
        ins = [(r.take(32), r.u32(), r.var(), r.u32()) for _ in range(r.cs())]
        outs = [(r.u64(), r.var()) for _ in range(r.cs())]
        wit = [[r.var() for _ in range(r.cs())] for _ in ins] if segwit else None
        locktime = r.u32()
        assert r.o == len(raw), "trailing bytes"
        return cls(version, ins, outs, locktime, wit)

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
        return dsha(self.ser(False))[::-1].hex()


def ser_out(amt, spk):
    return struct.pack("<Q", amt) + cs(len(spk)) + spk


# ---------------------------------------------------------------- sighashes

def bip143(tx, idx, script_code, amount, ht):
    """BIP143 sighash. SINGLE with idx >= len(outs) uses a zero hashOutputs (BIP143)."""
    assert ht in (1, 2, 3, 0x81, 0x82, 0x83)
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


def bip341(tx, spent, idx, ht):
    """BIP341 key-path TapSighash (ext_flag 0, no annex)."""
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
    return tagged("TapSighash", m)


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
    d = parse_der(sig)
    if d is None or not 0 < d[0] < N or not 0 < d[1] <= N // 2:
        return False
    try:
        return PublicKey(pub33).verify(sig[:-1], digest, hasher=None)
    except Exception:
        return False


def ecdsa_hint(pub33, digest, sig):
    """recovery id (1 byte) || Y of the public key (32 bytes)."""
    r, s, _ = parse_der(sig)
    rs = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    for rec in (0, 1):
        try:
            q = PublicKey.from_signature_and_message(rs + bytes([rec]), digest, hasher=None)
        except Exception:
            continue
        if q.format() == pub33:
            return bytes([rec]) + PublicKey(pub33).format(compressed=False)[33:]
    raise AssertionError("no recovery id (R.x >= n?)")


def lift_x_even_y(x):
    c = (pow(x, 3, P) + 7) % P
    y = pow(c, (P + 1) // 4, P)
    assert y * y % P == c
    return y if y % 2 == 0 else P - y


def schnorr_hint(sig):
    return lift_x_even_y(int.from_bytes(sig[:32], "big")).to_bytes(32, "big")


def sign_ecdsa(key, digest, ht):
    return key.sign(digest, hasher=None) + bytes([ht])


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
    for k, lim in ((1, 0x7F), (2, 0x7FFF), (3, 0x7FFFFF), (4, 0x7FFFFFFF), (5, 0x7FFFFFFFFF)):
        if n <= lim:
            return push(n.to_bytes(8, "little")[:k])


def read_num(s, o):
    """Inverse of num() at s[o]: (value, next offset), or None if not minimal/positive."""
    op = s[o]
    if op == 0:
        return 0, o + 1
    if 0x51 <= op <= 0x60:
        return op - 0x50, o + 1
    if 1 <= op <= 5:
        v = int.from_bytes(s[o + 1:o + 1 + op], "little")
        return (v, o + 1 + op) if num(v) == s[o:o + 1 + op] else None
    return None


def redeem_2of2(k1, k2):
    a, b = sorted([k1, k2])
    return b"\x52" + push(a) + push(b) + b"\x52\xae"


def wscript_to_local(delay, rev, delayed):
    return b"\x63" + push(rev) + b"\x67" + num(delay) + b"\xb2\x75" + push(delayed) + b"\x68\xac"


def wscript_to_remote_anchored(rk, csv):
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
    """Recognise a CLN witness script by rebuilding it from the fields it contains."""
    if len(ws) == 71 and ws[0] == 0x52 and ws[1] == 33 and ws[35] == 33:
        if redeem_2of2(ws[2:35], ws[36:69]) == ws:
            return "funding", {"k1": ws[2:35], "k2": ws[36:69]}
    if len(ws) >= 40 and ws[0] == 0x63 and ws[1] == 33:
        r = read_num(ws, 36)
        if r and wscript_to_local(r[0], ws[2:35], ws[r[1] + 3:r[1] + 36]) == ws:
            return "to_local", {"delay": r[0], "rev": ws[2:35], "delayed": ws[r[1] + 3:r[1] + 36]}
    if ws[0] == 33 and len(ws) == 40 and wscript_anchor(ws[1:34]) == ws:
        return "anchor", {"key": ws[1:34]}
    if ws[0] == 33 and len(ws) >= 37:
        r = read_num(ws, 35)
        if r and wscript_to_remote_anchored(ws[1:34], r[0]) == ws:
            return "to_remote", {"key": ws[1:34], "csv": r[0]}
    if len(ws) >= 136 and ws[:3] == b"\x76\xa9\x14" and ws[27] == 33:
        revh, rk = ws[3:23], ws[28:61]
        lk, ph = ws[71:104], ws[109:129]
        if wscript_htlc_offer(lk, rk, ph, revh) == ws:
            return "htlc_offered", {"revh": revh, "rk": rk, "lk": lk, "ph": ph}
        ph, lk = ws[69:89], ws[93:126]
        r = read_num(ws, 130)
        if r and wscript_htlc_receive(r[0], lk, rk, ph, revh) == ws:
            return "htlc_received", {"revh": revh, "rk": rk, "lk": lk, "ph": ph, "cltv": r[0]}
    return "unknown", {}


# ---------------------------------------------------------------- analysis of one input

def p2wpkh_code(h20):
    return b"\x76\xa9\x14" + h20 + b"\x88\xac"


class Checker:
    """Checks a whole tx the way the contract will, and collects the hints."""

    def __init__(self, tx, prevouts):
        self.tx, self.prevouts = tx, prevouts
        self.spent = [(p["amount"], bytes.fromhex(p["script"])) for p in prevouts]
        self.hints = b""
        self.notes = []  # per input: (kind, path, [hash types])

    def sig(self, idx, pub, sig, code):
        assert pub[0] in (2, 3) and len(pub) == 33
        d = parse_der(sig)
        assert d is not None, "bad DER"
        digest = bip143(self.tx, idx, code, self.spent[idx][0], d[2])
        assert ecdsa_ok(pub, digest, sig), "bad ECDSA signature"
        self.hints += ecdsa_hint(pub, digest, sig)
        return d[2]

    def check(self):
        for i in range(len(self.tx.ins)):
            self.notes.append(self.check_input(i))
        return self.hints

    def check_input(self, i):
        amount, spk = self.spent[i]
        w = self.tx.wit[i]
        assert self.tx.ins[i][2] == b"", "scriptSig"
        if len(spk) == 22 and spk[:2] == b"\x00\x14":
            assert len(w) == 2 and hash160(w[1]) == spk[2:]
            return "p2wpkh", "key", [self.sig(i, w[1], w[0], p2wpkh_code(spk[2:]))]
        if len(spk) == 34 and spk[:2] == b"\x51\x20":
            assert len(w) == 1 and len(w[0]) in (64, 65)
            ht = w[0][64] if len(w[0]) == 65 else 0
            digest = bip341(self.tx, self.spent, i, ht)
            assert PublicKeyXOnly(spk[2:]).verify(w[0][:64], digest), "bad schnorr"
            self.hints += schnorr_hint(w[0])
            return "p2tr", "key", [ht]
        assert len(spk) == 34 and spk[:2] == b"\x00\x20", "unsupported scriptPubKey"
        ws = w[-1]
        assert sha256(ws) == spk[2:]
        kind, f = classify(ws)
        st = w[:-1]
        seq = self.tx.ins[i][3]
        if kind == "funding":
            assert len(st) == 3 and st[0] == b""
            k1, k2 = f["k1"], f["k2"]
            return kind, "2-of-2", [self.sig(i, k1, st[1], ws), self.sig(i, k2, st[2], ws)]
        if kind == "to_local":
            assert len(st) == 2
            if st[1] == b"\x01":
                return kind, "revocation", [self.sig(i, f["rev"], st[0], ws)]
            assert st[1] == b"" and seq & 0xFFFF >= f["delay"]
            return kind, "delayed (CSV %d)" % f["delay"], [self.sig(i, f["delayed"], st[0], ws)]
        if kind == "to_remote":
            assert len(st) == 1
            return kind, "key (CSV %d)" % f["csv"], [self.sig(i, f["key"], st[0], ws)]
        if kind == "anchor":
            assert len(st) == 1
            if st[0] == b"":
                return kind, "anyone (CSV 16)", []
            return kind, "key", [self.sig(i, f["key"], st[0], ws)]
        if kind in ("htlc_offered", "htlc_received"):
            x = st[-1]
            if hash160(x) == f["revh"]:
                assert len(st) == 2
                return kind, "revocation", [self.sig(i, x, st[0], ws)]
            preimage = len(x) == 32
            if preimage:
                assert hash160(x) == f["ph"], "bad preimage"
            two_of_two = preimage == (kind == "htlc_received")
            if two_of_two:
                assert len(st) == 4 and st[0] == b""
                path = "HTLC-success tx (2-of-2 + preimage)" if preimage else "HTLC-timeout tx (2-of-2)"
                return kind, path, [self.sig(i, f["rk"], st[1], ws), self.sig(i, f["lk"], st[2], ws)]
            assert len(st) == 2
            if preimage:
                return kind, "remote with preimage", [self.sig(i, f["rk"], st[0], ws)]
            assert self.tx.locktime >= f["cltv"]
            return kind, "remote after timeout (CLTV %d)" % f["cltv"], [self.sig(i, f["rk"], st[0], ws)]
        raise AssertionError("unknown witness script " + ws.hex())


def min_height(tx, prevouts):
    """The lowest height the tx is valid at (nLockTime and BIP68), as the contract computes."""
    h = max(p["height"] for p in prevouts)
    final = all(q == 0xFFFFFFFF for _, _, _, q in tx.ins)
    if not final and tx.locktime:
        if tx.locktime < 500_000_000:
            h = max(h, tx.locktime + 1)
        else:
            h = max(h, (tx.locktime - GENESIS_TIME) // INTERVAL + 1)
    if tx.version >= 2:
        for (_, _, _, q), p in zip(tx.ins, prevouts):
            if not q & (1 << 31):
                assert not q & (1 << 22), "time-based relative lock"
                h = max(h, p["height"] + (q & 0xFFFF))
    return h


def load(path):
    return [json.loads(line) for line in open(path) if line.strip()]


def tx_kind(tx, notes):
    kinds = [k for k, _, _ in notes]
    if kinds == ["funding"]:
        if len(tx.outs) >= 2 and any(len(s) == 34 and s[:2] == b"\x00\x20" for _, s in tx.outs) and \
                tx.locktime >> 24 == 0x20:
            return "commitment"
        return "mutual close"
    if "htlc_offered" in kinds or "htlc_received" in kinds:
        return "HTLC tx" if any(n[1].endswith("tx (2-of-2)") or "success" in n[1] for n in notes) else "HTLC claim"
    if "to_local" in kinds:
        return "to_local sweep"
    if any(len(s) == 34 and s[:2] == b"\x00\x20" for _, s in tx.outs):
        return "wallet funding tx"
    return "wallet spend"


def describe(path):
    for n, e in enumerate(load(path)):
        tx = Tx.parse(bytes.fromhex(e["tx"]))
        assert tx.txid() == e["txid"]
        if any(p.get("mint") for p in e["prevouts"]):
            print("#%d h=%d %s… deposit (mint), skipped" % (n, e["height"], e["txid"][:16]))
            continue
        c = Checker(tx, e["prevouts"])
        c.check()
        print("#%d h=%d %s… %s: v%d locktime=%#x, %d in, %d out, %d bytes, valid from height %d" % (
            n, e["height"], e["txid"][:16], tx_kind(tx, c.notes), tx.version, tx.locktime, len(tx.ins),
            len(tx.outs), len(bytes.fromhex(e["tx"])), min_height(tx, e["prevouts"])))
        for i, ((kind, path_, hts), p) in enumerate(zip(c.notes, e["prevouts"])):
            print("    in %d: seq=%#010x spends %s (%d sat, created h=%d): %s, sighash %s" % (
                i, tx.ins[i][3], kind, p["amount"], p["height"], path_, ",".join("%#x" % h for h in hts) or "-"))
        for j, (a, s) in enumerate(tx.outs):
            print("    out %d: %d sat %s" % (j, a, s.hex()))



# ---------------------------------------------------------------- Solidity output

def err(name, *args):
    """Solidity expression for the expected revert data."""
    a = "".join(", uint256(%d)" % x for x in args)
    return "abi.encodeWithSelector(%s.selector%s)" % (name, a)


def tx_level_error(tx, prevouts, h):
    """The first tx-level check (in the contract's order) that fails at height h, or None."""
    final = all(q == 0xFFFFFFFF for _, _, _, q in tx.ins)
    lt = tx.locktime
    if lt and not final and not (lt < h if lt < 500_000_000 else lt < GENESIS_TIME + h * INTERVAL):
        return err("NonFinal")
    for i, ((_, _, _, q), p) in enumerate(zip(tx.ins, prevouts)):
        if p["height"] > h:
            return err("PrevoutFromFuture", i)
        if tx.version >= 2 and not q & (1 << 31) and h < p["height"] + (q & 0xFFFF):
            return err("RelativeLockNotMet", i)
    return None


class Case:
    def __init__(self, name, kind, tx, prevouts, height, hints=None, error=None):
        self.name, self.kind, self.tx, self.prevouts, self.height = name, kind, tx, prevouts, height
        if hints is None:
            hints = Checker(tx, prevouts).check()
        self.hints, self.error = hints, error

    def clone(self, name, error, tx=None, prevouts=None, height=None, hints=None):
        return Case(name, self.kind, tx or self.tx, prevouts or self.prevouts,
                    self.height if height is None else height, self.hints if hints is None else hints, error)


def sol_prevouts(var, prevouts, emit):
    emit("            %s = new Prevout[](%d);" % (var, len(prevouts)))
    for i, p in enumerate(prevouts):
        emit('            %s[%d] = Prevout(%d, hex"%s", %d);' % (var, i, p["amount"], p["script"], p["height"]))


def copy_tx(tx):
    return Tx(tx.version, list(tx.ins), list(tx.outs), tx.locktime, [list(w) for w in tx.wit])


def copy_prevouts(pv):
    return [dict(p) for p in pv]


def p2wsh(ws):
    return (b"\x00\x20" + sha256(ws)).hex()


def first_sig_pos(tx, idx, prevouts):
    """Index in the witness of input idx of its first signature."""
    spk = bytes.fromhex(prevouts[idx]["script"])
    w = tx.wit[idx]
    if spk[:2] == b"\x00\x20" and classify(w[-1])[0] == "funding":
        return 1
    if spk[:2] == b"\x00\x20" and len(w) == 5:
        return 1
    return 0


def flip_sig(tx, idx, pos):
    t = copy_tx(tx)
    sig = bytearray(t.wit[idx][pos])
    sig[63 if len(sig) in (64, 65) and t.wit[idx][pos][0] != 0x30 else len(sig) - 2] ^= 1
    t.wit[idx][pos] = bytes(sig)
    return t


def der(r, s, ht):
    def i(v):
        b = v.to_bytes(33, "big").lstrip(b"\x00")
        return b"\x00" + b if b[0] & 0x80 else b
    body = b"\x02" + bytes([len(i(r))]) + i(r) + b"\x02" + bytes([len(i(s))]) + i(s)
    return b"\x30" + bytes([len(body)]) + body + bytes([ht])


def tampered_real(c, rng):
    """Tampered copies of a real tx, each with the revert it must cause."""
    out = []
    tx, pv = c.tx, c.prevouts
    pos = first_sig_pos(tx, 0, pv)
    spk0 = bytes.fromhex(pv[0]["script"])
    out.append(c.clone(c.name + ": flipped signature byte", err("BadSignature", 0), tx=flip_sig(tx, 0, pos)))
    t = copy_tx(tx)
    t.outs[0] = (t.outs[0][0] - 1, t.outs[0][1])
    out.append(c.clone(c.name + ": output 0 amount - 1", err("BadSignature", 0), tx=t))
    t = copy_tx(tx)
    t.outs[0] = (t.outs[0][0] + sum(p["amount"] for p in pv), t.outs[0][1])
    out.append(c.clone(c.name + ": outputs exceed inputs", err("OutputsExceedInputs"), tx=t))
    q = copy_prevouts(pv)
    q[0]["amount"] += 1
    out.append(c.clone(c.name + ": prevout amount + 1", err("BadSignature", 0), prevouts=q))
    q = copy_prevouts(pv)
    bad = bytearray(spk0)
    bad[-1] ^= 1
    q[0]["script"] = bad.hex()
    e = {0x00: "KeyHashMismatch", 0x51: "BadSignature"}[spk0[0]] if len(spk0) == 22 or spk0[0] == 0x51 \
        else "WitnessScriptMismatch"
    out.append(c.clone(c.name + ": prevout script changed", err(e, 0), prevouts=q))
    if c.hints:
        h = bytearray(c.hints)
        h[0] ^= 1
        out.append(c.clone(c.name + ": wrong first hint byte", err("BadSignature", 0), hints=bytes(h)))
        out.append(c.clone(c.name + ": a hint byte too many", err("UnusedHints"), hints=c.hints + b"\x00"))
        out.append(c.clone(c.name + ": last hint byte missing", err("MissingHint", len(tx.ins) - 1),
                           hints=c.hints[:-1]))
    # swap a key: a different key in the script (and the prevout committing to it)
    t, q = copy_tx(tx), copy_prevouts(pv)
    w = t.wit[0]
    other = PrivateKey(rng.randbytes(32)).public_key.format()
    if len(spk0) == 22:
        w[1] = other
        q[0]["script"] = "0014" + hash160(other).hex()
        hints = c.hints[:1] + PublicKey(other).format(compressed=False)[33:] + c.hints[33:]
        out.append(c.clone(c.name + ": other key (P2WPKH)", err("BadSignature", 0), tx=t, prevouts=q, hints=hints))
    elif spk0[:2] == b"\x00\x20":
        ws = w[-1]
        k, f = classify(ws)
        if k == "funding":
            nws = b"\x52" + push(f["k2"]) + push(f["k1"]) + b"\x52\xae"  # key order swapped
        elif k == "to_local":
            nws = wscript_to_local(f["delay"], f["delayed"], f["rev"])  # revocation and delayed keys swapped
        else:
            nws = wscript_htlc_offer(f["rk"], f["lk"], f["ph"], f["revh"])  # local and remote swapped
        w[-1] = nws
        q[0]["script"] = p2wsh(nws)
        out.append(c.clone(c.name + ": keys swapped in the witness script", err("BadSignature", 0), tx=t,
                           prevouts=q))
        if k == "funding":
            t2 = copy_tx(tx)
            t2.wit[0][0] = b"\x00"
            out.append(c.clone(c.name + ": non-empty CHECKMULTISIG dummy", err("BadWitness", 0), tx=t2))
        if k == "to_local":
            t2 = copy_tx(tx)
            t2.ins[0] = t2.ins[0][:3] + (f["delay"] - 1,)
            out.append(c.clone(c.name + ": nSequence below the CSV delay", err("CsvNotMet", 0), tx=t2))
    # high S and non-strict DER for the first ECDSA signature
    sig = tx.wit[0][pos]
    if sig[0] == 0x30:
        r, s_, ht = parse_der(sig)
        t = copy_tx(tx)
        t.wit[0][pos] = der(r, N - s_, ht)
        out.append(c.clone(c.name + ": high-S signature", err("BadSignatureEncoding", 0), tx=t))
        t = copy_tx(tx)
        lr = sig[3]
        t.wit[0][pos] = b"\x30" + bytes([sig[1] + 1]) + b"\x02" + bytes([lr + 1]) + b"\x00" + sig[4:]
        out.append(c.clone(c.name + ": R with a needless leading zero", err("BadSignatureEncoding", 0), tx=t))
    if c.name.startswith("#3 "):
        # consensus/standardness limits, checked before any script
        t = copy_tx(tx)
        t.outs[0] = (t.outs[0][0], b"\xae" * 201)  # 201 CHECKMULTISIG = 4020 sigops, cost 16080 + 4
        out.append(c.clone(c.name + ": output with 201 OP_CHECKMULTISIG", err("TooManySigops"), tx=t))
        t = copy_tx(tx)
        t.outs[0] = (t.outs[0][0], b"\x4d\xff\xff" + b"\xae" * 199 + b"\xac" * 2)  # pushed: 0 sigops
        out.append(c.clone(c.name + ": sigop bytes after a truncated push (counted 0, so a signature error)",
                           err("BadSignature", 0), tx=t))
        t = copy_tx(tx)
        t.outs[0] = (t.outs[0][0], b"\x6a" + b"\x00" * 100_000)
        out.append(c.clone(c.name + ": over 100,000 bytes", err("TxTooLarge"), tx=t))
        t = copy_tx(tx)
        t.ins[0] = (b"\x00" * 32, 0xFFFFFFFF) + t.ins[0][2:]
        out.append(c.clone(c.name + ": null prevout", err("NullPrevout", 0), tx=t))
    if len(tx.ins) > 1:
        q = copy_prevouts(pv)
        for p_ in q:
            p_["amount"] = 21_000_000 * 100_000_000
        out.append(c.clone(c.name + ": inputs summing above MAX_MONEY", err("AmountTooLarge"), prevouts=q))
        t = copy_tx(tx)
        t.ins[1] = t.ins[0][:2] + t.ins[1][2:]
        out.append(c.clone(c.name + ": input 1 spends input 0's outpoint", err("DuplicateInput", 1), tx=t))
        t = copy_tx(tx)
        t.outs[1] = (t.outs[1][0] - 1, t.outs[1][1])
        # input 0's SINGLE|ANYONECANPAY signatures do not cover output 1; input 1's DEFAULT one does
        out.append(c.clone(c.name + ": output 1 amount - 1", err("BadSignature", 1), tx=t))
    return out


# ---------------------------------------------------------------- synthetic spends

class Synth:
    def __init__(self, rng):
        self.rng = rng

    def key(self):
        return PrivateKey(self.rng.randbytes(32))

    def outpoint(self):
        return self.rng.randbytes(32), self.rng.randrange(0, 4)

    def p2tr(self):
        return b"\x51\x20" + self.rng.randbytes(32)

    def spend(self, ws, amount, h0, seq, locktime, make_stack, extra_outs=()):
        """A 1-input tx spending P2WSH(ws); make_stack(tx, digest_fn) returns the stack without ws."""
        txid, vout = self.outpoint()
        tx = Tx(2, [(txid, vout, b"", seq)], [(amount - 300, self.p2tr())] + list(extra_outs), locktime)
        digest = lambda ht: bip143(tx, 0, ws, amount, ht)
        tx.wit = [make_stack(tx, digest) + [ws]]
        pv = [{"amount": amount, "script": p2wsh(ws), "height": h0}]
        return tx, pv


def synthetic(rng):
    """(cases, bad cases, sighash vectors) made with coincurve for the paths the real txs lack."""
    S = Synth(rng)
    k = {n: S.key() for n in ("rev", "delayed", "remote", "local", "fund", "wallet")}
    pub = {n: v.public_key.format() for n, v in k.items()}
    sig = lambda name, ht=1: (lambda d: sign_ecdsa(k[name], d(ht), ht))
    preimage = rng.randbytes(32)
    ph = hashlib.new("ripemd160", sha256(preimage)).digest()
    revh = hash160(pub["rev"])
    cases, bad = [], []
    h0 = 1000

    def make(name, kind, ws, seq, lt, stack):
        tx, pv = S.spend(ws, 50_000, h0, seq, lt, stack)
        return Case("synthetic " + name, kind, tx, pv, min_height(tx, pv))

    def add(*args):
        c = make(*args)
        cases.append(c)
        return c

    tl = wscript_to_local(144, pub["rev"], pub["delayed"])
    add("to_local delayed, CSV 144", "to_local sweep", tl, 144, 0, lambda t, d: [sig("delayed")(d), b""])
    c = add("to_local revocation", "penalty (to_local)", tl, 0xFFFFFFFD, 0, lambda t, d: [sig("rev")(d), b"\x01"])
    t = copy_tx(c.tx)
    t.wit[0][1] = b"\x02"
    bad.append(c.clone(c.name + ": IF argument 0x02 (MINIMALIF)", err("BadWitness", 0), tx=t))

    trm = wscript_to_remote_anchored(pub["remote"], 1)
    c = add("to_remote, CSV 1", "to_remote sweep", trm, 1, 0, lambda t, d: [sig("remote")(d)])
    # CHECKSIGVERIFY comes before CSV, so re-sign with the lower nSequence
    c = make("to_remote, CSV 1", "", trm, 0, 0, lambda t, d: [sig("remote")(d)])
    bad.append(c.clone(c.name + ": nSequence 0 (re-signed)", err("CsvNotMet", 0), height=h0 + 1))

    an = wscript_anchor(pub["fund"])
    add("anchor, key", "anchor spend", an, 0xFFFFFFFD, 0, lambda t, d: [sig("fund")(d)])
    c = add("anchor, anyone after 16", "anchor spend", an, 16, 0, lambda t, d: [b""])
    t = copy_tx(c.tx)
    t.ins[0] = t.ins[0][:3] + (15,)
    bad.append(c.clone(c.name + ": nSequence 15", err("CsvNotMet", 0), tx=t))
    t = copy_tx(c.tx)
    t.wit[0][0] = b"\x30\x01"
    bad.append(c.clone(c.name + ": garbage signature (NULLFAIL)", err("BadSignatureEncoding", 0), tx=t))

    off = wscript_htlc_offer(pub["local"], pub["remote"], ph, revh)
    c = add("offered HTLC, revocation", "penalty (HTLC)", off, 0xFFFFFFFD, 0,
            lambda t, d: [sig("rev")(d), pub["rev"]])
    t = copy_tx(c.tx)
    t.wit[0][1] = pub["local"]
    bad.append(c.clone(c.name + ": a key that is not the revocation key", err("BadWitness", 0), tx=t))
    c = add("offered HTLC, remote with preimage", "HTLC claim", off, 1, 0, lambda t, d: [sig("remote")(d), preimage])
    t = copy_tx(c.tx)
    t.wit[0][1] = bytes([preimage[0] ^ 1]) + preimage[1:]
    bad.append(c.clone(c.name + ": wrong preimage", err("BadPreimage", 0), tx=t))

    rcv = wscript_htlc_receive(70_000, pub["local"], pub["remote"], ph, revh)
    add("received HTLC, revocation", "penalty (HTLC)", rcv, 0xFFFFFFFD, 0, lambda t, d: [sig("rev")(d), pub["rev"]])
    c = add("received HTLC, HTLC-success tx", "HTLC tx", rcv, 1, 0,
            lambda t, d: [b"", sig("remote", 0x83)(d), sig("local")(d), preimage])
    t = copy_tx(c.tx)
    t.wit[0][3] = rng.randbytes(32)
    bad.append(c.clone(c.name + ": wrong preimage", err("BadPreimage", 0), tx=t))
    c = add("received HTLC, remote after CLTV 70000", "HTLC claim", rcv, 1, 70_000,
            lambda t, d: [sig("remote")(d), b""])
    t = copy_tx(c.tx)
    t.locktime = 69_999
    bad.append(c.clone(c.name + ": nLockTime below the CLTV", err("CltvNotMet", 0), tx=t))
    t = copy_tx(c.tx)
    t.ins[0] = t.ins[0][:3] + (0xFFFFFFFF,)
    bad.append(c.clone(c.name + ": final nSequence", err("CltvNotMet", 0), tx=t))

    # Every BIP143 hash type, on P2WPKH inputs: 6 inputs, 2 outputs, so inputs 4 and 5 sign
    # SINGLE without a matching output (zero hashOutputs).
    hts = [0x02, 0x03, 0x81, 0x82, 0x03, 0x83]
    keys = [S.key() for _ in hts]
    ins = [S.outpoint() + (b"", 0xFFFFFFFD) for _ in hts]
    amounts = [rng.randrange(10_000, 100_000) for _ in hts]
    tx = Tx(2, ins, [(30_000, S.p2tr()), (20_000, S.p2tr())], h0 - 1)
    pv = [{"amount": a, "script": "0014" + hash160(kk.public_key.format()).hex(), "height": h0 - 10}
          for a, kk in zip(amounts, keys)]
    sighashes = []
    for i, (ht, kk) in enumerate(zip(hts, keys)):
        code = p2wpkh_code(hash160(kk.public_key.format()))
        d = bip143(tx, i, code, amounts[i], ht)
        tx.wit[i] = [sign_ecdsa(kk, d, ht), kk.public_key.format()]
        sighashes.append((i, ht, code, amounts[i], d))
    c = Case("synthetic P2WPKH, hash types NONE SINGLE ALL|ACP NONE|ACP SINGLE SINGLE|ACP", "wallet spend", tx, pv,
             min_height(tx, pv))
    cases.append(c)
    # NONE (input 0) covers no output and SINGLE (input 1) only output 1, so changing output 0
    # first breaks input 2 (ALL|ACP); changing output 1 first breaks input 1.
    t = copy_tx(tx)
    t.outs[0] = (t.outs[0][0] - 1, t.outs[0][1])
    bad.append(c.clone(c.name + ": output 0 amount - 1", err("BadSignature", 2), tx=t))
    t = copy_tx(tx)
    t.outs[1] = (t.outs[1][0] - 1, t.outs[1][1])
    bad.append(c.clone(c.name + ": output 1 amount - 1", err("BadSignature", 1), tx=t))
    return cases, bad, (tx, sighashes)


def solidity(path):
    rng = random.Random(143)
    real = []
    htlc_txids = set()
    for n, e in enumerate(load(path)):
        if any(p.get("mint") for p in e["prevouts"]):
            continue
        tx = Tx.parse(bytes.fromhex(e["tx"]))
        assert tx.txid() == e["txid"]
        ch = Checker(tx, e["prevouts"])
        ch.check()
        kind = tx_kind(tx, ch.notes)
        if kind == "to_local sweep" and tx.ins[0][0][::-1].hex() in htlc_txids:
            kind = "HTLC-tx output sweep"  # same script as to_local (bitcoin_wscript_htlc_tx)
        if kind == "HTLC tx":
            htlc_txids.add(e["txid"])
        c = Case("#%d %s" % (n, kind), kind, tx, e["prevouts"], e["height"])
        assert tx.ser() == bytes.fromhex(e["tx"])
        real.append(c)
    synth, synth_bad, (mtx, sighashes) = synthetic(rng)
    bad = [b for c in real for b in tampered_real(c, rng)] + synth_bad

    out = []
    emit = out.append
    emit("// SPDX-License-Identifier: MIT")
    emit("// Generated by script/gen_vectors.py from spike S1's CLN regtest transactions")
    emit("// (s1-toychain/vectors/cln-regtest-txs.jsonl) plus synthetic spends signed with coincurve. Do not edit.")
    emit("pragma solidity ^0.8.24;\n")
    emit('import {Prevout} from "../src/SegwitValidator.sol";')
    emit('import "../src/SegwitValidator.sol" as V;')
    emit('import "../src/Templates.sol" as T;\n')
    emit("struct Case {\n    string name;\n    string kind;\n    bytes rawTx;\n    Prevout[] prevouts;\n    bytes hints;")
    emit("    uint32 height; // the height it was applied at (real) or its lowest valid height (synthetic)")
    emit("    uint32 minHeight; // lowest height it is valid at")
    emit("    bytes earlyError; // the revert at minHeight - 1")
    emit("    bytes32 txid; // internal byte order")
    emit("    uint64 fee;\n}\n")
    emit("struct Bad {\n    string name;\n    bytes rawTx;\n    Prevout[] prevouts;\n    bytes hints;\n    uint32 height;")
    emit("    bytes err; // the expected revert data\n}\n")
    emit("struct SighashVector {\n    uint256 index;\n    uint8 hashType;\n    bytes scriptCode;\n    uint64 amount;")
    emit("    bytes32 sigHash;\n}\n")
    emit("library Vectors {")
    emit("    uint256 internal constant GENESIS_TIME = %d;" % GENESIS_TIME)
    emit("    uint256 internal constant INTERVAL = %d;" % INTERVAL)
    emit("    uint256 internal constant REAL_COUNT = %d;" % len(real))
    emit("    uint256 internal constant SYNTHETIC_COUNT = %d;" % len(synth))
    emit("    uint256 internal constant BAD_COUNT = %d;\n" % len(bad))

    def cases_fn(fname, cases):
        emit("    function %s(uint256 i) internal pure returns (Case memory c) {" % fname)
        for j, c in enumerate(cases):
            mh = min_height(c.tx, c.prevouts)
            assert mh <= c.height
            assert tx_level_error(c.tx, c.prevouts, c.height) is None
            early = tx_level_error(c.tx, c.prevouts, mh - 1)
            assert early is not None
            fee = sum(p["amount"] for p in c.prevouts) - sum(a for a, _ in c.tx.outs)
            emit("        if (i == %d) {" % j)
            emit('            c.name = "%s";' % c.name)
            emit('            c.kind = "%s";' % c.kind)
            emit('            c.rawTx = hex"%s";' % c.tx.ser().hex())
            sol_prevouts("c.prevouts", c.prevouts, emit)
            emit('            c.hints = hex"%s";' % c.hints.hex())
            emit("            c.height = %d;" % c.height)
            emit("            c.minHeight = %d;" % mh)
            emit("            c.earlyError = %s;" % early.replace("abi.encodeWithSelector(", "abi.encodeWithSelector(V."))
            emit("            c.txid = 0x%s;" % dsha(c.tx.ser(False)).hex())
            emit("            c.fee = %d;" % fee)
            emit("            return c;\n        }")
        emit('        revert("no such vector");\n    }\n')

    cases_fn("real", real)
    cases_fn("synthetic", synth)

    emit("    function bad(uint256 i) internal pure returns (Bad memory b) {")
    for j, b in enumerate(bad):
        emit("        if (i == %d) {" % j)
        emit('            b.name = "%s";' % b.name)
        emit('            b.rawTx = hex"%s";' % b.tx.ser().hex())
        sol_prevouts("b.prevouts", b.prevouts, emit)
        emit('            b.hints = hex"%s";' % b.hints.hex())
        emit("            b.height = %d;" % b.height)
        ns = "T." if b.error.split("(")[1].split(".")[0] in TEMPLATE_ERRORS else "V."
        emit("            b.err = %s;" % b.error.replace("abi.encodeWithSelector(", "abi.encodeWithSelector(" + ns))
        emit("            return b;\n        }")
    emit('        revert("no such vector");\n    }\n')

    emit('    bytes internal constant HASHTYPES_TX = hex"%s";' % mtx.ser().hex())
    emit("    uint256 internal constant SIGHASH_COUNT = %d;\n" % len(sighashes))
    emit("    function sighash(uint256 i) internal pure returns (SighashVector memory v) {")
    for j, (i, ht, code, amt, d) in enumerate(sighashes):
        emit('        if (i == %d) return SighashVector(%d, 0x%02x, hex"%s", %d, 0x%s);' % (j, i, ht, code.hex(), amt, d.hex()))
    emit('        revert("no such vector");\n    }')
    emit("}")
    print("\n".join(out))


TEMPLATE_ERRORS = {"BadSignatureEncoding", "BadSignature", "MissingHint", "KeyHashMismatch", "WitnessScriptMismatch",
                   "UnknownWitnessScript", "BadWitness", "BadPreimage", "CsvNotMet", "CltvNotMet"}


if __name__ == "__main__":
    if sys.argv[1:2] == ["describe"]:
        describe(sys.argv[2])
    elif sys.argv[1:2] == ["solidity"]:
        solidity(sys.argv[2])
    else:
        raise SystemExit("usage: gen_vectors.py describe|solidity <jsonl>")
