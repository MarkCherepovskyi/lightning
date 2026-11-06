# SPDX-License-Identifier: MIT
"""Just enough Bitcoin for the toy chain: segwit transactions (parse,
serialize, txid), block headers and merkle roots, bech32/bech32m addresses."""
import hashlib
import struct


def dsha(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def varint(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b'\xfd' + struct.pack('<H', n)
    if n <= 0xffffffff:
        return b'\xfe' + struct.pack('<I', n)
    return b'\xff' + struct.pack('<Q', n)


class Reader:
    def __init__(self, b):
        self.b, self.i = b, 0

    def take(self, n):
        if self.i + n > len(self.b):
            raise ValueError('truncated')
        r = self.b[self.i:self.i + n]
        self.i += n
        return r

    def u32(self):
        return struct.unpack('<I', self.take(4))[0]

    def u64(self):
        return struct.unpack('<Q', self.take(8))[0]

    def varint(self):
        n = self.take(1)[0]
        if n < 0xfd:
            return n
        return int.from_bytes(self.take({0xfd: 2, 0xfe: 4, 0xff: 8}[n]), 'little')

    def varbytes(self):
        return self.take(self.varint())


class Tx:
    """inputs: [(txid_hex, vout, script_sig, sequence)], outputs: [(sats, script)],
    witnesses: [[items]] (one list per input, maybe empty)."""

    def __init__(self, version, inputs, outputs, locktime, witnesses=None):
        self.version, self.inputs, self.outputs, self.locktime = version, inputs, outputs, locktime
        self.witnesses = witnesses or [[] for _ in inputs]

    @classmethod
    def parse(cls, raw):
        r = Reader(raw)
        version = r.u32()
        segwit = r.b[r.i] == 0 and r.b[r.i + 1] == 1
        if segwit:
            r.take(2)
        inputs = []
        for _ in range(r.varint()):
            txid = r.take(32)[::-1].hex()
            inputs.append((txid, r.u32(), r.varbytes(), r.u32()))
        outputs = [(r.u64(), r.varbytes()) for _ in range(r.varint())]
        witnesses = [[] for _ in inputs]
        if segwit:
            for i in range(len(inputs)):
                witnesses[i] = [r.varbytes() for _ in range(r.varint())]
        locktime = r.u32()
        if r.i != len(raw):
            raise ValueError('trailing bytes')
        return cls(version, inputs, outputs, locktime, witnesses)

    def serialize(self, witness=True):
        witness = witness and any(self.witnesses)
        out = struct.pack('<I', self.version) + (b'\x00\x01' if witness else b'')
        out += varint(len(self.inputs))
        for txid, vout, ss, seq in self.inputs:
            out += bytes.fromhex(txid)[::-1] + struct.pack('<I', vout) + varint(len(ss)) + ss + struct.pack('<I', seq)
        out += varint(len(self.outputs))
        for sats, spk in self.outputs:
            out += struct.pack('<Q', sats) + varint(len(spk)) + spk
        if witness:
            for w in self.witnesses:
                out += varint(len(w)) + b''.join(varint(len(x)) + x for x in w)
        return out + struct.pack('<I', self.locktime)

    def txid(self):
        return dsha(self.serialize(witness=False))[::-1].hex()


def coinbase(height):
    """A dummy coinbase, unique per height (BIP34-style height push)."""
    h = height.to_bytes((height.bit_length() + 8) // 8, 'little') if height else b'\x00'
    script_sig = bytes([len(h)]) + h + b'toychain'
    return Tx(2, [('00' * 32, 0xffffffff, script_sig, 0xffffffff)],
              [(0, b'\x6a')], 0)


def merkle_root(txids_hex):
    layer = [bytes.fromhex(t)[::-1] for t in txids_hex]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def header(prev_hash_hex, merkle, timestamp):
    return (struct.pack('<I', 0x20000000) + bytes.fromhex(prev_hash_hex)[::-1] + merkle
            + struct.pack('<III', timestamp, 0x207fffff, 0))


def block_hash(hdr):
    return dsha(hdr)[::-1].hex()


# ------------------------------------------------------------------ bech32
CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
BECH32M = 0x2bc830a3


def _polymod(values):
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data, frombits, tobits, pad=True):
    acc = bits = 0
    ret = []
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & ((1 << tobits) - 1))
    if pad and bits:
        ret.append((acc << (tobits - bits)) & ((1 << tobits) - 1))
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & ((1 << tobits) - 1))):
        raise ValueError('bad padding')
    return ret


def segwit_address(hrp, version, program):
    data = [version] + _convertbits(program, 8, 5)
    const = 1 if version == 0 else BECH32M
    values = _hrp_expand(hrp) + data
    pm = _polymod(values + [0] * 6) ^ const
    return hrp + '1' + ''.join(CHARSET[d] for d in data + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def address_script(addr):
    """scriptPubKey of a segwit address."""
    hrp, data = addr.lower().rsplit('1', 1)
    values = [CHARSET.index(c) for c in data]
    const = _polymod(_hrp_expand(hrp) + values)
    version = values[0]
    if const != (1 if version == 0 else BECH32M):
        raise ValueError('bad checksum')
    program = bytes(_convertbits(values[1:-6], 5, 8, False))
    return bytes([version + 0x50 if version else 0, len(program)]) + program
