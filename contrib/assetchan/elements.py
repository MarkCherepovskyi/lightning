"""elementsd's JSON-RPC and its amounts, shared by the plugin and the tests:
the standard library only, positional params."""
from decimal import Decimal
from pyln.client import RpcException

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

COIN = 10**8


class ElementsError(RpcException):
    """An elementsd RPC call failed."""
    def __init__(self, message):
        super().__init__(message, code=-1)


class ElementsRpc:
    """elementsd at http://user:password@host:port, optionally a wallet of it."""

    def __init__(self, url, wallet=''):
        u = urllib.parse.urlsplit(url)
        self.url = '{}://{}{}'.format(u.scheme, u.netloc.rpartition('@')[2], u.path).rstrip('/')
        if wallet:
            self.url += '/wallet/' + urllib.parse.quote(wallet, safe='')
        auth = '{}:{}'.format(urllib.parse.unquote(u.username or ''),
                              urllib.parse.unquote(u.password or ''))
        self.headers = {'Content-Type': 'application/json',
                        'Authorization': 'Basic ' + base64.b64encode(auth.encode()).decode()}
        self.next_id = 0

    def call(self, method, *params):
        self.next_id += 1
        req = {'jsonrpc': '1.0', 'id': self.next_id, 'method': method,
               'params': list(params)}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, json.dumps(req).encode(),
                                                               self.headers), timeout=120) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            # An RPC error is an HTTP 500 with the usual JSON body.
            body = e.read()
        except OSError as e:
            raise ElementsError('elementsd {}: {}'.format(method, e))
        try:
            resp = json.loads(body.decode(), parse_float=Decimal)
        except ValueError:
            raise ElementsError('elementsd {}: unexpected reply {!r}'.format(method, body[:200]))
        err = resp.get('error')
        if err:
            if isinstance(err, dict):
                err = err.get('message', err)
            raise ElementsError('elementsd {}: {}'.format(method, err))
        return resp.get('result')


def rpc_url(user, password, host, port):
    """The http://user:password@host:port ElementsRpc takes."""
    return 'http://{}:{}@{}:{}'.format(urllib.parse.quote(user, safe=''),
                                       urllib.parse.quote(password, safe=''), host, port)


def coins(units):
    """Base units -> the decimal amount string elementsd takes."""
    return '{}.{:08d}'.format(*divmod(int(units), COIN))


def base_units(amount):
    """elementsd's decimal amount (a number or a string) -> base units."""
    return int(Decimal(str(amount)) * COIN)


def config_value(cfg, name, default=None):
    """A lightningd option's value from listconfigs' configs."""
    opt = cfg.get(name, {})
    for k in ('value_int', 'value_str', 'value_bool', 'values_str'):
        if k in opt:
            return opt[k]
    return default


# ------------------------------------------------- transaction (de)serialization
# Enough of Elements' transaction format to splice transactions together:
# the plugin adds L-BTC inputs and outputs to core's asset sweeps, and has
# elementsd sign only those, so it puts core's witness for input 0 back.
# https://github.com/ElementsProject/elements/blob/master/doc/elements-tx-format.md

class _Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        if self.pos + n > len(self.data):
            raise ValueError('truncated transaction')
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def u32(self):
        return int.from_bytes(self.take(4), 'little')

    def varint(self):
        n = self.take(1)[0]
        if n < 0xfd:
            return n
        return int.from_bytes(self.take({0xfd: 2, 0xfe: 4, 0xff: 8}[n]), 'little')

    def varbytes(self):
        return self.take(self.varint())

    def stack(self):
        return [self.varbytes() for _ in range(self.varint())]

    def commitment(self, explicit_len):
        """A confidential asset, value or nonce: null, explicit or committed."""
        prefix = self.data[self.pos]
        if prefix == 0:
            return self.take(1)
        return self.take(explicit_len if prefix == 1 else 33)


def _varint(n):
    if n < 0xfd:
        return bytes([n])
    for prefix, size in ((0xfd, 2), (0xfe, 4), (0xff, 8)):
        if n < 1 << (8 * size):
            return bytes([prefix]) + n.to_bytes(size, 'little')


def _varbytes(b):
    return _varint(len(b)) + b


def _stack(items):
    return _varint(len(items)) + b''.join(_varbytes(i) for i in items)


OUTPOINT_ISSUANCE_FLAG = 1 << 31
OUTPOINT_PEGIN_FLAG = 1 << 30


