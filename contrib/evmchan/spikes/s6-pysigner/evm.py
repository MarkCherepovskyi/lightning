# SPDX-License-Identifier: MIT
"""A minimal EVM client: keccak-256, RLP, EIP-1559 signing (coincurve), a
JSON-RPC client over urllib and just enough ABI. Standard library + coincurve."""
import json
import time
import urllib.error
import urllib.request

import coincurve

# --- keccak-256 (Ethereum's: original Keccak padding 0x01..0x80, not SHA3's 0x06) ---

_M64 = (1 << 64) - 1
# Rotation offsets, lane index x + 5*y.
_ROT = [0, 1, 62, 28, 27, 36, 44, 6, 55, 20, 3, 10, 43, 25, 39,
        41, 45, 15, 21, 8, 18, 2, 61, 56, 14]


def _round_constants():
    out, r = [], 1
    for _ in range(24):
        c = 0
        for j in range(7):
            if r & 1:
                c |= 1 << ((1 << j) - 1)
            r = ((r << 1) ^ 0x71) & 0xff if r & 0x80 else r << 1
        out.append(c)
    return out


_RC = _round_constants()
# For rho+pi: destination of lane x + 5*y is y + 5*((2x + 3y) % 5).
_PI = [y + 5 * ((2 * x + 3 * y) % 5) for y in range(5) for x in range(5)]


def _keccak_f(a):
    for rc in _RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M64)
             for x in range(5)]
        b = [0] * 25
        for i in range(25):
            v, n = a[i] ^ d[i % 5], _ROT[i]
            b[_PI[i]] = ((v << n) | (v >> (64 - n))) & _M64
        a = [b[i] ^ (~b[i - i % 5 + (i + 1) % 5] & b[i - i % 5 + (i + 2) % 5]) for i in range(25)]
        a[0] ^= rc
    return a


