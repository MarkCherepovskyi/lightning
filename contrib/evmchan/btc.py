# SPDX-License-Identifier: MIT
"""Bitcoin serialization for the EVM virtual chain (SPEC 3): transactions
(strict parse, serialize, txid), the coinbase of SPEC 3.2, headers, merkle
roots and blocks, the synthetic deposit tx of SPEC 3.3, and bech32/bech32m
addresses (including the segwit v16 exit address of an ETH account).

Byte order. A txid or block hash as hashed and as it appears inside a
serialization is "internal" order: the raw double SHA-256 bytes. Bitcoin
displays them reversed. In this module:
- `bytes` of length 32 are always internal order;
- hex strings are always display order (what CLN and bitcoind print);
- `display(b)` and `internal(s)` convert between the two.

Standard library only, except `deposit_prevout`, which needs keccak-256 from
evm.py.
"""
import hashlib
import struct

MAX_MONEY = 21_000_000 * 10**8
SEQUENCE_FINAL = 0xffffffff
HEADER_VERSION = 0x20000000
HEADER_BITS = 0x207fffff
COINBASE_TAG = b'evmchan'


def sha256(b):
    return hashlib.sha256(b).digest()


def dsha(b):
    return sha256(sha256(b))


def display(h32):
    """Internal-order 32 bytes -> display-order hex."""
    if len(h32) != 32:
        raise ValueError('need 32 bytes')
    return bytes(h32)[::-1].hex()


def internal(hex64):
    """Display-order hex -> internal-order 32 bytes."""
    b = bytes.fromhex(hex64)
    if len(b) != 32:
        raise ValueError('need 32 bytes of hex')
    return b[::-1]


def varint(n):
    if n < 0:
        raise ValueError('negative varint')
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b'\xfd' + struct.pack('<H', n)
    if n <= 0xffffffff:
        return b'\xfe' + struct.pack('<I', n)
    return b'\xff' + struct.pack('<Q', n)


class ParseError(ValueError):
    pass


class Reader:
    def __init__(self, b):
        self.b, self.i = bytes(b), 0

    def take(self, n):
        if self.i + n > len(self.b):
            raise ParseError('truncated')
        r = self.b[self.i:self.i + n]
        self.i += n
        return r

    def peek(self):
        if self.i >= len(self.b):
            raise ParseError('truncated')
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
            raise ParseError('non-canonical varint')
        return v

    def varbytes(self):
        return self.take(self.varint())


class TxIn:
    __slots__ = ('prev_txid', 'vout', 'script_sig', 'sequence')

    def __init__(self, prev_txid, vout, script_sig=b'', sequence=SEQUENCE_FINAL):
        if len(prev_txid) != 32:
            raise ValueError('prev_txid must be 32 internal-order bytes')
        self.prev_txid = bytes(prev_txid)   # internal byte order
        self.vout, self.script_sig, self.sequence = vout, bytes(script_sig), sequence

    def outpoint(self):
        return self.prev_txid + struct.pack('<I', self.vout)

    def __eq__(self, other):
        return isinstance(other, TxIn) and all(
            getattr(self, k) == getattr(other, k) for k in self.__slots__)


