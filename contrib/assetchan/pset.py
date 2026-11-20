"""Just enough of Elements' PSET (BIP 370 PSBTv2 with Elements fields) for
dual-funded asset channels: a PSET is a list of key-value maps (global, one
per input, one per output), and the plugin only ever adds or copies whole
entries between two PSETs of the same transaction.

- core sends each input's previous transaction over the wire
  (`tx_add_input`), so it wants the non-witness UTXO, which elementsd's
  `walletprocesspsbt` may leave out: add_prev_txs() puts it in;
- elementsd signs our inputs of the funding transaction core built, but may
  drop or rewrite core's own fields doing so (its serial_ids): copy_finals()
  takes just the finished scriptSigs and witnesses back into core's PSET.
"""
import base64

MAGICS = (b'pset\xff', b'psbt\xff')

# BIP 174/370 keys (single byte, no key data)
IN_NON_WITNESS_UTXO = b'\x00'
IN_FINAL_SCRIPTSIG = b'\x07'
IN_FINAL_SCRIPTWITNESS = b'\x08'
IN_PREVIOUS_TXID = b'\x0e'
IN_OUTPUT_INDEX = b'\x0f'
GLOBAL_INPUT_COUNT = b'\x04'
GLOBAL_OUTPUT_COUNT = b'\x05'


class PsetError(Exception):
    pass


def _varint(b, i):
    if i >= len(b):
        raise PsetError('truncated')
    v = b[i]
    if v < 0xfd:
        return v, i + 1
    size = {0xfd: 2, 0xfe: 4, 0xff: 8}[v]
    if i + 1 + size > len(b):
        raise PsetError('truncated')
    return int.from_bytes(b[i + 1:i + 1 + size], 'little'), i + 1 + size


def _ser_varint(n):
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b'\xfd' + n.to_bytes(2, 'little')
    if n <= 0xffffffff:
        return b'\xfe' + n.to_bytes(4, 'little')
    return b'\xff' + n.to_bytes(8, 'little')


class Pset:
    """magic, then maps: [global, inputs..., outputs...], each a list of
    (key, value) byte pairs in their original order."""

    def __init__(self, b64):
        raw = base64.b64decode(b64)
        self.magic = raw[:5]
        if self.magic not in MAGICS:
            raise PsetError('not a PSET/PSBT')
        i = 5
        maps, cur = [], []
        while i < len(raw):
            klen, i = _varint(raw, i)
            if klen == 0:
                maps.append(cur)
                cur = []
                continue
            key, i = raw[i:i + klen], i + klen
            vlen, i = _varint(raw, i)
            val, i = raw[i:i + vlen], i + vlen
            cur.append((key, val))
        if cur:
            raise PsetError('unterminated map')
        if not maps:
            raise PsetError('no global map')
        self.glob = maps[0]
        n_in = _varint(self._get(self.glob, GLOBAL_INPUT_COUNT), 0)[0]
        n_out = _varint(self._get(self.glob, GLOBAL_OUTPUT_COUNT), 0)[0]
        if len(maps) != 1 + n_in + n_out:
            raise PsetError('expected {} maps, got {}'.format(1 + n_in + n_out, len(maps)))
        self.inputs = maps[1:1 + n_in]
        self.outputs = maps[1 + n_in:]

    @staticmethod
    def _get(m, key):
        for k, v in m:
            if k == key:
                return v
        raise PsetError('missing key {}'.format(key.hex()))

    @staticmethod
    def _set(m, key, val):
        for n, (k, _) in enumerate(m):
            if k == key:
                m[n] = (key, val)
                return
        m.append((key, val))

    def b64(self):
        out = bytearray(self.magic)
        for m in [self.glob] + self.inputs + self.outputs:
            for k, v in m:
                out += _ser_varint(len(k)) + k + _ser_varint(len(v)) + v
            out += b'\x00'
        return base64.b64encode(bytes(out)).decode()

    def prevout(self, n):
        """Input n's (txid as elements-cli prints it, vout)."""
        m = self.inputs[n]
        txid = self._get(m, IN_PREVIOUS_TXID)[::-1].hex()
        return txid, int.from_bytes(self._get(m, IN_OUTPUT_INDEX), 'little')


def add_prev_txs(b64, raw_tx):
    """b64 with every input's non-witness UTXO filled in, raw_tx(txid)
    giving the hex of a previous transaction."""
    p = Pset(b64)
    for n, m in enumerate(p.inputs):
        if any(k == IN_NON_WITNESS_UTXO for k, _ in m):
            continue
        txid, _ = p.prevout(n)
        m.append((IN_NON_WITNESS_UTXO, bytes.fromhex(raw_tx(txid))))
    return p.b64()


def copy_finals(dest_b64, signed_b64, outpoints):
    """dest_b64 with the final scriptSig and witness of each input spending
    one of outpoints ({(txid, vout)}) copied from signed_b64, a PSET of the
    same transaction.  Raises PsetError if one isn't finished there."""
    dest, signed = Pset(dest_b64), Pset(signed_b64)
    finals = {}
    for n, m in enumerate(signed.inputs):
        fin = [(k, v) for k, v in m if k in (IN_FINAL_SCRIPTSIG, IN_FINAL_SCRIPTWITNESS)]
        if fin:
            finals[signed.prevout(n)] = fin
    for n, m in enumerate(dest.inputs):
        op = dest.prevout(n)
        if op not in outpoints:
            continue
        if op not in finals:
            raise PsetError('input {}:{} not signed'.format(*op))
        for k, v in finals[op]:
            Pset._set(m, k, v)
    return dest.b64()