def keccak256(data):
    rate = 136
    data = bytes(data)
    pad = rate - len(data) % rate
    data += b'\x81' if pad == 1 else b'\x01' + bytes(pad - 2) + b'\x80'
    a = [0] * 25
    for off in range(0, len(data), rate):
        for i in range(rate // 8):
            a[i] ^= int.from_bytes(data[off + 8 * i:off + 8 * i + 8], 'little')
        a = _keccak_f(a)
    return b''.join(a[i].to_bytes(8, 'little') for i in range(4))


# --- hex and addresses ---

def unhex(s):
    s = s[2:] if s[:2] in ('0x', '0X') else s
    return bytes.fromhex(s)


def qty(n):
    """An integer as a JSON-RPC quantity."""
    return hex(int(n))


def checksum_address(addr):
    """EIP-55 form of a 20-byte address (bytes or hex)."""
    h = (addr if isinstance(addr, bytes) else unhex(addr)).hex()
    if len(h) != 40:
        raise ValueError('bad address')
    k = keccak256(h.encode()).hex()
    return '0x' + ''.join(ch.upper() if int(k[i], 16) >= 8 else ch for i, ch in enumerate(h))


def address_bytes(addr):
    b = addr if isinstance(addr, bytes) else unhex(addr)
    if len(b) != 20:
        raise ValueError('bad address')
    return b


def create_address(sender, nonce):
    """The address of a contract created by sender's transaction with this nonce."""
    return checksum_address(keccak256(rlp([address_bytes(sender), nonce]))[12:])


# --- RLP ---

def _rlp_len(n, base):
    if n < 56:
        return bytes([base + n])
    b = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return bytes([base + 55 + len(b)]) + b


def rlp(item):
    """RLP of bytes, a non-negative int (minimal big-endian) or a list of those."""
    if isinstance(item, (list, tuple)):
        body = b''.join(rlp(x) for x in item)
        return _rlp_len(len(body), 0xc0) + body
    if isinstance(item, int):
        if item < 0:
            raise ValueError('negative int')
        item = item.to_bytes((item.bit_length() + 7) // 8, 'big')
    item = bytes(item)
    if len(item) == 1 and item[0] < 0x80:
        return item
    return _rlp_len(len(item), 0x80) + item


# --- EIP-1559 transactions ---

def _tx_fields(tx):
    to = tx.get('to')
    access = [[address_bytes(a), [unhex(k) if isinstance(k, str) else k for k in keys]]
              for a, keys in tx.get('accessList', [])]
    data = tx.get('data', b'')
    return [tx['chainId'], tx['nonce'], tx['maxPriorityFeePerGas'], tx['maxFeePerGas'],
            tx['gas'], address_bytes(to) if to else b'', tx.get('value', 0),
            unhex(data) if isinstance(data, str) else bytes(data), access]


class Account:
    """A secp256k1 key and the EVM account it controls."""

    def __init__(self, key):
        self.key = coincurve.PrivateKey(unhex(key) if isinstance(key, str) else key)
        self.address = checksum_address(keccak256(self.key.public_key.format(False)[1:])[12:])

    def sign_tx(self, tx):
        """(raw type-2 transaction bytes, its hash as 0x hex) for a dict with chainId,
        nonce, maxPriorityFeePerGas, maxFeePerGas, gas, to (None creates), value, data."""
        fields = _tx_fields(tx)
        sig = self.key.sign_recoverable(keccak256(b'\x02' + rlp(fields)), hasher=None)
        y_parity = sig[64]  # libsecp256k1 gives low s, so the recovery id is the parity
        if y_parity > 1:
            raise ValueError('recovery id {} (r overflowed n)'.format(y_parity))
        raw = b'\x02' + rlp(fields + [y_parity, int.from_bytes(sig[:32], 'big'),
                                      int.from_bytes(sig[32:64], 'big')])
        return raw, '0x' + keccak256(raw).hex()


# --- ABI ---

def selector(signature):
    return keccak256(signature.encode())[:4]


def _dynamic(t):
    return t in ('bytes', 'string') or t.endswith('[]')


def _word(n):
    return int(n).to_bytes(32, 'big')


def _enc(t, v):
    if t.endswith('[]'):
        return _word(len(v)) + abi_encode([t[:-2]] * len(v), v)
    if t in ('bytes', 'string'):
        b = v.encode() if t == 'string' else bytes(v)
        return _word(len(b)) + b + bytes(-len(b) % 32)
    if t == 'address':
        return bytes(12) + address_bytes(v)
    if t == 'bool':
        return _word(1 if v else 0)
    if t.startswith('uint'):
        if not 0 <= v < 1 << int(t[4:] or 256):
            raise ValueError('{} out of range for {}'.format(v, t))
        return _word(v)
    if t.startswith('bytes'):
        b = unhex(v) if isinstance(v, str) else bytes(v)
        if len(b) != int(t[5:]):
            raise ValueError('{} needs {} bytes'.format(t, t[5:]))
        return b + bytes(32 - len(b))
    raise ValueError('unsupported ABI type ' + t)


def abi_encode(types, values):
    if len(types) != len(values):
        raise ValueError('{} types, {} values'.format(len(types), len(values)))
    heads, tails, tail_len = [], [], 32 * len(types)
    for t, v in zip(types, values):
        e = _enc(t, v)
        if _dynamic(t):
            heads.append(_word(tail_len))
            tails.append(e)
            tail_len += len(e)
        else:
            heads.append(e)
    return b''.join(heads + tails)


def calldata(signature, types, values):
    """calldata('submit(bytes,bytes,bytes)', ['bytes'] * 3, [a, b, c])."""
    return selector(signature) + abi_encode(types, values)


def _int_at(d, off):
    if off < 0 or off + 32 > len(d):
        raise ValueError('ABI data truncated')
    return int.from_bytes(d[off:off + 32], 'big')


def _dec(t, d):
    if t.endswith('[]'):
        n = _int_at(d, 0)
        if n > len(d) // 32:
            raise ValueError('ABI array length too large')
        return abi_decode([t[:-2]] * n, d[32:])
    if t in ('bytes', 'string'):
        n = _int_at(d, 0)
        if 32 + n > len(d):
            raise ValueError('ABI bytes truncated')
        return d[32:32 + n].decode() if t == 'string' else d[32:32 + n]
    w = d[:32]
    if t == 'address':
        return checksum_address(w[12:])
    if t == 'bool':
        return w[31] == 1
    if t.startswith('uint'):
        return int.from_bytes(w, 'big')
    if t.startswith('bytes'):
        return w[:int(t[5:])]
    raise ValueError('unsupported ABI type ' + t)


def abi_decode(types, data):
    data, out = bytes(data), []
    for i, t in enumerate(types):
        if _dynamic(t):
            out.append(_dec(t, data[_int_at(data, 32 * i):]))
        else:
            _int_at(data, 32 * i)
            out.append(_dec(t, data[32 * i:32 * i + 32]))
    return out


def decode_log(log, event_signature, indexed_types, data_types):
    """(indexed values, data values) of an eth_getLogs entry, after checking topic0
    is keccak(event_signature). Indexed dynamic types come back as their 32-byte hash."""
    topics = [unhex(t) for t in log['topics']]
    if not topics or topics[0] != keccak256(event_signature.encode()):
        raise ValueError('not a {} log'.format(event_signature))
    if len(topics) != 1 + len(indexed_types):
        raise ValueError('expected {} indexed topics'.format(len(indexed_types)))
    indexed = [t if _dynamic(ty) else _dec(ty, t) for ty, t in zip(indexed_types, topics[1:])]
    return indexed, abi_decode(data_types, unhex(log['data']))


# --- JSON-RPC ---

class EvmError(Exception):
    def __init__(self, message, code=None, data=None):
        super().__init__(message)
        self.code, self.data = code, data


class EvmRpc:
    """An EVM node's JSON-RPC at url (http or https, an API key in the path is fine)."""

    def __init__(self, url, timeout=60):
        self.url, self.timeout, self.next_id, self._chain_id = url, timeout, 0, None

    def call(self, method, *params):
        self.next_id += 1
        req = json.dumps({'jsonrpc': '2.0', 'id': self.next_id, 'method': method,
                          'params': list(params)}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    self.url, req, {'Content-Type': 'application/json'}), timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
        except OSError as e:
            raise EvmError('{}: {}'.format(method, e))
        try:
            resp = json.loads(body.decode())
        except ValueError:
            raise EvmError('{}: unexpected reply {!r}'.format(method, body[:200]))
        err = resp.get('error')
        if err:
            if isinstance(err, dict):
                raise EvmError('{}: {}'.format(method, err.get('message')),
                               err.get('code'), err.get('data'))
            raise EvmError('{}: {}'.format(method, err))
        return resp.get('result')

    def chain_id(self):
        if self._chain_id is None:
            self._chain_id = int(self.call('eth_chainId'), 16)
        return self._chain_id

    def nonce(self, address, block='pending'):
        return int(self.call('eth_getTransactionCount', address, block), 16)

    def block(self, tag='latest', full=False):
        """A block by number or by tag: latest, safe, finalized, earliest, pending."""
        return self.call('eth_getBlockByNumber', qty(tag) if isinstance(tag, int) else tag, full)

    def fees(self):
        """(maxPriorityFeePerGas, maxFeePerGas) for a transaction sent now."""
        base = self.block('latest').get('baseFeePerGas')
        if base is None:  # no EIP-1559 on this chain: both fields act as gasPrice
            gp = int(self.call('eth_gasPrice'), 16)
            return gp, gp
        base = int(base, 16)
        try:
            tip = int(self.call('eth_maxPriorityFeePerGas'), 16)
        except EvmError:
            try:
                rewards = sorted(int(r[0], 16) for r in
                                 self.call('eth_feeHistory', qty(5), 'latest', [50])['reward'] if r)
                tip = rewards[len(rewards) // 2] if rewards else 0
            except (EvmError, KeyError, TypeError):
                tip = max(int(self.call('eth_gasPrice'), 16) - base, 0)
        return tip, 2 * base + tip

    @staticmethod
    def _callobj(to, data, value, sender):
        obj = {'data': '0x' + bytes(data).hex(), 'value': qty(value)}
        if to:
            obj['to'] = to
        if sender:
            obj['from'] = sender
        return obj

    def estimate_gas(self, sender, to, data=b'', value=0):
        return int(self.call('eth_estimateGas', self._callobj(to, data, value, sender)), 16)

    def eth_call(self, to, data, block='latest', value=0, sender=None):
        return unhex(self.call('eth_call', self._callobj(to, data, value, sender), block))

    def get_logs(self, address, topics=None, from_block=0, to_block='latest'):
        f = {'address': address, 'fromBlock': qty(from_block) if isinstance(from_block, int)
             else from_block, 'toBlock': qty(to_block) if isinstance(to_block, int) else to_block}
        if topics is not None:
            f['topics'] = topics
        return self.call('eth_getLogs', f)

    def send_raw(self, raw):
        return self.call('eth_sendRawTransaction', '0x' + raw.hex())

    def wait_receipt(self, txhash, timeout=120, poll=0.2):
        end = time.monotonic() + timeout
        while True:
            r = self.call('eth_getTransactionReceipt', txhash)
            if r is not None:
                return r
            if time.monotonic() > end:
                raise EvmError('no receipt for {} after {}s'.format(txhash, timeout))
            time.sleep(poll)

    def send(self, account, to, value=0, data=b'', gas=None, timeout=120):
        """Sign and send a type-2 transaction from account, wait for its receipt and
        return it; raises EvmError if it reverted. to=None creates a contract."""
        tip, max_fee = self.fees()
        if gas is None:
            gas = self.estimate_gas(account.address, to, data, value)
        raw, txhash = account.sign_tx({
            'chainId': self.chain_id(), 'nonce': self.nonce(account.address),
            'maxPriorityFeePerGas': tip, 'maxFeePerGas': max_fee, 'gas': gas,
            'to': to, 'value': value, 'data': data})
        got = self.send_raw(raw)
        if got.lower() != txhash:
            raise EvmError('node returned hash {}, expected {}'.format(got, txhash))
        receipt = self.wait_receipt(txhash, timeout)
        if int(receipt['status'], 16) != 1:
            raise EvmError('transaction {} reverted'.format(txhash), data=receipt)
        return receipt