class Tx:
    """inputs: [TxIn], outputs: [(sats, scriptPubKey bytes)], witnesses: one list
    of items per input (an empty list for none)."""

    def __init__(self, version, inputs, outputs, locktime=0, witnesses=None):
        self.version, self.inputs, self.outputs, self.locktime = version, list(inputs), list(outputs), locktime
        self.witnesses = [list(w) for w in witnesses] if witnesses is not None else [[] for _ in self.inputs]
        if len(self.witnesses) != len(self.inputs):
            raise ValueError('one witness per input')

    @classmethod
    def parse(cls, raw):
        """Strict parse: canonical varints, no trailing bytes, marker and flag
        `00 01` present only when some witness is non-empty, at least one input
        and one output (SPEC 4.3.1, without the size limit)."""
        r = Reader(raw)
        tx = cls.read(r)
        if r.i != len(r.b):
            raise ParseError('trailing bytes')
        return tx

    @classmethod
    def read(cls, r):
        """Reads one tx from a Reader, with parse()'s rules but trailing bytes allowed."""
        version = r.u32()
        segwit = False
        if r.peek() == 0:
            # A zero input count is never valid, so 00 here is the marker.
            r.take(1)
            if r.take(1) != b'\x01':
                raise ParseError('bad segwit flag')
            segwit = True
        inputs = []
        for _ in range(r.varint()):
            prev = r.take(32)
            vout = r.u32()
            inputs.append(TxIn(prev, vout, r.varbytes(), r.u32()))
        if not inputs:
            raise ParseError('no inputs')
        outputs = []
        for _ in range(r.varint()):
            amount = r.u64()
            outputs.append((amount, r.varbytes()))
        if not outputs:
            raise ParseError('no outputs')
        witnesses = [[] for _ in inputs]
        if segwit:
            for i in range(len(inputs)):
                witnesses[i] = [r.varbytes() for _ in range(r.varint())]
            if not any(witnesses):
                raise ParseError('segwit marker with empty witnesses')
        locktime = r.u32()
        return cls(version, inputs, outputs, locktime, witnesses)

    def has_witness(self):
        return any(self.witnesses)

    def serialize(self, witness=True):
        witness = witness and self.has_witness()
        out = [struct.pack('<I', self.version)]
        if witness:
            out.append(b'\x00\x01')
        out.append(varint(len(self.inputs)))
        for i in self.inputs:
            out += [i.outpoint(), varint(len(i.script_sig)), i.script_sig, struct.pack('<I', i.sequence)]
        out.append(varint(len(self.outputs)))
        for amount, spk in self.outputs:
            out += [struct.pack('<Q', amount), varint(len(spk)), bytes(spk)]
        if witness:
            for w in self.witnesses:
                out.append(varint(len(w)))
                for item in w:
                    out += [varint(len(item)), bytes(item)]
        out.append(struct.pack('<I', self.locktime))
        return b''.join(out)

    def txid(self):
        """Internal-order txid: double SHA-256 of the non-witness serialization."""
        return dsha(self.serialize(witness=False))

    def txid_hex(self):
        """Display-order txid."""
        return display(self.txid())

    def is_coinbase(self):
        return len(self.inputs) == 1 and self.inputs[0].prev_txid == bytes(32) \
            and self.inputs[0].vout == 0xffffffff


def txid_of(raw):
    """Internal-order txid of a raw tx (parsed strictly)."""
    return Tx.parse(raw).txid()


# ------------------------------------------------------------ scripts

def is_exit_script(spk):
    """Segwit v16 with a 20-byte program, `6014<20 bytes>` (SPEC 4.4)."""
    return len(spk) == 22 and spk[0] == 0x60 and spk[1] == 0x14


def is_op_return(spk):
    return len(spk) != 0 and spk[0] == 0x6a


def exit_script(eth_address):
    """The scriptPubKey that `withdraw`s to this ETH address (20 bytes or hex)."""
    a = eth_address if isinstance(eth_address, (bytes, bytearray)) else bytes.fromhex(
        eth_address[2:] if eth_address[:2] in ('0x', '0X') else eth_address)
    if len(a) != 20:
        raise ValueError('an ETH address is 20 bytes')
    return b'\x60\x14' + bytes(a)


# ------------------------------------------------------------ SPEC 3.2: blocks

def scriptnum(n):
    """CScriptNum serialization of a non-negative n: little-endian, no
    leading zero bytes, plus 00 if the top bit is set; empty for 0."""
    if n < 0:
        raise ValueError('negative')
    out = bytearray()
    while n:
        out.append(n & 0xff)
        n >>= 8
    if out and out[-1] & 0x80:
        out.append(0)
    return bytes(out)


