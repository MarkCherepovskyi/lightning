# SPDX-License-Identifier: MIT
"""A minimal EVM client: keccak-256, RLP, EIP-1559 signing (coincurve), a
JSON-RPC client over urllib, just enough ABI (static types, bytes, string,
dynamic arrays and tuples) and eth_getLogs with adaptive range splitting.
Standard library + coincurve."""
import collections
import itertools
import json
import re
import threading
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


def _split_tuple(t):
    """'(a,b[],(c,d))' -> ['a', 'b[]', '(c,d)']."""
    if not (t.startswith('(') and t.endswith(')')):
        raise ValueError('not a tuple type: ' + t)
    inner, parts, depth, cur = t[1:-1], [], 0, ''
    for ch in inner:
        if ch == ',' and depth == 0:
            parts.append(cur)
            cur = ''
            continue
        depth += {'(': 1, ')': -1}.get(ch, 0)
        if depth < 0:
            raise ValueError('bad tuple type: ' + t)
        cur += ch
    if depth:
        raise ValueError('bad tuple type: ' + t)
    if cur or parts:
        parts.append(cur)
    if any(not p for p in parts):
        raise ValueError('bad tuple type: ' + t)
    return parts


def _dynamic(t):
    if t.endswith('[]') or t in ('bytes', 'string'):
        return True
    if t.startswith('('):
        return any(_dynamic(c) for c in _split_tuple(t))
    return False


def _word(n):
    return int(n).to_bytes(32, 'big')


def _enc(t, v):
    if t.endswith('[]'):
        return _word(len(v)) + abi_encode([t[:-2]] * len(v), v)
    if t.startswith('('):
        return abi_encode(_split_tuple(t), list(v))
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


def _static_size(t):
    if t.startswith('(') and not t.endswith('[]'):
        return sum(_static_size(c) for c in _split_tuple(t))
    return 32


def abi_encode(types, values):
    """ABI encoding of values; a tuple type is written '(uint64,bytes,uint32)'
    and takes a tuple or list of its components."""
    if len(types) != len(values):
        raise ValueError('{} types, {} values'.format(len(types), len(values)))
    heads, tails = [], []
    tail_len = sum(32 if _dynamic(t) else _static_size(t) for t in types)
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
    """calldata('submit(bytes,(uint64,bytes,uint32)[])', ['bytes', '(uint64,bytes,uint32)[]'],
    [raw, [(amount, script, height), ...]])."""
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
    if t.startswith('('):
        return tuple(abi_decode(_split_tuple(t), d))
    if t in ('bytes', 'string'):
        n = _int_at(d, 0)
        if 32 + n > len(d):
            raise ValueError('ABI bytes truncated')
        return d[32:32 + n].decode() if t == 'string' else d[32:32 + n]
    w = d[:32]
    if len(w) != 32:
        raise ValueError('ABI data truncated')
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
    data, out, off = bytes(data), [], 0
    for t in types:
        if _dynamic(t):
            out.append(_dec(t, data[_int_at(data, off):]))
            off += 32
        else:
            size = _static_size(t)
            if off + size > len(data):
                raise ValueError('ABI data truncated')
            out.append(_dec(t, data[off:off + size]))
            off += size
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

# The largest JSON-RPC reply read: a provider can't make the plugin hold more.
# 10,000 logs (eth_getLogs' result cap) of a 1 kB tx each are about 25 MB of hex.
MAX_REPLY = 64 * 1024 * 1024
LOGS_CHUNK = 10000  # SPEC 5.2: eth_getLogs ranges start at 10,000 blocks (Infura's cap)
SPARSE_LOGS = 1000  # a range with fewer logs than this lets the next one grow back

# How nodes say an eth_getLogs reply would be too big or its range too wide:
#   Infura    -32005 "query returned more than 10000 results. Try with this block range [...]"
#   Alchemy   "Log response size exceeded. ..."; its free tier "up to a 10 block range"
#   others    "exceed maximum block range", "block range is too wide", "too many logs",
#             "response too large"
# Infura also uses -32005 for rate limits ("daily request count exceeded,
# request rate limited"): those are retried with backoff, not split.
_TOO_MANY = re.compile(r'more than \d+ (results|logs)|too many (results|logs)|response size|'
                       r'response too large|(block )?range.*(too|large|wide|exceed)|exceed.*range|'
                       r'up to a \d+ block range|expanded block range', re.I)
# How a node states its block-range cap, e.g. Infura on Base: -32602 "range
# 50000 exceeds limit of 10000"; others "exceed maximum block range: 5000",
# "up to a 2K block range" (Alchemy, K = 1,000).
_RANGE_LIMIT = re.compile(r'exceeds? (?:the )?limit of (\d+)|maximum block range(?: is|:| of)? (\d+)|'
                          r'up to a (\d+)(k?) block range', re.I)
_RATE_LIMIT = re.compile(r'rate.?limit|request count|credits|capacity|too many requests|'
                         r'request rate|throttl', re.I)