class TxIn:
    def __init__(self, txid, vout, sequence, script_sig=b'', issuance=b'',
                 witness=None, proofs=(b'', b''), pegin_witness=None):
        self.txid, self.vout, self.sequence = txid, vout, sequence
        self.script_sig, self.issuance = script_sig, issuance
        self.witness = witness or []
        self.proofs = proofs                 # issuance amount, inflation keys
        self.pegin_witness = pegin_witness or []
        self.pegin = False


class TxOut:
    def __init__(self, asset, value, script, nonce=b'\x00', proofs=(b'', b'')):
        self.asset, self.value, self.nonce, self.script = asset, value, nonce, script
        self.proofs = proofs                 # surjection, range

    @classmethod
    def explicit(cls, asset_id, units, script):
        """An unblinded output of `units` of `asset_id` (as elements-cli prints it)."""
        return cls(b'\x01' + bytes.fromhex(asset_id)[::-1],
                   b'\x01' + int(units).to_bytes(8, 'big'), script)


class Tx:
    def __init__(self, version, inputs, outputs, locktime):
        self.version, self.inputs, self.outputs, self.locktime = version, inputs, outputs, locktime

    @classmethod
    def parse(cls, hexstr):
        r = _Reader(bytes.fromhex(hexstr))
        version = r.u32()
        flag = r.take(1)[0]
        inputs = []
        for _ in range(r.varint()):
            txid = r.take(32)[::-1].hex()
            vout = r.u32()
            issuance = pegin = False
            if vout != 0xffffffff:
                issuance = bool(vout & OUTPOINT_ISSUANCE_FLAG)
                pegin = bool(vout & OUTPOINT_PEGIN_FLAG)
                vout &= ~(OUTPOINT_ISSUANCE_FLAG | OUTPOINT_PEGIN_FLAG)
            script_sig = r.varbytes()
            sequence = r.u32()
            raw_issuance = b''
            if issuance:
                start = r.pos
                r.take(64)                   # blinding nonce, entropy
                r.commitment(9)              # amount
                r.commitment(9)              # inflation keys
                raw_issuance = r.data[start:r.pos]
            txin = TxIn(txid, vout, sequence, script_sig, raw_issuance)
            txin.pegin = pegin
            inputs.append(txin)
        outputs = []
        for _ in range(r.varint()):
            asset = r.commitment(33)
            value = r.commitment(9)
            nonce = r.commitment(33)
            outputs.append(TxOut(asset, value, r.varbytes(), nonce))
        locktime = r.u32()
        if flag & 1:
            for txin in inputs:
                txin.proofs = (r.varbytes(), r.varbytes())
                txin.witness = r.stack()
                txin.pegin_witness = r.stack()
            for txout in outputs:
                txout.proofs = (r.varbytes(), r.varbytes())
        if r.pos != len(r.data):
            raise ValueError('trailing bytes after transaction')
        return cls(version, inputs, outputs, locktime)

    def _has_witness(self):
        return (any(i.witness or i.pegin_witness or any(i.proofs) for i in self.inputs)
                or any(any(o.proofs) for o in self.outputs))

    def serialize(self, with_witness=True):
        witness = with_witness and self._has_witness()
        out = self.version.to_bytes(4, 'little') + bytes([1 if witness else 0])
        out += _varint(len(self.inputs))
        for i in self.inputs:
            vout = i.vout
            if i.issuance:
                vout |= OUTPOINT_ISSUANCE_FLAG
            if i.pegin:
                vout |= OUTPOINT_PEGIN_FLAG
            out += (bytes.fromhex(i.txid)[::-1] + vout.to_bytes(4, 'little')
                    + _varbytes(i.script_sig) + i.sequence.to_bytes(4, 'little') + i.issuance)
        out += _varint(len(self.outputs))
        for o in self.outputs:
            out += o.asset + o.value + o.nonce + _varbytes(o.script)
        out += self.locktime.to_bytes(4, 'little')
        if witness:
            for i in self.inputs:
                out += (_varbytes(i.proofs[0]) + _varbytes(i.proofs[1])
                        + _stack(i.witness) + _stack(i.pegin_witness))
            for o in self.outputs:
                out += _varbytes(o.proofs[0]) + _varbytes(o.proofs[1])
        return out

    def hex(self):
        return self.serialize().hex()

    def vsize(self):
        """Virtual size, as relay fees count it (witness data at a quarter)."""
        base, total = len(self.serialize(False)), len(self.serialize())
        return (3 * base + total + 3) // 4