def coinbase_script_sig(h):
    """`len(n) || n || 07 "evmchan"`, with n = CScriptNum(h), or 00 for h = 0.
    Always a direct data push of 1-5 bytes, never OP_0 or OP_1..OP_16."""
    if not 0 <= h <= 0xffffffff:
        raise ValueError('height out of range')
    n = scriptnum(h) or b'\x00'
    return bytes([len(n)]) + n + bytes([len(COINBASE_TAG)]) + COINBASE_TAG


def coinbase(h):
    """The coinbase of virtual block h (SPEC 3.2), as a Tx."""
    return Tx(2, [TxIn(bytes(32), 0xffffffff, coinbase_script_sig(h), SEQUENCE_FINAL)],
              [(0, b'\x6a')], 0)


def merkle_root(txids):
    """Bitcoin's merkle root of internal-order txids (the last one of an odd
    level duplicated). Internal order in, internal order out."""
    if not txids:
        raise ValueError('no txids')
    layer = [bytes(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def header(prev_hash, merkle, time, version=HEADER_VERSION, bits=HEADER_BITS, nonce=0):
    """An 80-byte header; prev_hash and merkle in internal order."""
    if len(prev_hash) != 32 or len(merkle) != 32:
        raise ValueError('need 32-byte hashes')
    return struct.pack('<I', version) + bytes(prev_hash) + bytes(merkle) + struct.pack('<III', time, bits, nonce)


def block_hash(hdr):
    """Internal-order hash of an 80-byte header."""
    if len(hdr) != 80:
        raise ValueError('a header is 80 bytes')
    return dsha(hdr)


def block_time(h, genesis_time, interval):
    """T(h) = GENESIS_TIME + h * INTERVAL, a u32 (SPEC 3.1)."""
    t = genesis_time + h * interval
    if not 0 <= t <= 0xffffffff:
        raise ValueError('block time does not fit in a u32')
    return t


def block_header(h, prev_hash, txids, genesis_time, interval):
    """Header of virtual block h, where txids are its non-coinbase txids
    (internal order) in EVM order. Returns (header, merkle root)."""
    root = merkle_root([coinbase(h).txid()] + list(txids))
    return header(prev_hash, root, block_time(h, genesis_time, interval)), root


def build_block(h, prev_hash, raw_txs, genesis_time, interval, txids=None):
    """Virtual block h (SPEC 3.2): (block bytes, internal-order hash).
    raw_txs are its TxApplied raw txs in EVM order, serialized as carried (with
    witness); txids, if given, are their internal-order txids (else computed)."""
    raw_txs = [bytes(r) for r in raw_txs]
    if txids is None:
        txids = [txid_of(r) for r in raw_txs]
    hdr, _ = block_header(h, prev_hash, txids, genesis_time, interval)
    body = [hdr, varint(1 + len(raw_txs)), coinbase(h).serialize(witness=False)] + raw_txs
    return b''.join(body), block_hash(hdr)


def parse_block(raw):
    """(80-byte header, [Tx]) of a serialized block; strict, no trailing bytes."""
    r = Reader(raw)
    hdr = r.take(80)
    txs = [Tx.read(r) for _ in range(r.varint())]
    if r.i != len(r.b):
        raise ParseError('trailing bytes after the block')
    return hdr, txs


# ------------------------------------------------------------ SPEC 3.3: deposits

def deposit_prevout(chain_id, contract, nonce):
    """keccak256(abi.encodePacked("evmchan deposit", uint256 chainid, address
    contract, uint256 depositNonce)), 32 bytes as stored (internal order)."""
    from evm import keccak256, address_bytes
    return keccak256(b'evmchan deposit' + chain_id.to_bytes(32, 'big') + address_bytes(contract)
                     + nonce.to_bytes(32, 'big'))


def deposit_tx(prevout_txid, amount, script_pubkey):
    """The synthetic deposit tx of SPEC 3.3 spending the fake prevout
    (prevout_txid, 0); its output 0 is (amount sats, script_pubkey)."""
    return Tx(2, [TxIn(prevout_txid, 0, b'', SEQUENCE_FINAL)], [(amount, bytes(script_pubkey))], 0)


def deposit_tx_for(chain_id, contract, nonce, amount, script_pubkey):
    return deposit_tx(deposit_prevout(chain_id, contract, nonce), amount, script_pubkey)


# ------------------------------------------------------------ bech32 / bech32m (BIP173, BIP350)

CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
BECH32_CONST, BECH32M_CONST = 1, 0x2bc830a3


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
    maxv = (1 << tobits) - 1
    for v in data:
        if v < 0 or v >> frombits:
            raise ValueError('bad value')
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError('bad padding')
    return ret


def segwit_address(hrp, version, program):
    """bech32 (v0) or bech32m (v1-16) address of a witness program."""
    program = bytes(program)
    if not 0 <= version <= 16 or not 2 <= len(program) <= 40 or \
            (version == 0 and len(program) not in (20, 32)):
        raise ValueError('bad witness program')
    data = [version] + _convertbits(program, 8, 5)
    const = BECH32_CONST if version == 0 else BECH32M_CONST
    pm = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ const
    return hrp + '1' + ''.join(CHARSET[d] for d in data + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def decode_segwit_address(addr, hrp=None):
    """(hrp, version, program) of a segwit address; checks case, length,
    checksum type for the version and program length (BIP173/BIP350)."""
    if addr.lower() != addr and addr.upper() != addr:
        raise ValueError('mixed case')
    addr = addr.lower()
    if len(addr) > 90 or any(ord(c) < 33 or ord(c) > 126 for c in addr):
        raise ValueError('bad address characters or length')
    pos = addr.rfind('1')
    if pos < 1 or pos + 7 > len(addr):
        raise ValueError('bad separator')
    got_hrp, data = addr[:pos], addr[pos + 1:]
    if hrp is not None and got_hrp != hrp:
        raise ValueError('wrong hrp {}'.format(got_hrp))
    if any(c not in CHARSET for c in data):
        raise ValueError('bad character')
    values = [CHARSET.index(c) for c in data]
    const = _polymod(_hrp_expand(got_hrp) + values)
    if not values[:-6]:
        raise ValueError('no witness version')
    version = values[0]
    if version > 16:
        raise ValueError('bad witness version')
    if const != (BECH32_CONST if version == 0 else BECH32M_CONST):
        raise ValueError('bad checksum')
    program = bytes(_convertbits(values[1:-6], 5, 8, False))
    if not 2 <= len(program) <= 40 or (version == 0 and len(program) not in (20, 32)):
        raise ValueError('bad program length')
    return got_hrp, version, program


def witness_script(version, program):
    return bytes([version + 0x50 if version else 0, len(program)]) + bytes(program)


def address_script(addr, hrp=None):
    """scriptPubKey of a segwit address."""
    _, version, program = decode_segwit_address(addr, hrp)
    return witness_script(version, program)


def script_address(spk, hrp='bcrt'):
    """Segwit address of a scriptPubKey, or None if it isn't a witness program."""
    if len(spk) < 4 or len(spk) != spk[1] + 2:
        return None
    if spk[0] == 0:
        version = 0
    elif 0x51 <= spk[0] <= 0x60:
        version = spk[0] - 0x50
    else:
        return None
    try:
        return segwit_address(hrp, version, spk[2:])
    except ValueError:
        return None


def exit_address(eth_address, hrp='bcrt'):
    """The `bcrt1s...` address (segwit v16, the 20 address bytes) that the
    contract pays out to eth_address (SPEC 5.5)."""
    return segwit_address(hrp, 16, exit_script(eth_address)[2:])