def stated_range_limit(message):
    """The block-range cap a node's error message states, or None."""
    m = _RANGE_LIMIT.search(str(message or ''))
    if not m:
        return None
    if m.group(1) or m.group(2):
        n = int(m.group(1) or m.group(2))
    else:
        n = int(m.group(3)) * (1000 if m.group(4) else 1)
    return n if n >= 1 else None


def classify(message, code=None, http_status=None):
    """(too_many_results, rate_limited) for a node's error message and code."""
    message = str(message or '')
    rate = http_status == 429 or bool(_RATE_LIMIT.search(message))
    too_many = bool(_TOO_MANY.search(message)) and not rate
    if code == -32005 and not too_many:
        rate = True
    return too_many, rate


def error_kind(e):
    """A transport error's type, and its cause's type and errno, without any
    message text (urllib's can hold the URL, and so the API key):
    'URLError(gaierror 8)' for a failed DNS lookup, 'URLError(TimeoutError)',
    'URLError(ConnectionRefusedError 61)', 'TimeoutError'."""
    kind = type(e).__name__
    reason = getattr(e, 'reason', None)
    if isinstance(reason, BaseException):
        errno = getattr(reason, 'errno', None)
        kind += '({}{})'.format(type(reason).__name__, ' {}'.format(errno) if isinstance(errno, int) else '')
    return kind


class EvmError(Exception):
    """An RPC failure. Its text never holds the URL or any part of a reply
    (SPEC 5.2, "Secrets"): evm.py builds it from the method name, the error's
    type and its code only. The node's message is classified on arrival into
    .too_many (split the eth_getLogs range) and .rate_limited (back off), then
    dropped; .range_limit keeps the block-range cap it states (a number), if
    any. .data keeps a revert's data (for decoding error names); it is never
    part of the text."""

    def __init__(self, message, code=None, data=None, too_many=None, rate_limited=None, range_limit=None):
        super().__init__(message)
        self.code, self.data = code, data
        tm, rl = classify(message, code)
        self.too_many = tm if too_many is None else too_many
        self.rate_limited = rl if rate_limited is None else rate_limited
        self.range_limit = stated_range_limit(message) if range_limit is None else range_limit


def too_many_results(err):
    """True if an EvmError from eth_getLogs means "split the range"."""
    return bool(getattr(err, 'too_many', False))


class EvmRpc:
    """An EVM node's JSON-RPC at url (http or https, an API key in the path is fine).

    call() is one attempt; request() retries rate-limited calls with
    exponential backoff (rate_limit_backoff, doubling, rate_limit_retries
    times), and every helper below uses it."""

    rate_limit_retries = 3
    rate_limit_backoff = 0.25   # seconds before the first retry

    def __init__(self, url, timeout=60):
        self.url, self.timeout, self._ids, self._chain_id = url, timeout, itertools.count(1), None
        self.calls = collections.Counter()      # HTTP requests by method (for credit accounting)
        self._calls_lock = threading.Lock()
        self.logs_range_cap = None  # the eth_getLogs block-range cap the node stated, once it has

    def call(self, method, *params):
        """One JSON-RPC call; raises EvmError on any failure. Safe to call from
        several threads (each call is its own HTTP request; next() on
        itertools.count is atomic under the GIL)."""
        req = json.dumps({'jsonrpc': '2.0', 'id': next(self._ids), 'method': method,
                          'params': list(params)}).encode()
        with self._calls_lock:
            self.calls[method] += 1
        status = None
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    self.url, req, {'Content-Type': 'application/json'}), timeout=self.timeout) as r:
                body = r.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = e.read(MAX_REPLY + 1)
            except Exception:
                body = b''
            finally:
                e.close()
        except Exception as e:
            # Never str(e): urllib puts the URL (and so the API key) in some of
            # its messages ("unknown url type: ...").
            raise EvmError('{}: {}'.format(method, error_kind(e))) from None
        if len(body) > MAX_REPLY:
            raise EvmError('{}: reply over {} bytes'.format(method, MAX_REPLY))
        try:
            resp = json.loads(body.decode())
            if not isinstance(resp, dict):
                raise ValueError
        except (ValueError, RecursionError):      # RecursionError: absurdly nested JSON
            too_many, rate = classify('', None, status)
            raise EvmError('{}: bad reply (HTTP {})'.format(method, status or 200),
                           too_many=too_many, rate_limited=rate) from None
        err = resp.get('error')
        if err:
            code = err.get('code') if isinstance(err, dict) else None
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            msg = str((err.get('message') if isinstance(err, dict) else err) or '')
            too_many, rate = classify(msg, code, status)
            raise EvmError('{}: JSON-RPC error {}'.format(method, code), code,
                           err.get('data') if isinstance(err, dict) else None,
                           too_many=too_many, rate_limited=rate, range_limit=stated_range_limit(msg) or 0)
        if status is not None:
            too_many, rate = classify('', None, status)
            raise EvmError('{}: HTTP {}'.format(method, status), too_many=too_many, rate_limited=rate)
        return resp.get('result')

    def request(self, method, *params):
        """call(), retried with exponential backoff while it is rate limited."""
        delay = self.rate_limit_backoff
        for attempt in range(self.rate_limit_retries + 1):
            try:
                return self.call(method, *params)
            except EvmError as e:
                if not e.rate_limited or attempt == self.rate_limit_retries:
                    raise
            time.sleep(delay)
            delay *= 2

    def chain_id(self):
        if self._chain_id is None:
            self._chain_id = int(self.request('eth_chainId'), 16)
        return self._chain_id

    def nonce(self, address, block='pending'):
        return int(self.request('eth_getTransactionCount', address, block), 16)

    def block(self, tag='latest', full=False):
        """A block by number or by tag: latest, safe, finalized, earliest, pending."""
        return self.request('eth_getBlockByNumber', qty(tag) if isinstance(tag, int) else tag, full)

    def fees(self):
        """(maxPriorityFeePerGas, maxFeePerGas) for a transaction sent now."""
        base = self.block('latest').get('baseFeePerGas')
        if base is None:  # no EIP-1559 on this chain: both fields act as gasPrice
            gp = int(self.request('eth_gasPrice'), 16)
            return gp, gp
        base = int(base, 16)
        try:
            tip = int(self.request('eth_maxPriorityFeePerGas'), 16)
        except EvmError:
            try:
                rewards = sorted(int(r[0], 16) for r in
                                 self.request('eth_feeHistory', qty(5), 'latest', [50])['reward'] if r)
                tip = rewards[len(rewards) // 2] if rewards else 0
            except (EvmError, KeyError, TypeError):
                tip = max(int(self.request('eth_gasPrice'), 16) - base, 0)
        return tip, 2 * base + tip

    @staticmethod
    def _callobj(to, data, value, sender):
        obj = {'data': '0x' + bytes(data).hex(), 'value': qty(value)}
        if to:
            obj['to'] = to
        if sender:
            obj['from'] = sender
        return obj

    def estimate_gas(self, sender, to, data=b'', value=0, block=None):
        """block: None leaves it to the node (anvil then estimates against
        its pool, i.e. `pending`), or a tag/number to pin it."""
        params = [self._callobj(to, data, value, sender)]
        if block is not None:
            params.append(qty(block) if isinstance(block, int) else block)
        return int(self.request('eth_estimateGas', *params), 16)

    def eth_call(self, to, data, block='latest', value=0, sender=None):
        """block: a tag, a number, or {'blockHash': ..., 'requireCanonical': ...} (EIP-1898)."""
        if isinstance(block, int):
            block = qty(block)
        return unhex(self.request('eth_call', self._callobj(to, data, value, sender), block))

    def code(self, address, block='latest'):
        return unhex(self.request('eth_getCode', address, block))

    def get_logs(self, address, topics=None, from_block=0, to_block='latest'):
        f = {'address': address, 'fromBlock': qty(from_block) if isinstance(from_block, int)
             else from_block, 'toBlock': qty(to_block) if isinstance(to_block, int) else to_block}
        if topics is not None:
            f['topics'] = topics
        return self.request('eth_getLogs', f)

    def get_logs_chunked(self, address, topics, from_block, to_block, chunk=None):
        """eth_getLogs over [from_block, to_block] (block numbers, inclusive) in
        ranges of at most `chunk` blocks (LOGS_CHUNK), or the block-range cap
        the node stated ("range N exceeds limit of M": M is adopted for this
        RPC, remembered, and the range retried). A range is halved whenever
        the node reports too many results (Infura caps a reply at 10,000
        logs) and grows back toward the cap after each sparse one. Yields
        (first, last, logs) per range, in block order, so a caller can store
        each range as it comes. Raises EvmError if a single block still has
        too many results, or on any other failure (rate limits are retried
        inside request())."""
        if from_block > to_block:
            return
        cap = max(1, int(chunk or LOGS_CHUNK))
        if self.logs_range_cap:
            cap = min(cap, self.logs_range_cap)
        size = cap
        start = from_block
        while start <= to_block:
            end = min(to_block, start + size - 1)
            try:
                logs = self.get_logs(address, topics, start, end)
            except EvmError as e:
                limit = getattr(e, 'range_limit', None)
                if limit and limit < end - start + 1:
                    # Not a failure: the node said what it takes.
                    self.logs_range_cap = min(limit, self.logs_range_cap or limit)
                    cap, size = min(cap, limit), min(size, limit)
                    continue
                if not too_many_results(e) or end == start:
                    raise
                size = max(1, (end - start + 1) // 2)
                continue
            logs = logs or []
            yield start, end, logs
            start = end + 1
            if len(logs) < SPARSE_LOGS:
                size = min(cap, size * 2)

    def send_raw(self, raw):
        return self.request('eth_sendRawTransaction', '0x' + raw.hex())

    def wait_receipt(self, txhash, timeout=120, poll=0.2):
        end = time.monotonic() + timeout
        while True:
            r = self.request('eth_getTransactionReceipt', txhash)
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
        if not isinstance(got, str) or got.lower() != txhash:
            raise EvmError('eth_sendRawTransaction: the node returned another hash than {}'.format(txhash))
        receipt = self.wait_receipt(txhash, timeout)
        if int(receipt['status'], 16) != 1:
            raise EvmError('transaction {} reverted'.format(txhash), data=receipt)
        return receipt
