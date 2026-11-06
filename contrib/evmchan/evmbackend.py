#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The Bitcoin backend plugin of SPEC 5: Core Lightning runs unchanged with
`--network=regtest --disable-plugin=bcli --plugin=evmbackend.py` and sees the
UtxoChain contract as a chain of Bitcoin blocks.

    lightningd --network=regtest --disable-plugin=bcli \\
        --plugin=contrib/evmchan/evmbackend.py \\
        --evm-rpc=<url> --evm-contract=<address> [SPEC 5.6 options]

What it does (section numbers are SPEC's):
- 5.1 options; the node's own ETH key (evm-key-file, created 0600).
- 5.2 the index (evmindex.Index), synced by a poll thread every evm-poll s.
- 5.3 bcli's five methods. The read methods answer from the index only and
  never wait on a send; sendrawtransaction runs on one send worker (sends are
  serialized per key) and replies through pyln's async replies, always
  within 2 x INTERVAL (a timer answers if the worker hasn't). A send that
  gets no receipt in time is tracked and, an INTERVAL later, replaced with
  the same nonce at 1.25x the fees.
- 5.4 hooks that keep CLN on what the contract can spend.
- 5.5 evm-deposit, evm-exitaddr, evm-withdraw, evm-status, and alarms.
- 5.6 the CLN option check, from a background thread after init.

No backend method ever returns a JSON-RPC error (lightningd treats one as
fatal): every failure is an answer inside `result`.

Threads: pyln's main loop (methods and hooks; never blocks on the network),
the poll thread (Index.sync, alarms), the send worker (every EVM tx from the
node key), the option-check thread (once), and short-lived threads for the
async hooks and RPCs that need lightningd or the EVM node.

Secrets (5.1, 5.2): the RPC URL (and the Infura key in it) and the ETH key
never reach a log or an error. evm.EvmError carries only the method, the
error's type and its code, and every message goes through scrub() as well.

The logic is in class Backend, which has no pyln dependency (the unit tests
drive it directly); main() wires it to pyln.
"""
import base64
import collections
import decimal
import json
import os
import queue
import stat
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import coincurve   # noqa: E402

import btc         # noqa: E402
import evm         # noqa: E402
import evmindex    # noqa: E402

# ------------------------------------------------------------ constants

SAT_WEI = 10**10                      # SPEC 2: 1 sat = 10 gwei
GAS_CAP = 12_000_000                  # SPEC 4.5 / 5.3: refuse a submit estimated above this
GAS_LIMIT_MAX = 16_000_000            # below EIP-7825's 16,777,216 per-tx cap
MAX_HTLCS = 30                        # SPEC 5.4 / 5.6
BUMP_NUM, BUMP_DEN = 5, 4             # replacement fees: 1.25x
FEE_ALARM_MULTIPLE = 10               # BROKEN once a replacement's fees pass 10x the first send's
CONNECT_RETRY = 60                    # seconds init keeps trying the EVM node (bcli's default)
RECENT_KEEP = 64                      # mined sends remembered, to re-send if a reorg takes their nonce back
CLN_DEFAULT_POLL = 30                 # lightningd polls the backend every 30 s without --dev-bitcoind-poll
SUBMIT_SIG = 'submit(bytes,(uint64,bytes,uint32)[])'
SUBMIT_TYPES = ['bytes', '(uint64,bytes,uint32)[]']

ESTIMATEFEES = {'feerate_floor': 1000,
                'feerates': [{'blocks': 2, 'feerate': 5000}, {'blocks': 6, 'feerate': 4000},
                             {'blocks': 12, 'feerate': 3000}, {'blocks': 100, 'feerate': 1000}]}

# Every custom error of contracts/src/Errors.sol by selector, plus Solidity's
# Error(string) and Panic(uint256). Embedded so the plugin never depends on
# build output; tests/test_backend.py checks it against Errors.sol and the ABI.
ERRORS = {
    '085de625': 'TooEarly()',
    'ab8c720a': 'BadInterval()',
    'd2169344': 'HeightOverflow()',
    '259e8a16': 'BadDepositValue()',
    'e68ef9b9': 'BadDepositScript()',
    'd0d04f60': 'NothingToWithdraw()',
    '750b219c': 'WithdrawFailed()',
    'cf3c5481': 'TxTooLarge()',
    'cdda5c84': 'Truncated()',
    'd47a5751': 'NonCanonicalCompactSize()',
    'd4c08c19': 'BadSegwitFlag()',
    '0b858f8d': 'EmptyWitnesses()',
    '315fd5b2': 'NoInputs()',
    '9db7282b': 'NoOutputs()',
    '1054cca6': 'TrailingBytes()',
    '752a077a': 'PrevoutCountMismatch()',
    '614e7c97': 'ScriptSigNotEmpty(uint256)',
    '3f785f48': 'NullPrevout(uint256)',
    '73f9ec56': 'DuplicateInput(uint256)',
    'cd7f81f9': 'UnknownUtxo(uint256)',
    '06250401': 'AmountTooLarge()',
    'af548d92': 'OutputsExceedInputs()',
    '9c2f1072': 'TooManySigops()',
    'c97a0f02': 'NonFinal()',
    'a3b7efd7': 'TimeRelativeLockUnsupported(uint256)',
    '4c4259a3': 'RelativeLockNotMet(uint256)',
    'd2d4a3fc': 'UnsupportedScriptPubKey(uint256)',
    'e644bc14': 'BadWitness(uint256)',
    '2f6248ab': 'KeyHashMismatch(uint256)',
    '224c9181': 'WitnessScriptMismatch(uint256)',
    '022f31a0': 'UnknownWitnessScript(uint256)',
    '47bdd76e': 'BadPreimage(uint256)',
    '55905e4e': 'CsvNotMet(uint256)',
    'cd1e5411': 'CltvNotMet(uint256)',
    '67f58716': 'BadSignatureEncoding(uint256)',
    'a1c97319': 'BadSignature(uint256)',
    'd212a85f': 'BadPublicKey(uint256)',
    '6c3def5b': 'NoMatchingOutput(uint256)',
    '08c379a0': 'Error(string)',
    '4e487b71': 'Panic(uint256)',
}

# SPEC 5.4: anchors_zero_fee_htlc_tx (22) with static_remotekey (12), and
# optionally scid_alias (46) and zeroconf (50).
CTYPE_REQUIRED = frozenset((12, 22))
CTYPE_ALLOWED = frozenset((12, 22, 46, 50))
ANCHOR_BITS = (22, 23)
SPLICE_BITS = (62, 63)

# SPEC 5.6 (draft 5): minimums in wall-clock seconds; the anvil column only
# on chain id 31337.
ANVIL_CHAIN_ID = 31337
ANVIL_SECONDS = {'watchtime-blocks': 80, 'cltv-delta': 80, 'cltv-final': 80}
PUBLIC_SECONDS = {'watchtime-blocks': 24 * 3600, 'cltv-delta': 8640, 'cltv-final': 8640}

# Positional parameter names of the commands the rpc_command hook checks, in
# the order lightningd/the spender plugin parse them (not the schemas' order).
PARAMS = {
    'fundchannel': ['id', 'amount', 'feerate', 'announce', 'minconf', 'utxos', 'push_msat', 'close_to',
                    'request_amt', 'compact_lease', 'mindepth', 'reserve', 'channel_type'],
    'fundchannel_start': ['id', 'amount', 'feerate', 'announce', 'close_to', 'push_msat', 'mindepth',
                          'reserve', 'channel_type', 'asset', 'fee_reserve'],
    'multifundchannel': ['destinations', 'feerate', 'minconf', 'utxos', 'minchannels', 'commitment_feerate'],
    'openchannel_init': ['id', 'amount', 'initialpsbt', 'commitment_feerate', 'funding_feerate', 'announce',
                         'close_to', 'request_amt', 'compact_lease', 'channel_type'],
    'openchannel_signed': ['channel_id', 'signed_psbt'],
    'openchannel_update': ['channel_id', 'psbt'],
    'fundchannel_complete': ['id', 'psbt', 'withhold'],
}
OPEN_COMMANDS = ('fundchannel', 'fundchannel_start', 'multifundchannel', 'openchannel_init')
RPC_FILTER = OPEN_COMMANDS + ('openchannel_update', 'openchannel_signed', 'fundchannel_complete')
ERR_REFUSED = -32602    # JSONRPC2_INVALID_PARAMS: what the rpc_command hook returns
DEV_NO_OUTBOUND_FILTER = 'evm-dev-no-outbound-filter'   # tests only, with --developer (SPEC 5.4)


class ConfigError(Exception):
    """Bad options or key file. The text never holds the URL or the key."""


class UserError(Exception):
    """A message for the caller of one of our RPCs, written by us (never
    holding any part of a node's reply)."""


# ------------------------------------------------------------ small helpers

def scrub_text(text, secrets):
    text = str(text)
    for s in secrets:
        if s:
            text = text.replace(s, '<redacted>')
    return text


def feature_set(hexstr, bit):
    """True if feature `bit` is set in a hex feature field (big-endian bit
    field: bit 0 is the low bit of the last byte)."""
    if not hexstr:
        return False
    return bool((int(hexstr, 16) >> bit) & 1)


def ctype_problem(bits):
    """None if a channel_type (list of bit numbers) is one SPEC 5.4 allows,
    else why not."""
    if not isinstance(bits, list) or not all(isinstance(b, int) and not isinstance(b, bool) for b in bits):
        return 'channel_type must be a list of feature bits'
    s = set(bits)
    if not CTYPE_REQUIRED <= s or not s <= CTYPE_ALLOWED:
        return ('channel_type {} is not allowed on this chain: only anchors_zero_fee_htlc_tx (22) with '
                'static_remotekey (12), optionally scid_alias (46) and zeroconf (50)'.format(sorted(s)))
    return None


def parse_sat(v):
    """An amount as CLN's param_sat(_or_all) takes it: 'all', an integer of
    sats, or a string with a sat/msat/btc suffix. Returns int sats, 'all', or
    None if it can't be read (lightningd will reject it itself)."""
    if v == 'all':
        return 'all'
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        if hasattr(v, 'millisatoshis'):     # pyln Millisatoshi
            return int(v.millisatoshis) // 1000
        s = str(v).strip().lower()
        if s.endswith('msat'):
            return int(s[:-4]) // 1000
        if s.endswith('sat'):
            return int(s[:-3])
        if s.endswith('btc'):
            return int(decimal.Decimal(s[:-3]) * 10**8)
        return int(s)
    except (ValueError, ArithmeticError):
        return None


def msat_int(v):
    if hasattr(v, 'millisatoshis'):
        return int(v.millisatoshis)
    if isinstance(v, str) and v.endswith('msat'):
        return int(v[:-4])
    return int(v)


def named_params(method, params):
    """The parameters of a JSON-RPC request as a dict, whether they came by
    name or by position; None if they are neither."""
    if isinstance(params, dict):
        return params
    if isinstance(params, list):
        names = PARAMS.get(method, [])
        if len(params) > len(names):
            return None
        return {n: v for n, v in zip(names, params) if v is not None}
    if params is None:
        return {}
    return None


def revert_reason(err):
    """The contract error a failed eth_call/eth_estimateGas reverted with, as
    'Name' or 'Name(arg)' (e.g. 'NonFinal', 'BadSignature(1)'), or a generic
    description. Only selectors and decoded arguments: nothing else of the
    node's reply."""
    data = getattr(err, 'data', None)
    for _ in range(3):        # some nodes nest it: {data: {data: '0x..'}}
        if isinstance(data, dict):
            data = data.get('data')
    if not isinstance(data, str) or not data.startswith('0x'):
        if getattr(err, 'code', None) == 3:
            return 'reverted'
        return str(err)
    try:
        b = evm.unhex(data)
    except ValueError:
        return 'reverted'
    if len(b) < 4:
        return 'reverted'
    sig = ERRORS.get(b[:4].hex())
    if sig is None:
        return 'reverted with an unknown error'
    name, types = sig.split('(', 1)
    types = [t for t in types[:-1].split(',') if t]
    if not types or name == 'Error':
        return name     # Error(string)'s text is the node's: never copied
    try:
        args = evm.abi_decode(types, b[4:])
    except ValueError:
        return name
    if name == 'Panic':
        return 'Panic(0x{:x})'.format(args[0])
    return '{}({})'.format(name, ','.join(str(a) for a in args))


# ------------------------------------------------------------ PSBT (BIP174 v0 and BIP370 v2)

def _compact(r):
    n = r.take(1)[0]
    if n < 0xfd:
        return n
    size = {0xfd: 2, 0xfe: 4, 0xff: 8}[n]
    return int.from_bytes(r.take(size), 'little')


def _psbt_map(r):
    """[(key bytes, value bytes)] of one PSBT map."""
    out = []
    while True:
        klen = _compact(r)
        if klen == 0:
            return out
        key = r.take(klen)
        out.append((key, r.take(_compact(r))))


def _unsigned_tx_inputs(raw):
    """[(prev txid internal, vout)] of a PSBT v0's unsigned tx (no witness;
    zero inputs or outputs allowed, unlike btc.Tx.parse)."""
    r = btc.Reader(raw)
    r.take(4)
    out = []
    for _ in range(_compact(r)):
        prev, vout = r.take(32), r.u32()
        r.take(_compact(r))
        r.take(4)
        out.append((prev, vout))
    return out


def psbt_inputs(b64):
    """For each input of a PSBT: (prevout txid internal or None, vout or
    None, scriptPubKey of the coin it spends or None, why it can't be
    checked or None). Raises ValueError if the PSBT doesn't parse."""
    raw = base64.b64decode(b64, validate=True)
    if raw[:5] != b'psbt\xff':
        raise ValueError('not a PSBT')
    r = btc.Reader(raw[5:])
    glob = _psbt_map(r)
    version, n_in, prevouts = 0, None, None
    for k, v in glob:
        if k == b'\x00':
            prevouts = _unsigned_tx_inputs(v)
            n_in = len(prevouts)
        elif k == b'\xfb':
            version = int.from_bytes(v, 'little')
        elif k == b'\x04':
            n_in = _compact(btc.Reader(v))
    if version not in (0, 2) or n_in is None:
        raise ValueError('unsupported PSBT')
    out = []
    for i in range(n_in):
        m = dict((k, v) for k, v in _psbt_map(r) if len(k) == 1)   # keys without key data
        prev, vout = prevouts[i] if prevouts else (m.get(b'\x0e'), None)
        if vout is None and b'\x0f' in m:
            vout = int.from_bytes(m[b'\x0f'], 'little')
        spk, why = None, None
        if b'\x01' in m:                  # witness utxo: amount, script
            wr = btc.Reader(m[b'\x01'])
            wr.u64()
            spk = wr.take(_compact(wr))
        if b'\x00' in m:                  # non-witness utxo: the whole previous tx
            try:
                ptx = btc.Tx.parse(m[b'\x00'])
                if prev is not None and ptx.txid() != prev:
                    why = 'its previous tx does not match its prevout'
                elif vout is None or vout >= len(ptx.outputs):
                    why = 'its prevout index is out of range'
                elif spk is not None and spk != ptx.outputs[vout][1]:
                    why = 'its witness utxo and previous tx disagree'
                else:
                    spk = ptx.outputs[vout][1]
            except ValueError:
                why = 'its previous tx does not parse'
        if spk is None and why is None:
            why = 'it carries no utxo information'
        if why is None and (m.get(b'\x04') or m.get(b'\x07')):
            why = 'it has a redeem script or scriptSig'
        out.append((prev, vout, spk, why))
    return out


def is_segwit_wallet_script(spk):
    """P2WPKH or P2TR: the only wallet inputs the contract can spend."""
    return (len(spk) == 22 and spk[:2] == b'\x00\x14') or (len(spk) == 34 and spk[:2] == b'\x51\x20')


def psbt_problem(b64):
    """None if every input of the PSBT spends a P2WPKH or P2TR output, else
    why not. No count limit (SPEC 5.3 step 3 answers that)."""
    try:
        ins = psbt_inputs(b64)
    except (ValueError, KeyError, IndexError, TypeError):
        return 'the PSBT does not parse'
    for i, (_, _, spk, why) in enumerate(ins):
        if why:
            return 'funding input {}: {}'.format(i, why)
        if not is_segwit_wallet_script(spk):
            return 'funding input {} is not P2WPKH or P2TR (the contract could never spend it)'.format(i)
    return None


# ------------------------------------------------------------ options (5.1) and the key file

class Config:
    def __init__(self, rpc_url, contract, key, key_file, confirm, poll, min_balance_wei, lightning_dir,
                 secrets=(), dev_no_outbound_filter=False):
        self.rpc_url, self.contract, self.key, self.key_file = rpc_url, contract, key, key_file
        self.confirm, self.poll, self.min_balance_wei = confirm, poll, min_balance_wei
        self.lightning_dir = lightning_dir
        self.secrets = [s for s in secrets if s]
        # Tests only (SPEC 5.4): the rpc_command hook lets our own opens
        # through, so a peer's hooks are what refuses them.
        self.dev_no_outbound_filter = dev_no_outbound_filter

    @classmethod
    def from_options(cls, get_option, lightning_dir, env, log=None, developer=False):
        """From the plugin's options (get_option(name)) and the environment.
        developer: lightningd runs with --developer. Creates the key file if
        it doesn't exist. Raises ConfigError."""
        url = get_option('evm-rpc')
        if not url:
            raise ConfigError('--evm-rpc is required')
        secrets = []
        if '${INFURA_API_KEY}' in url:
            k = env.get('INFURA_API_KEY')
            if not k:
                raise ConfigError('--evm-rpc contains ${INFURA_API_KEY} but INFURA_API_KEY is not set')
            secrets.append(k)
            url = url.replace('${INFURA_API_KEY}', k)
        if not (url.startswith('http://') or url.startswith('https://')):
            raise ConfigError('--evm-rpc must be an http:// or https:// URL')
        secrets.append(url)
        contract = get_option('evm-contract')
        try:
            contract = evm.checksum_address(contract or '')
        except ValueError:
            raise ConfigError('--evm-contract must be a 20-byte hex address') from None
        confirm = get_option('evm-confirm') or 'safe'
        if confirm not in evmindex.TAGS:
            raise ConfigError('--evm-confirm must be safe or latest')
        try:
            poll = float(get_option('evm-poll'))
        except (TypeError, ValueError):
            raise ConfigError('--evm-poll must be a number of seconds') from None
        if not 0 < poll <= 3600:
            raise ConfigError('--evm-poll must be between 0 and 3600 seconds')
        try:
            min_balance = decimal.Decimal(str(get_option('evm-min-balance')))
            if not min_balance.is_finite() or min_balance < 0:
                raise ValueError
            min_balance_wei = int(min_balance * 10**18)
        except (ValueError, ArithmeticError):
            raise ConfigError('--evm-min-balance must be a non-negative amount of ETH') from None
        key_file = get_option('evm-key-file') or os.path.join(lightning_dir, 'evm-key')
        key_file = os.path.join(lightning_dir, key_file)   # relative to the lightning dir
        no_filter = get_option(DEV_NO_OUTBOUND_FILTER) in (True, 'true')
        if no_filter and not developer:
            raise ConfigError('--{} is for tests: it needs lightningd --developer'.format(DEV_NO_OUTBOUND_FILTER))
        key = load_or_create_key(key_file, log)
        secrets.append(key.hex())
        return cls(url, contract, key, key_file, confirm, poll, min_balance_wei, lightning_dir, secrets,
                   dev_no_outbound_filter=no_filter)


def load_or_create_key(path, log=None):
    """The 32-byte ETH key in `path` (hex), created with mode 0600 if the
    file doesn't exist. Raises ConfigError (never with the key in it)."""
    log = log or (lambda level, msg: None)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            st = os.stat(path)
            with open(path) as f:
                text = f.read().strip()
        except OSError as e:
            raise ConfigError('cannot read evm-key-file ({})'.format(type(e).__name__)) from None
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log('broken', 'evm-key-file {} is accessible by group or others: chmod 600 it'.format(path))
        try:
            key = evm.unhex(text)
            if len(key) != 32:
                raise ValueError
            coincurve.PrivateKey(key)
        except Exception:
            raise ConfigError('evm-key-file {} does not hold a valid 32-byte hex key'.format(path)) from None
        return key
    except OSError as e:
        raise ConfigError('cannot create evm-key-file {} ({})'.format(path, type(e).__name__)) from None
    while True:
        key = os.urandom(32)
        try:
            coincurve.PrivateKey(key)
            break
        except Exception:
            continue
    with os.fdopen(fd, 'w') as f:
        f.write('0x' + key.hex() + '\n')
        f.flush()
        os.fsync(f.fileno())
    log('info', 'created a new EVM key in {} for {}: fund it to pay for gas'.format(
        path, evm.Account(key).address))
    return key


def option_minimums(interval, chain_id):
    """SPEC 5.6 (draft 5): the minimums as ({option: blocks}, most seconds
    lightningd may poll). They are wall-clock times divided by INTERVAL,
    rounded up; the anvil column only on chain id 31337."""
    anvil = chain_id == ANVIL_CHAIN_ID
    secs = ANVIL_SECONDS if anvil else PUBLIC_SECONDS
    mins = {k: -(-v // interval) for k, v in secs.items()}
    mins['funding-confirms'] = 1 if anvil else 3
    poll = 1 if anvil else max(1, interval / 2.0)
    return mins, poll


def _config_value(configs, name):
    c = configs.get(name)
    if not isinstance(c, dict):
        return None
    for k in ('value_int', 'value_bool', 'value_str', 'value_msat'):
        if k in c:
            return c[k]
    if 'set' in c:
        return c['set']
    return None


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_options(configs, info, interval, chain_id):
    """SPEC 5.6: the problems with lightningd's options (`listconfigs`'s
    'configs' and `getinfo`), [] if none."""
    problems = []
    mins, most_poll = option_minimums(interval, chain_id)
    for name, need in mins.items():
        v = _config_value(configs, name)
        if not _is_int(v) or v < need:
            problems.append('--{} is {}, needs at least {} (INTERVAL {} s{})'.format(
                name, v, need, interval, ', anvil' if chain_id == ANVIL_CHAIN_ID else ''))
    v = _config_value(configs, 'max-concurrent-htlcs')
    if not _is_int(v) or v > MAX_HTLCS:
        problems.append('--max-concurrent-htlcs is {}, needs at most {}'.format(v, MAX_HTLCS))
    dev = _config_value(configs, 'developer') is True
    if not dev:
        problems.append('--developer is not set (needed for --dev-force-features=-63)')
    poll = cln_poll_seconds(configs)
    if poll > most_poll:
        problems.append('lightningd polls the backend every {} s, needs at most {:g} s (--dev-bitcoind-poll)'
                        .format(poll, most_poll))
    feats = (info or {}).get('our_features') or {}
    for kind in ('init', 'node'):
        try:
            on = [b for b in SPLICE_BITS if feature_set(feats.get(kind), b)]
        except (TypeError, ValueError):
            on = ['?']
        if on:
            problems.append('splicing is on ({} feature bit {}): needs --dev-force-features=-63'
                            .format(kind, on[0]))
    return problems


def cln_poll_seconds(configs):
    if _config_value(configs, 'developer') is True:
        v = _config_value(configs, 'dev-bitcoind-poll')
        if _is_int(v):
            return v
    return CLN_DEFAULT_POLL


# ------------------------------------------------------------ the send path (5.3)

NOT_SENT = 'timed out before sending: not sent'


def ok():
    return {'success': True, 'errmsg': ''}


def fail(msg):
    return {'success': False, 'errmsg': msg}


class Reply:
    """Answers one sendrawtransaction exactly once, within its deadline.

    state goes 'queued' -> 'checking' -> 'sending'. commit() moves to
    'sending' (the send is persisted and about to go to the node) only if
    nothing has answered yet; from then on the deadline timer answers
    success, never failure: a committed send is tracked and will be
    (re)sent, and CLN reads failure as "not sent" (SPEC 5.3 step 4)."""

    def __init__(self, respond, deadline=None):
        self._respond, self.deadline = respond, deadline
        self._lock = threading.Lock()
        self.done = False
        self.result = None
        self.state = 'queued'
        self.event = threading.Event()
        self._timer = None
        if deadline is not None:
            self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._expire)
            self._timer.daemon = True
            self._timer.start()

    def _deliver(self, result):
        if self._timer is not None:
            self._timer.cancel()
        try:
            self._respond(result)
        except Exception:
            pass
        self.event.set()

    def answer(self, result):
        with self._lock:
            if self.done:
                return False
            self.done, self.result = True, result
        self._deliver(result)
        return True

    def commit(self):
        with self._lock:
            if self.done:
                return False
            self.state = 'sending'
            return True

    def uncommit(self):
        """Back from 'sending' after a refusal: True if nothing answered yet
        (so the caller answers failure), False if the deadline already said
        success (so the send must stay tracked)."""
        with self._lock:
            if self.done:
                return False
            self.state = 'checking'
            return True

    def set_state(self, state):
        with self._lock:
            if self.state != 'sending':
                self.state = state

    def _expire(self):
        with self._lock:
            if self.done:
                return
            st = self.state
            result = ok() if st == 'sending' else fail('timed out before sending ({}): not sent'.format(st))
            self.done, self.result = True, result
        self._deliver(result)

    def expired(self):
        return self.done or (self.deadline is not None and time.monotonic() >= self.deadline)


class Pending:
    """An EVM tx from the node key at a nonce that isn't mined yet: a
    submit, a deposit/withdraw call, or a filler self-send. Persisted
    (calldata, no signatures) in <lightning-dir>/evm-pending.json before it
    is sent, so a restart keeps tracking and replacing it."""

    FIELDS = ('kind', 'btc_txid', 'nonce', 'gas', 'to', 'value', 'tip', 'max_fee', 'first_max_fee',
              'hashes', 'replacements', 'outpoints', 'hash_txid', 'requeued', 'bodies')

    def __init__(self, kind, btc_txid, nonce, gas, to, data, value, tip, max_fee, txhash, outpoints=()):
        self.kind, self.btc_txid, self.nonce = kind, btc_txid, nonce
        self.gas, self.to, self.data, self.value = gas, to, data, value
        self.tip, self.max_fee, self.first_max_fee = tip, max_fee, max_fee
        self.hashes = [txhash]
        self.hash_txid = {txhash: btc_txid}      # which btc tx each version carries
        # The submit calldata of each btc tx sent at this nonce (step 6 and a
        # cancel overwrite p.data): a reorg re-sends the version that was mined.
        self.bodies = {}
        if kind == 'submit' and btc_txid:
            self.add_body(btc_txid, data, outpoints, gas)
        self.sent = set()                        # versions handed to the node (step 5 answers only these)
        self.outpoints = list(outpoints)         # 'txid:vout' the submit spends
        self.sent_at = time.monotonic()
        self.replace_at = None
        self.replacements = 0
        self.alarmed = False
        self.requeued = False                    # tracked again after a reorg took its nonce back, not re-sent yet
        self.scan_pos = 0                        # receipt lookups: next hash to try (from the newest)
        self.missing = 0
        self.missing_since = 0.0
        self.done = threading.Event()            # set by Backend._finish / _drop
        self.result = self.receipt = None

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.FIELDS}
        d['data'] = self.data.hex()
        d['data_keccak'] = '0x' + evm.keccak256(self.data).hex()
        return d

    @classmethod
    def from_dict(cls, d):
        p = cls(d['kind'], d['btc_txid'], int(d['nonce']), int(d['gas']), d['to'], bytes.fromhex(d['data']),
                int(d['value']), int(d['tip']), int(d['max_fee']), d['hashes'][0], d.get('outpoints') or [])
        p.hashes, p.first_max_fee, p.replacements = list(d['hashes']), int(d['first_max_fee']), int(d['replacements'])
        p.hash_txid = dict(d.get('hash_txid') or {h: p.btc_txid for h in p.hashes})
        p.requeued = bool(d.get('requeued'))
        if isinstance(d.get('bodies'), dict):
            p.bodies = {t: {'data': b['data'], 'outpoints': list(b['outpoints']), 'gas': int(b['gas'])}
                        for t, b in d['bodies'].items()}
        p.sent = set(p.hashes)                   # persisted before sending: may have gone out
        return p

    def add_body(self, btc_txid, data, outpoints, gas):
        self.bodies[btc_txid] = {'data': data.hex(), 'outpoints': list(outpoints), 'gas': gas}

    def describe(self):
        return {'kind': self.kind, 'txid': self.btc_txid, 'nonce': self.nonce, 'evm_txs': list(self.hashes),
                'age_s': round(time.monotonic() - self.sent_at, 1), 'replacements': self.replacements,
                'max_fee_per_gas': self.max_fee}


# ------------------------------------------------------------ the backend

class Backend:
    """Everything but the pyln glue. log(level, message) with level
    'broken', 'unusual', 'info' or 'debug'; lnrpc() returns an object with
    .call(method, params) talking to lightningd (used only after init, from
    background threads)."""

    def __init__(self, cfg, log, lnrpc=None):
        self.cfg = cfg
        self._log = log
        self.lnrpc = lnrpc
        self.account = evm.Account(cfg.key)
        self.address = self.account.address
        self.contract = cfg.contract
        self.stop_event = threading.Event()
        self.ix = None
        self.rpc = None          # the poll thread's and the read RPCs'
        self.srpc = None         # the send worker's (short timeouts)
        self.interval = None
        self.jobs = queue.Queue()
        self.pending = {}        # nonce -> Pending (the send worker owns it; others read under _plock)
        self._plock = threading.Lock()
        self.threads = []
        # option check (5.6)
        self.options_checked = False
        self.option_problems = []
        self.cln_poll = CLN_DEFAULT_POLL
        self.cln_configs = {}
        # balance and alarms (5.5)
        self.balance = None
        self.balance_at = 0.0
        self.balance_dirty = True
        self.alarms = {}         # name -> {'since', 'logged', 'message'}
        self.started_at = time.time()   # rpc_calls in evm-status count from here
        self.sync_error_since = None
        self.last_sync_error = None
        self.tag_seen = None     # (tag block number, time.time() when it changed)
        self._init_tag_ts = None
        self._last_chaininfo = None
        self.nonce_hwm = 0       # one above the highest nonce we have used
        self._hwm_init = False
        self.mined_floor = 0     # one above the highest nonce we saw mined
        # Sends seen mined, by nonce (the last RECENT_KEEP), each as the
        # version that was mined: if a reorg takes their nonces back, they are
        # tracked and re-sent again (M4). Persisted with the pending sends.
        self.recent = collections.OrderedDict()
        self._behind_since = None    # when the chain's nonce first read below mined_floor
        self._save_dirty = False
        self._klock = threading.RLock()   # signing and sending from the key (5.3 step 8)
        self._chain_id = None    # for tests without an index
        self._hlock = threading.Lock()
        self._approved = {}      # outgoing channel -> [time.monotonic() of forwards let through]
        self.pending_file = os.path.join(cfg.lightning_dir, 'evm-pending.json')

    # -------------------------------------------------------- logging

    def log(self, level, message):
        try:
            self._log(level, scrub_text(message, self.cfg.secrets))
        except Exception:
            pass

    def scrub(self, message):
        return scrub_text(message, self.cfg.secrets)

    # -------------------------------------------------------- startup

    def connect(self, retry_for=CONNECT_RETRY, rpc=None, srpc=None):
        """Opens the index (checking the contract's code and reading its
        immutables), the tag block and the balance. Retries the EVM node for
        up to retry_for seconds. Never talks to lightningd. Raises
        evm.EvmError or evmindex.EvmIndexError."""
        self.rpc = rpc or evm.EvmRpc(self.cfg.rpc_url, timeout=10)    # init must end well within 120 s
        end = time.monotonic() + retry_for
        while True:
            try:
                self.ix = evmindex.Index(os.path.join(self.cfg.lightning_dir, 'evmchan.sqlite3'), self.rpc,
                                         self.contract, tag=self.cfg.confirm, log=self.log)
                break
            except evm.EvmError as e:
                if time.monotonic() >= end:
                    raise
                self.log('unusual', 'EVM node not ready ({}); retrying'.format(e))
                time.sleep(1)
        self.interval = self.ix.interval
        # Per-call timeout well inside a send's budget (step 8).
        self.srpc = srpc or evm.EvmRpc(self.cfg.rpc_url, timeout=max(1.0, min(20.0, self.interval / 2.0)))
        # The tag's timestamp, which getchaininfo needs before the first
        # checkpoint (a new index would otherwise report height 0, and
        # lightningd would exit: "gone backwards").
        while True:
            try:
                b = self.rpc.request('eth_getBlockByNumber', self.cfg.confirm, False)
                self._init_tag_ts = int(b['timestamp'], 16)
                break
            except (evm.EvmError, KeyError, TypeError, ValueError) as e:
                if time.monotonic() >= end:
                    raise evm.EvmError('eth_getBlockByNumber: no {} block ({})'.format(
                        self.cfg.confirm, type(e).__name__)) from None
                time.sleep(1)
        try:
            self.refresh_balance(force=True)
        except Exception as e:     # balance None refuses opens until a poll reads it
            self.log('unusual', 'balance not read at startup ({})'.format(type(e).__name__))
        self._load_pending()
        self.rpc.timeout = 30
        self.log('info', 'UtxoChain {} on chain {} (INTERVAL {} s, DEPLOY_BLOCK {}); node key {}; tip {}'.format(
            self.contract, self.ix.chain_id, self.interval, self.ix.deploy_block, self.address, self.ix.tip()))

    def _load_pending(self):
        try:
            with open(self.pending_file) as f:
                saved = json.load(f)
            if isinstance(saved, list):          # before M4: the pending sends only
                saved = {'pending': saved}
            if not isinstance(saved, dict):
                raise ValueError
            mined_floor = int(saved.get('mined_floor') or 0)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError) as e:
            self.log('broken', '{} unreadable ({}): tracked sends lost'.format(self.pending_file, type(e).__name__))
            return

        def parsed(key):
            out = []
            for d in saved.get(key) or []:
                try:
                    out.append(Pending.from_dict(d))
                except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                    continue
            return sorted(out, key=lambda p: p.nonce)
        for p in parsed('pending'):
            p.replace_at = time.monotonic() if p.requeued else time.monotonic() + self.interval
            self.pending[p.nonce] = p
            self.nonce_hwm = max(self.nonce_hwm, p.nonce + 1)
        for p in parsed('recent')[-RECENT_KEEP:]:
            p.done.set()
            self.recent[p.nonce] = p
            self.nonce_hwm = max(self.nonce_hwm, p.nonce + 1)
        self.mined_floor = max(self.mined_floor, mined_floor)
        if self.pending:
            self.log('info', 'tracking {} sends from before the restart (nonces {})'.format(
                len(self.pending), sorted(self.pending)))

    def _save_pending(self):
        """Writes the tracked sends, and the ones seen mined; the caller
        holds _plock."""
        tmp = self.pending_file + '.tmp'
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'pending': [p.to_dict() for p in self.pending.values()],
                           'recent': [p.to_dict() for p in self.recent.values()],
                           'mined_floor': self.mined_floor}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.pending_file)
            dfd = os.open(os.path.dirname(self.pending_file) or '.', os.O_RDONLY)
            try:
                os.fsync(dfd)                # the rename survives a power loss
            finally:
                os.close(dfd)
            self._save_dirty = False
            return True
        except OSError as e:
            failed = type(e).__name__
        self._save_dirty = True              # the tracker retries until it is written
        self.log('broken', 'cannot save tracked sends ({})'.format(failed))
        return False

    def start(self):
        for target, name in ((self._poll_loop, 'evm-poll'), (self._worker_loop, 'evm-send'),
                             (self._tracker_loop, 'evm-track'),
                             (self._option_check_loop, 'evm-options')):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_event.set()
        self.jobs.put(None)

    # -------------------------------------------------------- the five methods (5.3)

    def getchaininfo(self):
        tip = self.ix.tip()
        if not self.ix.ibd:
            r = self.ix.chaininfo()
        else:
            # Until this process has a checkpoint: ibd, headercount from the
            # latest read of the tag (init's live read, then each poll's), so
            # a new index file never reports a chain shorter than CLN's.
            ts = None
            tb = self.ix.tag_block()
            if tb is not None:
                ts = tb[2]
            elif self._init_tag_ts is not None:
                ts = self._init_tag_ts
            head = tip if ts is None else max(self.ix.height_at(ts) - 1, tip, 0)
            r = {'chain': 'regtest', 'headercount': head, 'blockcount': tip, 'ibd': True}
        self._last_chaininfo = r
        return r

    def getchaininfo_fallback(self):
        r = self._last_chaininfo or {'chain': 'regtest', 'headercount': 0, 'blockcount': 0, 'ibd': True}
        return dict(r, ibd=True)

    def getrawblockbyheight(self, height):
        try:
            h = int(height)
        except (TypeError, ValueError):
            return {'blockhash': None, 'block': None}
        b = self.ix.block(h)
        if b is None:
            return {'blockhash': None, 'block': None}
        return {'blockhash': b[0], 'block': b[1].hex()}

    def getutxout(self, txid, vout):
        try:
            u = self.ix.utxo(str(txid), int(vout))
        except (TypeError, ValueError):
            u = None
        if u is None:
            return {'amount': None, 'script': None}
        return {'amount': u['amount'], 'script': u['script'].hex()}

    def estimatefees(self):
        return {'feerate_floor': ESTIMATEFEES['feerate_floor'],
                'feerates': [dict(x) for x in ESTIMATEFEES['feerates']]}

    def time_limit(self):
        """sendrawtransaction's budget, a little under 2 x INTERVAL."""
        two = 2.0 * self.interval
        return two - min(0.5, 0.1 * two)

    def track_tick(self):
        """About once per EVM block (2 s on OP Stack), never slower than
        INTERVAL/2."""
        return max(0.25, min(2.0, self.interval / 2.0))

    # --- steps 1, 5: answered at once, on pyln's thread

    def _known(self, txid):
        """Step 1 (in the index) or step 5 (a version of a tracked send)."""
        try:
            if self.ix.tx(txid) is not None:
                return True
        except Exception as e:     # noqa: B902
            self.log('broken', 'index read failed ({})'.format(type(e).__name__))
        with self._plock:
            return any(p.hash_txid.get(h) == txid for p in self.pending.values() if p.kind == 'submit'
                       for h in p.sent)

    def sendrawtransaction(self, txhex, respond):
        """Answers respond(result) within 2 x INTERVAL: at once for steps 1
        and 5, else from the send worker (or the deadline timer). Never
        raises. Returns the Reply."""
        reply = Reply(respond, time.monotonic() + self.time_limit())
        tx = None
        try:
            tx = btc.Tx.parse(bytes.fromhex(txhex))
        except (ValueError, TypeError):
            pass
        if tx is None:
            reply.answer(fail('the transaction does not parse'))
        elif self._known(tx.txid_hex()):
            reply.answer(ok())
        else:
            self.jobs.put(lambda dequeued: self._send_job(tx, reply, dequeued))
        return reply

    def send_now(self, txhex, timeout=None):
        """sendrawtransaction, waiting for its answer (tests, scripts)."""
        reply = self.sendrawtransaction(txhex, lambda r: None)
        reply.event.wait(timeout if timeout is not None else 2 * self.interval + 5)
        return reply.result

    # --- steps 2-4, 6, 8: on the send worker

    def _worker_loop(self):
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                continue
            failed = None
            try:
                job(time.monotonic())        # step 8: each job's budget starts when it is dequeued
            except Exception as e:           # noqa: B902
                failed = type(e).__name__
            if failed:
                self.log('broken', 'send worker: {}'.format(failed))

    def _send_job(self, tx, reply, dequeued):
        if reply.done:
            return                           # answered (timed out) while queued: not sent
        reply.set_state('checking')
        budget_end = dequeued + self.time_limit()
        txid = tx.txid_hex()
        result, p, err, internal = None, None, None, False
        try:
            result, p = self._send_steps(tx, txid, reply, budget_end)
        except evm.EvmError as e:
            err = 'EVM node: {}'.format(self.scrub(e))      # method, type and code only
        except Exception as e:               # noqa: B902
            err, internal = 'internal error ({})'.format(type(e).__name__), True
        if err is not None:                  # step 10: answered outside the handler
            if internal:
                self.log('broken', 'sendrawtransaction {}: {}'.format(txid, err))
            result = fail(err)
        reply.answer(result)
        if p is not None and result['success']:
            self._quick_receipt(p)

    def _send_steps(self, tx, txid, reply, budget_end):
        """(result, Pending or None). Exceptions only before the send is
        committed (they mean: not sent). The job's budget is checked before
        every RPC (step 8)."""
        def out_of_time():
            return time.monotonic() >= budget_end or reply.expired()
        if self._known(txid):
            return ok(), None
        outpoints = ['{}:{}'.format(btc.display(i.prev_txid), i.vout) for i in tx.inputs]
        if self._requeue_if_reorged(outpoints) and self._known(txid):
            return ok(), None                # the reorged send, re-sent at its own nonce
        prevouts = self.ix.prevouts(tx)
        if prevouts is None:
            return fail('missing or spent inputs'), None
        data = evm.calldata(SUBMIT_SIG, SUBMIT_TYPES, [tx.serialize(), [tuple(x) for x in prevouts]])
        gas, why = self._check_call(data, 0, out_of_time)
        if why is not None:
            return fail(why), None
        if out_of_time():
            return fail(NOT_SENT), None
        with self._klock:
            old = self._pending_spending(outpoints)
            if old is not None:
                return self._resign(old, txid, data, gas, outpoints, reply, budget_end), old
            return self._send_new('submit', txid, data, 0, gas, outpoints, reply, budget_end)

    def _check_call(self, data, value, out_of_time=lambda: False):
        """Step 3: (gas, None), or (None, why) if it would revert or not fit."""
        why = None
        if out_of_time():
            return None, NOT_SENT
        try:
            self.srpc.eth_call(self.contract, data, 'latest', value=value, sender=self.address)
        except evm.EvmError as e:
            why = revert_reason(e) if (e.data is not None or e.code == 3) else \
                'eth_call failed: {}'.format(self.scrub(e))
        if why is not None:
            return None, why
        if out_of_time():
            return None, NOT_SENT
        try:
            gas = self.srpc.estimate_gas(self.address, self.contract, data, value, block='latest')
        except evm.EvmError as e:
            why = revert_reason(e) if (e.data is not None or e.code == 3) else \
                'eth_estimateGas failed: {}'.format(self.scrub(e))
        if why is not None:
            return None, why
        if gas > GAS_CAP:
            return None, 'too large for one EVM tx: {} gas'.format(gas)
        return gas, None

    def _pending_spending(self, outpoints):
        """The lowest-nonce tracked submit spending any of these prevouts."""
        want = set(outpoints)
        with self._plock:
            hits = [p for p in self.pending.values() if p.kind == 'submit' and want & set(p.outpoints)]
        return min(hits, key=lambda p: p.nonce) if hits else None

    def _alloc_nonce(self):
        """Step 4: the next nonce not used, from a high-water mark that
        starts at eth_getTransactionCount(pending). The caller holds _klock."""
        if not self._hwm_init:
            self.nonce_hwm = max(self.nonce_hwm, self.srpc.nonce(self.address, 'pending'))
            self._hwm_init = True
        with self._plock:
            n = max([self.nonce_hwm] + [q + 1 for q in self.pending])
        self.nonce_hwm = n + 1
        return n

    def _gas_limit(self, gas):
        return min(gas + gas // 5 + 30_000, GAS_LIMIT_MAX)

    def _bumped(self, p, cur=None):
        """1.25x p's fees, at least +1 wei and the network's current fees
        (cur: (tip, max fee) already read)."""
        cur_tip, cur_max = cur if cur is not None else self.srpc.fees()
        tip = max(-(-p.tip * BUMP_NUM // BUMP_DEN), p.tip + 1, cur_tip)
        return tip, max(-(-p.max_fee * BUMP_NUM // BUMP_DEN), p.max_fee + 1, cur_max, tip)

    def _push(self, raw, attempts=3):
        """eth_sendRawTransaction: (accepted, refused, why). Rate-limited
        replies (never accepted) are retried with backoff. Any other error
        is a refusal only if the node then doesn't know the tx: a 5xx with a
        JSON body, "already known" or a timeout after acceptance is not one
        (step 4: an accepted tx is never reported failed). refused False
        and accepted False: unknown (transport failure), so keep tracking."""
        txhash = '0x' + evm.keccak256(raw).hex()
        why, code, delay = None, None, 0.25
        for i in range(attempts):
            limited = False
            try:
                self.srpc.call('eth_sendRawTransaction', '0x' + raw.hex())
                return True, False, None
            except evm.EvmError as e:
                why, code, limited = self.scrub(e), e.code, e.rate_limited
            if not limited or i == attempts - 1:
                break
            time.sleep(delay)
            delay *= 2
        known = None
        try:
            known = self.srpc.call('eth_getTransactionByHash', txhash) is not None
        except evm.EvmError:
            pass
        if known:
            return True, False, None
        if known is False and code is not None:
            self._hwm_init = False           # re-read the nonce: ours may be stale (H1)
            return False, True, why
        return False, False, why

    def _send_new(self, kind, btc_txid, data, value, gas, outpoints, reply, budget_end):
        """Step 4 at a new nonce: persist, sign, send; success once the
        node accepts it. The caller holds _klock. Returns (result, Pending
        or None)."""
        tip, max_fee = self.srpc.fees()
        nonce = self._alloc_nonce()
        gas_limit = self._gas_limit(gas)
        raw, txhash = self._sign(nonce, tip, max_fee, gas_limit, self.contract, value, data)
        if time.monotonic() >= budget_end:
            self._release_nonce(nonce)
            return fail(NOT_SENT), None
        p = Pending(kind, btc_txid, nonce, gas_limit, self.contract, data, value, tip, max_fee, txhash, outpoints)
        p.replace_at = time.monotonic() + self.interval
        with self._plock:
            self.pending[nonce] = p
            saved = self._save_pending()     # persisted before it is sent
            if not saved:
                del self.pending[nonce]
        if not saved:
            self._release_nonce(nonce)
            return fail('cannot persist the send: not sent'), None
        if reply is not None and not reply.commit():     # answered meanwhile: never send now
            with self._plock:
                del self.pending[nonce]
                self._save_pending()
            self._release_nonce(nonce)
            return fail(NOT_SENT), None
        accepted, refused, why = self._push(raw)
        if refused and (reply is None or reply.uncommit()):
            with self._plock:
                del self.pending[nonce]
                self._save_pending()
            self._release_nonce(nonce)
            return fail('eth_sendRawTransaction refused: {}'.format(why)), None
        with self._plock:
            p.sent.add(txhash)               # step 5 may now answer its rebroadcasts at once
        if refused:
            # The deadline already answered success (M2): keep tracking it,
            # the tracker re-sends it at this nonce.
            self.log('broken', '{} {}: refused ({}) after its time limit answered success; re-sending '
                     'nonce {}'.format(kind, btc_txid or '', why, nonce))
            p.replace_at = time.monotonic()
            return ok(), p
        if not accepted:
            # Transport failure: it may have reached the node. It is tracked,
            # and the tracker re-sends it at the next tick.
            self.log('unusual', '{} {}: eth_sendRawTransaction failed ({}); re-sending nonce {} next tick'
                     .format(kind, btc_txid or '', why, nonce))
            p.replace_at = time.monotonic()
        self.balance_dirty = True
        self.log('debug', '{} {} sent as EVM tx {} (nonce {}, gas {})'.format(
            kind, btc_txid or '', txhash, nonce, gas_limit))
        return ok(), p

    def _release_nonce(self, nonce):
        if self.nonce_hwm == nonce + 1:
            self.nonce_hwm = nonce           # never used: the next send takes it (else a filler fills it)

    def _resign(self, p, txid, data, gas, outpoints, reply, budget_end):
        """Step 6: a re-signed version of a tracked submit replaces it at the
        same nonce, at >= 1.25x the fees. The caller holds _klock."""
        tip, max_fee = self._bumped(p)
        gas_limit = max(self._gas_limit(gas), p.gas)
        raw, txhash = self._sign(p.nonce, tip, max_fee, gas_limit, self.contract, 0, data)
        if time.monotonic() >= budget_end:
            return fail(NOT_SENT)
        saved = (p.btc_txid, p.data, p.outpoints, p.gas, p.tip, p.max_fee)

        def restore():
            p.btc_txid, p.data, p.outpoints, p.gas, p.tip, p.max_fee = saved
            p.hashes.pop()
            p.hash_txid.pop(txhash, None)
            if had_body is None:
                p.bodies.pop(txid, None)
            else:
                p.bodies[txid] = had_body
        had_body = None
        with self._plock:
            p.btc_txid, p.data, p.outpoints, p.gas, p.tip, p.max_fee = txid, data, outpoints, gas_limit, tip, max_fee
            p.hashes.append(txhash)
            p.hash_txid[txhash] = txid
            had_body = p.bodies.get(txid)
            p.add_body(txid, data, outpoints, gas_limit)
            p.scan_pos = 0
            if not self._save_pending():
                restore()
                return fail('cannot persist the send: not sent')
        if reply is not None and not reply.commit():
            with self._plock:
                restore()
                self._save_pending()
            return fail(NOT_SENT)
        accepted, refused, why = self._push(raw)
        if refused and (reply is None or reply.uncommit()):
            with self._plock:
                restore()
                self._save_pending()
            return fail('eth_sendRawTransaction refused: {}'.format(why))
        with self._plock:
            p.sent.add(txhash)
        if refused:
            # The deadline already answered success: keep the new version and
            # let the tracker re-send it at this nonce.
            self.log('broken', 're-signed {} refused ({}) after its time limit answered success; re-sending '
                     'nonce {}'.format(txid, why, p.nonce))
            p.replace_at = time.monotonic()
            return ok()
        p.replacements += 1
        p.replace_at = time.monotonic() + self.interval
        self.log('info', 're-signed {} replaces {} at nonce {} (EVM tx {})'.format(
            txid, saved[0], p.nonce, txhash))
        return ok()

    def _quick_receipt(self, p):
        """One receipt look-up right after a send (anvil mines at once), so a
        child finds its prevouts without waiting for the tracker (5.2)."""
        try:
            r = self.srpc.call('eth_getTransactionReceipt', p.hashes[-1])
        except evm.EvmError:
            return
        if isinstance(r, dict) and r.get('blockHash'):
            self._finish(p, r)

    # --- step 7: the tracker

    def _tracker_loop(self):
        last, ticks = 0.0, 0
        while not self.stop_event.is_set():
            self.stop_event.wait(self.track_tick())
            with self._plock:
                busy = bool(self.pending)
            # Idle, only the gap check is left: once per poll is enough.
            if not busy and time.monotonic() - last < self.cfg.poll:
                continue
            if self._save_dirty:
                with self._plock:
                    self._save_pending()
            last = time.monotonic()
            ticks += 1
            failed = None
            try:
                self._track(check_gaps=not busy or ticks % 10 == 0)
            except evm.EvmError as e:
                failed = ('unusual', self.scrub(e))
            except Exception as e:           # noqa: B902
                failed = ('broken', type(e).__name__)
            if failed:
                self.log(failed[0], 'tracking sends: {}'.format(failed[1]))

    def _track(self, check_gaps=True):
        latest = self.srpc.nonce(self.address, 'latest')
        with self._klock:
            self.nonce_hwm = max(self.nonce_hwm, latest)   # never below the chain's (H1)
        now = time.monotonic()
        if latest < self.mined_floor:
            # A nonce we saw mined isn't any more: one stale read, or a reorg
            # (one that lasts an INTERVAL). mined_floor would otherwise hide
            # that gap from _fill_gaps for good, and wedge the key.
            if self._behind_since is None:
                self._behind_since = now
            elif now - self._behind_since >= self.interval:
                self._requeue_reorged(latest, now)
        else:
            self._behind_since = None
        with self._plock:
            items = sorted(self.pending.values(), key=lambda q: q.nonce)
        for p in items:
            if p.nonce < latest:
                r = self._scan_receipts(p)
                if r is not None:
                    self._finish(p, r)
                elif p.missing >= 3 and now - p.missing_since >= max(10.0, self.interval):
                    self._drop(p)
                    self.log('unusual', 'nonce {} was mined, but by none of the {} versions we sent of {} {}; '
                             'no longer tracked'.format(p.nonce, len(p.hashes), p.kind, p.btc_txid or ''))
            elif p.replace_at is not None and now >= p.replace_at:
                if p.nonce != latest and not p.requeued:
                    p.replace_at = now + self.interval    # queued behind a lower nonce: can't mine yet
                    continue
                fees = self._bumped(p)                    # outside the key lock (step 8)
                with self._klock:
                    with self._plock:
                        live = self.pending.get(p.nonce) is p
                    if live:
                        self._replace(p, fees)
        if check_gaps or latest < self.nonce_hwm:
            self._fill_gaps(latest)

    def _requeue_reorged(self, latest, now):
        """A reorg took back nonces we had seen mined (the chain's nonce has
        stayed below mined_floor for an INTERVAL, or CLN rebroadcast a tx
        whose mined nonce is no longer below it): those sends are tracked
        again and re-sent at once, lowest nonce first, each as the version
        that was mined, as a mempool would keep them (bitcoind does; CLN
        doesn't rebroadcast a wallet tx), so neither the txs nor the key are
        lost. Nonces we no longer remember are left to _fill_gaps."""
        cur = self.srpc.fees()               # outside the key lock (step 8)
        with self._klock:
            with self._plock:
                back = sorted(n for n in self.recent if n >= latest and n not in self.pending)
                for n in back:
                    p = self.recent.pop(n)
                    p.replace_at, p.missing, p.scan_pos, p.requeued = now, 0, 0, True
                    self.pending[n] = p
                self.mined_floor = latest
                self._behind_since = None
                self._save_pending()
            if not back:
                return
            self.log('unusual', 'a reorg took back nonces {} that were mined: re-sending {}'.format(
                back, ', '.join('{} {}'.format(self.pending[n].kind, self.pending[n].btc_txid or '')
                                for n in back)))
            for n in back:
                with self._plock:
                    p = self.pending.get(n)
                if p is not None and p.requeued:
                    self._replace(p, self._bumped(p, cur))

    def _requeue_if_reorged(self, outpoints):
        """Before a new nonce goes to a tx (5.3 step 4): if a send we saw
        mined spends any of its prevouts and the chain's nonce is no longer
        above that send's, a reorg took it back. It is re-sent at its own
        nonce now (with the other sends the reorg took), instead of a second
        copy at a new nonce, one of which would only revert. True if so."""
        want = set(outpoints)

        def hits(latest):
            with self._plock:
                return [n for n, p in self.recent.items()
                        if n >= latest and p.kind == 'submit' and want & set(p.outpoints)]
        if not hits(0):
            return False
        latest = self.srpc.nonce(self.address, 'latest')
        if not hits(latest):
            return False
        self._requeue_reorged(latest, time.monotonic())
        return True

    def _scan_receipts(self, p, per_tick=3):
        """Receipts of p's versions, newest first, stopping at the first hit;
        at most per_tick look-ups per call (the rest on later ticks)."""
        hashes = list(reversed(p.hashes))
        for _ in range(min(per_tick, len(hashes))):
            h = hashes[p.scan_pos % len(hashes)]
            p.scan_pos += 1
            if p.scan_pos >= len(hashes):
                p.scan_pos = 0
                p.missing += 1
                if p.missing == 1:
                    p.missing_since = time.monotonic()
            try:
                r = self.srpc.call('eth_getTransactionReceipt', h)
            except evm.EvmError:
                return None                  # rate limited or down: try again next tick
            if isinstance(r, dict) and r.get('blockHash'):
                return r
        return None

    def _fill_gaps(self, latest, most=4):
        """Step 7: a 0-value self-send at every untracked nonce between
        latest and pending (or our high-water mark), so the key can't be
        wedged by a tx we don't know about."""
        latest = max(latest, self.mined_floor)          # a nonce just mined is no gap
        top = max(self.srpc.nonce(self.address, 'pending'), self.nonce_hwm)
        if top <= latest:
            return
        with self._plock:
            if not [n for n in range(latest, top) if n not in self.pending]:
                return
        cur_tip, cur_max = self.srpc.fees()               # outside the key lock
        tip = -(-cur_tip * BUMP_NUM // BUMP_DEN) + 1
        max_fee = max(-(-cur_max * BUMP_NUM // BUMP_DEN), tip)
        with self._klock:
            with self._plock:
                gaps = [n for n in range(latest, top) if n not in self.pending][:most]
            for n in gaps:
                raw, txhash = self._sign(n, tip, max_fee, 21_000, self.address, 0, b'')
                p = Pending('filler', None, n, 21_000, self.address, b'', 0, tip, max_fee, txhash)
                p.replace_at = time.monotonic() + self.interval
                with self._plock:
                    self.pending[n] = p
                    if not self._save_pending():
                        del self.pending[n]
                        return
                accepted, refused, why = self._push(raw)
                if refused:
                    with self._plock:
                        self.pending.pop(n, None)
                        self._save_pending()
                    self.log('unusual', 'filler self-send at nonce {} refused ({})'.format(n, why))
                    continue
                self.nonce_hwm = max(self.nonce_hwm, n + 1)
                self.log('unusual', 'nonce {} was not tracked: sent a 0-value self-send there (EVM tx {})'
                         .format(n, txhash))

    def _replace(self, p, fees=None):
        """Step 7: same nonce, 1.25x the fees (at least the network's
        current ones), every INTERVAL while it isn't mined. No cap but the
        key's balance; BROKEN past 10x the first fees. The caller holds
        _klock."""
        now = time.monotonic()
        p.replace_at = now + self.interval
        kind, to, data, value, gas = p.kind, p.to, p.data, p.value, p.gas
        if kind == 'submit' and p.btc_txid and not p.requeued and self.ix.tx(p.btc_txid) is not None:
            # A third party's copy was mined first (SPEC 4.2): ours would only
            # revert. Free the nonce with a 0-value self-send (committed only
            # if the node accepts it).
            kind, to, data, value, gas = 'cancel', self.address, b'', 0, 21_000
        tip, max_fee = fees if fees is not None else self._bumped(p)
        if self.balance is not None and max_fee * gas + value > self.balance:
            # Below 1.25x the node would refuse it anyway: don't send (L3).
            if not p.alarmed:
                p.alarmed = True
                self.log('broken', '{} {} (nonce {}) not mined, and the key cannot pay 1.25x its fees'
                         .format(p.kind, p.btc_txid or '', p.nonce))
            return
        if max_fee > p.first_max_fee * FEE_ALARM_MULTIPLE and not p.alarmed:
            p.alarmed = True
            self.log('broken', '{} {} (nonce {}) still not mined, fees now above {}x the first send\'s'.format(
                p.kind, p.btc_txid or '', p.nonce, FEE_ALARM_MULTIPLE))
        raw, txhash = self._sign(p.nonce, tip, max_fee, gas, to, value, data)
        old = (p.tip, p.max_fee, p.kind, p.to, p.data, p.value, p.gas, p.outpoints, p.requeued)

        def restore():
            p.hashes.pop()
            p.hash_txid.pop(txhash, None)
            p.tip, p.max_fee, p.kind, p.to, p.data, p.value, p.gas, p.outpoints, p.requeued = old
        with self._plock:
            p.requeued = False   # re-sent once as it was mined; a later replacement may cancel
            p.hashes.append(txhash)
            p.hash_txid[txhash] = p.btc_txid if kind == 'submit' else None
            p.tip, p.max_fee, p.kind, p.to, p.data, p.value, p.gas = tip, max_fee, kind, to, data, value, gas
            if kind == 'cancel':
                p.outpoints = []
            p.scan_pos = 0
            if not self._save_pending():     # before it is sent
                restore()
                return
        accepted, refused, why = self._push(raw)
        if refused:
            with self._plock:
                restore()
                self._save_pending()
            self.log('unusual', 'replacing {} {} (nonce {}) refused: {}'.format(
                p.kind, p.btc_txid or '', p.nonce, why))
            return
        with self._plock:
            p.sent.add(txhash)
        if kind == 'cancel' and old[2] == 'submit':
            self.log('info', '{} is in the chain already: cancelled nonce {}'.format(p.btc_txid, p.nonce))
        p.replacements += 1
        self.log('info', 'replaced {} {} (nonce {}) with EVM tx {} at maxFeePerGas {}'.format(
            p.kind, p.btc_txid or '', p.nonce, txhash, max_fee))

    def _sign(self, nonce, tip, max_fee, gas, to, value, data):
        return self.account.sign_tx({
            'chainId': self.ix.chain_id, 'nonce': nonce, 'maxPriorityFeePerGas': tip, 'maxFeePerGas': max_fee,
            'gas': gas, 'to': to, 'value': value, 'data': data})

    def _pending_by_txid(self, txid):
        with self._plock:
            for p in self.pending.values():
                if txid in p.hash_txid.values():
                    return p
        return None

    def _drop(self, p):
        with self._plock:
            if self.pending.get(p.nonce) is p:
                del self.pending[p.nonce]
                self._save_pending()
        self._settle(p)

    def _settle(self, p):
        if not p.done.is_set():
            if p.result is None:
                p.result = fail('no longer tracked')
            p.done.set()

    def _finish(self, p, receipt):
        """A tracked nonce was mined: ingest its receipt (5.2)."""
        try:
            status = int(receipt.get('status', '0x0'), 16)
        except (TypeError, ValueError):
            status = 0
        which = p.hash_txid.get(receipt.get('transactionHash'), p.btc_txid)
        with self._plock:
            self._as_mined(p, receipt.get('transactionHash'))     # its receipt is that version's
        if status == 1:
            if p.to == self.contract:
                self.ix.add_receipt(receipt)
            res = ok()
        else:
            res = fail('reverted on chain (EVM tx {})'.format(receipt.get('transactionHash')))
            if p.kind == 'submit':
                level, why = 'broken', 'CLN will rebroadcast it'
                if self._inputs_spent_elsewhere(p):
                    # The peer (or anyone, SPEC 4.2) got the same or a
                    # conflicting tx mined first: lost gas, nothing else.
                    level, why = 'unusual', 'its inputs were spent by another submit first'
                self.log(level, 'submit of {} (nonce {}) reverted on chain; {}'.format(which, p.nonce, why))
        p.result, p.receipt = res, receipt
        self.balance_dirty = True
        with self._plock:
            self.mined_floor = max(self.mined_floor, p.nonce + 1)
            self.recent.pop(p.nonce, None)
            self.recent[p.nonce] = p
            while len(self.recent) > RECENT_KEEP:
                self.recent.popitem(last=False)
            if self.pending.get(p.nonce) is p:
                del self.pending[p.nonce]
            self._save_pending()
        self._settle(p)
        self.log('info' if status == 1 else 'unusual', '{} {} (nonce {}) mined{}'.format(
            p.kind, which or '', p.nonce, '' if status == 1 else ', reverted'))
        return res

    def _as_mined(self, p, txhash):
        """Sets a submit's fields to its version that was mined (txhash): the
        latest one may be a re-signed tx or a cancel that wasn't. The caller
        holds _plock."""
        if p.kind not in ('submit', 'cancel') or txhash not in p.hash_txid:
            return
        t = p.hash_txid[txhash]
        if t is None:                        # the cancel
            p.kind, p.to, p.data, p.value, p.gas, p.outpoints = 'cancel', self.address, b'', 0, 21_000, []
            p.bodies = {}
            return
        body = p.bodies.get(t)
        if body is None:
            return
        p.kind, p.btc_txid, p.to, p.value = 'submit', t, self.contract, 0
        p.data, p.outpoints, p.gas = bytes.fromhex(body['data']), list(body['outpoints']), body['gas']
        p.bodies = {t: body}

    def _inputs_spent_elsewhere(self, p):
        """True if an input of a reverted submit is no longer unspent: in the
        index, or by the contract's isUnspent at latest."""
        for op in p.outpoints:
            txid, vout = op.rsplit(':', 1)
            u = self.ix.utxo(txid, int(vout))
            if u is None:
                return True
            data = evm.calldata('isUnspent(bytes32,uint32,uint64,bytes,uint32)',
                                ['bytes32', 'uint32', 'uint64', 'bytes', 'uint32'],
                                [btc.internal(txid), int(vout), u['amount'], u['script'], u['height']])
            try:
                if not evm.abi_decode(['bool'], self.srpc.eth_call(self.contract, data, 'latest'))[0]:
                    return True
            except (evm.EvmError, ValueError):
                return False
        return False

    # --- deposit and withdraw calls

    def run_on_worker(self, fn, timeout=None):
        """Runs fn(dequeued) on the send worker and returns its result or
        raises its exception. If it times out before it starts, it is
        cancelled (never run later)."""
        box = {}
        done = threading.Event()
        lock = threading.Lock()

        def job(dequeued):
            with lock:
                if box.get('cancelled'):
                    return
                box['started'] = True
            try:
                box['r'] = fn(dequeued)
            except BaseException as e:   # noqa: B902 (handed back to the caller)
                box['e'] = e
            done.set()
        self.jobs.put(job)
        if not done.wait(timeout):
            with lock:
                if not box.get('started'):
                    box['cancelled'] = True
            if box.get('cancelled'):
                raise UserError('the send worker is busy: not sent, try again')
            done.wait()
        if 'e' in box:
            raise box['e']
        return box['r']

    def contract_call(self, kind, data, value=0, wait=None):
        """A call to the contract from the node key (evm-deposit,
        evm-withdraw): checked and sent on the worker, then its receipt
        (ingested by the tracker) waited for here. Returns (Pending, receipt
        or None while pending). Raises UserError with the revert reason, or
        if it reverted on chain."""
        wait = wait if wait is not None else max(60.0, 4.0 * self.interval)

        def run(dequeued):
            gas, why = self._check_call(data, value)
            if why is not None:
                raise UserError(why)
            with self._klock:
                res, p = self._send_new(kind, None, data, value, gas, [], None, dequeued + self.time_limit())
            if p is None:
                raise UserError(res['errmsg'])
            return p
        p = self.run_on_worker(run, timeout=max(30.0, 4.0 * self.interval))
        if not p.done.wait(wait):
            return p, None
        if not p.result['success']:
            raise UserError('{} {}'.format(kind, p.result['errmsg']))
        return p, p.receipt

    # -------------------------------------------------------- polling and alarms (5.2, 5.5)

    def _poll_loop(self):
        while not self.stop_event.is_set():
            self.poll_once()
            self.stop_event.wait(self.cfg.poll)

    def poll_once(self, now=None):
        try:
            res = self.ix.sync()
            if not res.get('skipped'):
                self.sync_error_since = None
                self.last_sync_error = None
        except evm.EvmError as e:
            self._sync_failed(self.scrub(e), now)
        except Exception as e:
            self._sync_failed(type(e).__name__, now)
            self.log('broken', 'index sync: {}'.format(type(e).__name__))
        try:
            self.refresh_balance()
        except Exception as e:
            self.log('unusual', 'balance: {}'.format(self.scrub(e) if isinstance(e, evm.EvmError)
                                                     else type(e).__name__))
        self.check_alarms(now)

    def _sync_failed(self, why, now=None):
        now = time.time() if now is None else now
        if self.sync_error_since is None:
            self.sync_error_since = now
        self.last_sync_error = why
        self.log('unusual', 'index sync failed: {}'.format(why))

    def refresh_balance(self, force=False, now=None):
        """eth_getBalance of the node key, at most every 5 minutes unless a
        send changed it or it is below evm-min-balance."""
        now = time.time() if now is None else now
        low = self.balance is None or self.balance < self.cfg.min_balance_wei
        if not (force or self.balance_dirty or low or now - self.balance_at >= 300):
            return self.balance
        self.balance_dirty = False
        self.balance = int(self.rpc.request('eth_getBalance', self.address, 'latest'), 16)
        self.balance_at = now
        return self.balance

    def margin(self):
        """The smallest margin of SPEC 5.6, in blocks: the to_self_delay we
        demand, and half the HTLC deltas (CLN goes on chain (delta+1)/2
        blocks before expiry). From lightningd's values once checked, else
        the minimums."""
        mins = option_minimums(self.interval, self.chain_id())[0]
        vals = {}
        for k in ('watchtime-blocks', 'cltv-delta', 'cltv-final'):
            v = _config_value(self.cln_configs, k)
            vals[k] = v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else mins[k]
        return min(vals['watchtime-blocks'], (vals['cltv-delta'] + 1) // 2, (vals['cltv-final'] + 1) // 2)

    def lag(self):
        """L of SPEC 3.4: H(latest) - tip, plus CLN's poll time, in blocks.
        Until the option check has read lightningd's poll, the poll 5.6
        demands is assumed (else every start would alarm)."""
        h = self.ix.current_height()
        if h is None:
            return None
        # Heights follow the wall clock (3.1): during an outage the last
        # sync's H(latest) freezes, but the timelocks still run down.
        h = max(h, self.ix.height_at(int(time.time())))
        poll = self.cln_poll if self.options_checked else max(1, self.interval // 2)
        return h - self.ix.tip() + poll / float(self.interval)

    def check_alarms(self, now=None):
        """SPEC 5.5's alarms, at BROKEN level when raised (and every 10
        minutes while they last), info when cleared."""
        now = time.time() if now is None else now
        tb = self.ix.tag_block()
        if tb is not None and (self.tag_seen is None or self.tag_seen[0] != tb[0]):
            self.tag_seen = (tb[0], now)
        lag = self.lag()
        margin = self.margin()
        found = {}
        if lag is not None and lag > margin / 3.0:
            found['lag'] = 'lag L = {:.1f} blocks is above a third of the smallest margin ({} blocks)'.format(
                lag, margin)
        if self.tag_seen is not None and now - self.tag_seen[1] > 10 * self.interval:
            found['tag'] = 'the {} block has not advanced for {:.0f} s (10 x INTERVAL is {} s)'.format(
                self.cfg.confirm, now - self.tag_seen[1], 10 * self.interval)
        if self.balance is not None and self.balance < self.cfg.min_balance_wei:
            found['balance'] = 'ETH balance of {} is {} wei, below evm-min-balance ({} wei): new channels refused' \
                .format(self.address, self.balance, self.cfg.min_balance_wei)
        if self.sync_error_since is not None and now - self.sync_error_since > 5 * self.interval:
            found['sync'] = 'index sync failing for {:.0f} s (last: {})'.format(
                now - self.sync_error_since, self.last_sync_error)
        for name, msg in found.items():
            a = self.alarms.get(name)
            if a is None or now - a['logged'] >= 600:
                self.log('broken', 'alarm: ' + msg)
                self.alarms[name] = {'since': a['since'] if a else now, 'logged': now, 'message': msg}
            else:
                a['message'] = msg
        for name in [n for n in self.alarms if n not in found]:
            del self.alarms[name]
            self.log('info', 'alarm cleared: {}'.format(name))
        return found

    # -------------------------------------------------------- the option check (5.6)

    def _option_check_loop(self):
        while not self.stop_event.is_set():
            try:
                rpc = self.lnrpc()
                configs = rpc.call('listconfigs')['configs']
                info = rpc.call('getinfo')
            except Exception:
                self.stop_event.wait(1)
                continue
            self.apply_option_check(configs, info)
            return

    def chain_id(self):
        return self.ix.chain_id if self.ix is not None else self._chain_id

    def apply_option_check(self, configs, info):
        self.cln_configs = configs
        self.cln_poll = cln_poll_seconds(configs)
        self.option_problems = check_options(configs, info, self.interval, self.chain_id())
        self.options_checked = True
        for p in self.option_problems:
            self.log('broken', 'CLN option below SPEC 5.6: {}; channel opens are refused until restarted '
                     'with correct values'.format(p))
        if not self.option_problems:
            self.log('info', 'CLN options meet SPEC 5.6')

    def open_refusal(self):
        """Why new channels are refused now (5.1, 5.6), or None."""
        if not self.options_checked:
            return 'evmbackend has not checked lightningd\'s options yet'
        if self.option_problems:
            return 'lightningd runs with options below SPEC 5.6: ' + '; '.join(self.option_problems)
        if self.balance is None:
            return 'the node\'s ETH balance is unknown'
        if self.balance < self.cfg.min_balance_wei:
            return 'the node\'s ETH balance is below evm-min-balance'
        return None

    # -------------------------------------------------------- hooks (5.4)

    def peer_connected(self, peer):
        try:
            feats = peer.get('features') or ''
            anchors = any(feature_set(feats, b) for b in ANCHOR_BITS)
        except (AttributeError, TypeError, ValueError):
            anchors = False
        if anchors:
            return {'result': 'continue'}
        self.log('info', 'disconnecting peer {}: no anchors_zero_fee_htlc_tx (feature 22/23)'.format(
            peer.get('id') if isinstance(peer, dict) else '?'))
        return {'result': 'disconnect',
                'error_message': 'this node only opens anchors_zero_fee_htlc_tx channels (feature 22/23)'}

    def _reject_cmd(self, msg):
        return {'return': {'error': {'code': ERR_REFUSED, 'message': 'evmbackend: ' + msg}}}

    def rpc_command(self, request):
        """The rpc_command hook (SPEC 5.4): channel types, leases and assets
        on outbound opens, every PSBT input of a dual-funded open, and opens
        refused while the option check or the balance says so. No UTXO-count
        check (draft 5): step 3 of sendrawtransaction answers that."""
        method = request.get('method')
        if method not in RPC_FILTER:         # lightningd filters too; this is for direct callers
            return {'result': 'continue'}
        if self.cfg.dev_no_outbound_filter:  # tests: the peer's hooks must refuse it
            return {'result': 'continue'}
        p = named_params(method, request.get('params'))
        r = None
        if p is None:
            r = self._reject_cmd('cannot read the parameters of {}'.format(method))
        elif method in OPEN_COMMANDS:
            r = self._outbound_open_problem(method, p)
        elif method in ('openchannel_update', 'openchannel_signed', 'fundchannel_complete'):
            why = self.open_refusal() if self.option_problems else None
            if why:
                r = self._reject_cmd('channel opens are refused: ' + why)
            else:
                psbt = p.get('signed_psbt' if method == 'openchannel_signed' else 'psbt')
                why = psbt_problem(psbt) if psbt else None
                r = self._reject_cmd(why) if why else None
        if r is not None:
            self.log('info', 'refused {}: {}'.format(method, r['return']['error']['message']))
            return r
        return {'result': 'continue'}

    def _outbound_open_problem(self, method, p):
        why = self.open_refusal()
        if why:
            return self._reject_cmd('channel opens are refused: ' + why)
        dests = p.get('destinations') if method == 'multifundchannel' else [p]
        if not isinstance(dests, list):
            return self._reject_cmd('cannot read destinations')
        for d in dests:
            if not isinstance(d, dict):
                return self._reject_cmd('cannot read destinations')
            if d.get('channel_type') is not None:
                why = ctype_problem(d['channel_type'])
                if why:
                    return self._reject_cmd(why)
            if d.get('compact_lease') is not None or parse_sat(d.get('request_amt', 0)) not in (0, None):
                return self._reject_cmd('liquidity-ad leases are not supported: their lease CSV makes '
                                        'outputs the contract cannot spend')
            if d.get('asset') is not None or d.get('fee_reserve') is not None:
                return self._reject_cmd('asset channels are not supported on this chain')
        if method == 'openchannel_init' and p.get('initialpsbt'):
            why = psbt_problem(p['initialpsbt'])
            if why:
                return self._reject_cmd(why)
        return None

    def _open_hook_problem(self, payload, lease):
        why = self.open_refusal()
        if why:
            return 'channel opens are refused: ' + why
        ct = payload.get('channel_type')
        if not isinstance(ct, dict):
            return 'no channel_type: only anchors_zero_fee_htlc_tx channels are supported'
        why = ctype_problem(ct.get('bits'))
        if why:
            return why
        try:
            mah = int(payload.get('max_accepted_htlcs'))
        except (TypeError, ValueError):
            return 'cannot read max_accepted_htlcs'
        if mah > MAX_HTLCS:
            return 'max_accepted_htlcs {} is above {} (the EVM gas cap)'.format(mah, MAX_HTLCS)
        if lease:
            try:
                leased = msat_int(payload.get('requested_lease_msat') or 0)
            except (TypeError, ValueError):
                leased = 1
            if leased or payload.get('lease_blockheight_start'):
                return 'liquidity-ad leases are not supported (their lease CSV makes outputs the contract ' \
                       'cannot spend)'
        return None

    def openchannel(self, oc):
        why = self._open_hook_problem(oc, lease=False)
        if why:
            self.log('info', 'refused an open from {}: {}'.format(oc.get('id'), why))
            return {'result': 'reject', 'error_message': why}
        return {'result': 'continue'}

    def openchannel2(self, oc2):
        why = self._open_hook_problem(oc2, lease=True)
        if why:
            self.log('info', 'refused a dual-funded open from {}: {}'.format(oc2.get('id'), why))
            return {'result': 'reject', 'error_message': why}
        return {'result': 'continue'}

    def openchannel2_psbt(self, payload, hook):
        """openchannel2_changed and openchannel2_sign: 'continue' must hand
        the PSBT back unchanged. A refusal aborts the open first: lightningd
        (upstream dual_open_control.c) crashes on a 'reject' from these two
        hooks while dualopend runs (it frees the hook's payload but leaves its
        destructor on dualopend, which fires when the reject kills dualopend:
        any peer could crash us with a P2WSH input). Once dualopend is gone
        the answer is ignored. May call lightningd: run it off pyln's thread."""
        psbt = payload.get('psbt')
        why = self.open_refusal() if self.option_problems else None
        if why:
            why = 'channel opens are refused: ' + why
        else:
            why = psbt_problem(psbt) if isinstance(psbt, str) else 'no PSBT'
        if not why:
            return {'result': 'continue', 'psbt': psbt}
        cid = payload.get('channel_id')
        self.log('info', '{} for {}: refused: {}'.format(hook, cid, why))
        if self._abort_open(cid) and isinstance(psbt, str):
            return {'result': 'continue', 'psbt': psbt}
        return {'result': 'reject'}

    def _abort_open(self, channel_id):
        """Ends a dual-funded open we accepted (its dualopend exits):
        openchannel_abort, else a forced disconnect of its peer. True if
        lightningd did either."""
        if self.lnrpc is None or not isinstance(channel_id, str):
            return False
        rpc = self.lnrpc()
        try:
            rpc.call('openchannel_abort', {'channel_id': channel_id})
            return True
        except Exception as e:               # noqa: B902
            self.log('unusual', 'openchannel_abort {} failed ({}); disconnecting the peer'.format(
                channel_id, type(e).__name__))
        try:
            peers = [c.get('peer_id') for c in rpc.call('listpeerchannels', {})['channels']
                     if c.get('channel_id') == channel_id]
            if peers:
                rpc.call('disconnect', {'id': peers[0], 'force': True})
                return True
        except Exception as e:               # noqa: B902
            self.log('broken', 'cannot end the refused open {} ({})'.format(channel_id, type(e).__name__))
        return False

    def htlc_accepted(self, onion, htlc, forward_to=None):
        """Fails a forward whose outgoing channel already has MAX_HTLCS
        outgoing HTLCs (may call lightningd: run it off the main thread)."""
        onion = onion or {}
        scid, node = onion.get('short_channel_id'), onion.get('next_node_id')
        if not (forward_to or scid or node):
            return {'result': 'continue'}
        with self._hlock:
            return self._htlc_decide(forward_to, scid, node, (htlc or {}).get('payment_hash'))

    def _htlc_decide(self, forward_to, scid, node, payment_hash):
        """Counts the channel's outgoing HTLCs in listpeerchannels plus the
        forwards approved in the last 10 s that it doesn't show yet, matched
        by payment hash (a multiset difference), so none counts twice."""
        chans = self.lnrpc().call('listpeerchannels', {'id': node} if (node and not forward_to and not scid)
                                  else {})['channels']
        now = time.monotonic()
        for k in list(self._approved):
            self._approved[k] = [(t, h) for t, h in self._approved[k] if now - t < 10]
            if not self._approved[k]:
                del self._approved[k]
        matched = []
        for c in chans:
            alias = c.get('alias') or {}
            if forward_to:
                match = c.get('channel_id') == forward_to
            elif scid:
                match = scid in (c.get('short_channel_id'), alias.get('local'), alias.get('remote'))
            else:
                match = c.get('peer_id') == node and c.get('state') == 'CHANNELD_NORMAL'
            if not match:
                continue
            matched.append(c.get('channel_id'))
            listed = collections.Counter(h.get('payment_hash') for h in c.get('htlcs') or []
                                         if h.get('direction') == 'out')
            recent = collections.Counter(h for _, h in self._approved.get(c.get('channel_id'), []))
            n = sum(listed.values()) + sum((recent - listed).values())
            if n >= MAX_HTLCS:
                self.log('info', 'failing a forward to {}: it has {} outgoing HTLCs already'.format(
                    c.get('short_channel_id') or c.get('channel_id'), n))
                return {'result': 'fail', 'failure_message': '10070000'}   # temporary_channel_failure
        for cid in matched:
            self._approved.setdefault(cid, []).append((now, payment_hash))
        return {'result': 'continue'}

    # -------------------------------------------------------- RPCs (5.5)

    def withdrawable(self):
        data = evm.calldata('withdrawable(address)', ['address'], [self.address])
        return evm.abi_decode(['uint256'], self.rpc.eth_call(self.contract, data))[0]

    def deposit(self, amount, address=None):
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise UserError('amount must be a whole number of sats') from None
        if amount <= 0:
            raise UserError('amount must be at least 1 sat')
        if address is None:
            address = self.lnrpc().call('newaddr', {'addresstype': 'p2tr'})['p2tr']
        try:
            spk = btc.address_script(address, 'bcrt')
        except ValueError:
            raise UserError('not a bcrt segwit address') from None
        data = evm.calldata('deposit(bytes)', ['bytes'], [spk])
        p, receipt = self.contract_call('deposit', data, amount * SAT_WEI)
        out = {'address': address, 'amount_sat': amount, 'evm_tx': p.hashes[-1]}
        if receipt is None:
            out['status'] = 'pending'
            return out
        for log in receipt.get('logs') or []:
            ev = self.ix.parse_log(log)
            if ev is not None:
                out['txid'] = btc.display(ev['txid'])
                out['height'] = ev['height']
        out['status'] = 'mined'
        return out

    def withdraw(self):
        amount = self.withdrawable()
        if amount == 0:
            raise UserError('nothing to withdraw for {}'.format(self.address))
        p, receipt = self.contract_call('withdraw', evm.selector('withdraw()'))
        return {'address': self.address, 'amount_wei': amount, 'evm_tx': p.hashes[-1],
                'status': 'mined' if receipt is not None else 'pending'}

    def exitaddr(self, ethaddress):
        try:
            return {'address': btc.exit_address(evm.address_bytes(ethaddress), 'bcrt'),
                    'ethaddress': evm.checksum_address(ethaddress)}
        except ValueError:
            raise UserError('not a 20-byte hex ETH address') from None

    def status(self):
        out = {'contract': self.contract, 'chain_id': self.ix.chain_id, 'interval': self.interval,
               'confirm': self.cfg.confirm, 'eth_address': self.address}
        try:
            out['balance_wei'] = self.refresh_balance(force=True)
        except evm.EvmError as e:
            out['balance_error'] = self.scrub(e)
        try:
            out['withdrawable_wei'] = self.withdrawable()
        except evm.EvmError as e:
            out['withdrawable_error'] = self.scrub(e)
        tb = self.ix.tag_block()
        out.update({'tip': self.ix.tip(), 'ibd': self.ix.ibd, 'current_height': self.ix.current_height(),
                    'lag': self.lag(), 'smallest_margin': self.margin()})
        if tb is not None:
            out['tag_block'] = tb[0]
            out['tag_timestamp'] = tb[2]
            out['tag_age_s'] = round(time.time() - tb[2], 1)
        if self.tag_seen is not None:
            out['tag_unchanged_s'] = round(time.time() - self.tag_seen[1], 1)
        cp = self.ix.checkpoint()
        if cp is not None:
            out['checkpoint'] = {'evm_block': cp[0], 'count': cp[2]}
        with self._plock:
            out['pending_sends'] = [p.describe() for p in sorted(self.pending.values(), key=lambda x: x.nonce)]
        out['alarms'] = {k: v['message'] for k, v in self.alarms.items()}
        if self.sync_error_since is not None:
            out['sync_error'] = self.last_sync_error
        calls = collections.Counter()
        for r in {id(r): r for r in (self.rpc, self.srpc) if hasattr(r, 'calls')}.values():
            with r._calls_lock:
                calls.update(r.calls)
        out['rpc_calls'] = dict(sorted(calls.items()))
        out['rpc_calls_since'] = self.started_at
        out['options_checked'] = self.options_checked
        out['option_problems'] = list(self.option_problems)
        out['opens_refused'] = self.open_refusal()
        return out


# ------------------------------------------------------------ pyln glue

def main():
    from pyln.client import LightningRpc, Plugin

    plugin = Plugin(dynamic=False)
    st = {}

    plugin.add_option('evm-rpc', None, 'EVM JSON-RPC URL; ${INFURA_API_KEY} is read from the environment')
    plugin.add_option('evm-contract', None, 'address of the UtxoChain contract')
    plugin.add_option('evm-key-file', None, 'file with this node\'s ETH key (default <lightning-dir>/evm-key, '
                      'created 0600)')
    plugin.add_option('evm-confirm', 'safe', 'confirmation tag: safe or latest')
    plugin.add_option('evm-poll', '45', 'seconds between index polls (1 on anvil)')
    plugin.add_option('evm-min-balance', '0.01', 'ETH below which new channels are refused')
    plugin.add_flag_option(DEV_NO_OUTBOUND_FILTER, 'tests only, needs --developer: our own channel opens skip '
                           'the rpc_command checks, so the peer\'s hooks refuse them')
    developer = '--developer' in sys.argv[1:]      # lightningd passes it to plugins it runs with --developer

    def log(level, msg):
        plugin.log(msg, level=level)

    def backend():
        return st['b']

    @plugin.init()
    def init(options, configuration, plugin, **kwargs):
        ldir = configuration['lightning-dir']
        rpc_path = os.path.join(ldir, configuration['rpc-file'])
        secrets = []
        try:
            cfg = Config.from_options(plugin.get_option, ldir, os.environ, log, developer=developer)
            secrets = cfg.secrets
            b = Backend(cfg, log, lnrpc=lambda: LightningRpc(rpc_path))
            b.connect()
        except Exception as e:     # noqa: B902 - lightningd can't run without its backend: like bcli, exit
            why = scrub_text(e, secrets) if isinstance(e, (ConfigError, evm.EvmError, evmindex.EvmIndexError)) \
                else type(e).__name__
            plugin.log('evmbackend cannot start: {}'.format(why), level='broken')
            sys.stdout.flush()
            time.sleep(0.5)
            os._exit(1)
        b.start()
        st['b'] = b

    def chaininfo_fallback():
        try:
            return backend().getchaininfo_fallback()
        except Exception:          # noqa: B902
            return {'chain': 'regtest', 'headercount': 0, 'blockcount': 0, 'ibd': True}

    def guard(name, fn, fallback):
        try:
            return fn()
        except Exception as e:
            log('broken', '{}: internal error ({})'.format(name, type(e).__name__))
            return fallback() if callable(fallback) else fallback

    def in_thread(name, fn, request, fallback):
        """Answers request with fn() from a new thread (fallback on error)."""
        def run():
            try:
                r = fn()
            except Exception as e:
                log('broken', '{}: internal error ({})'.format(name, type(e).__name__))
                r = fallback
            request.set_result(r)
        threading.Thread(target=run, name=name, daemon=True).start()

    def rpc_in_thread(name, fn, request):
        """Our own RPCs: errors go back to the caller as JSON-RPC errors."""
        def run():
            msg, result = None, None
            try:
                result = fn()
            except (UserError, evm.EvmError) as e:     # written by us / method, type and code only
                msg = backend().scrub(e)
            except Exception as e:                     # noqa: B902
                msg = 'internal error ({})'.format(type(e).__name__)
                log('broken', '{}: {}'.format(name, msg))
            # Outside the except blocks (SPEC 5.3 step 10): pyln's
            # set_exception attaches traceback.format_exc(), which is then
            # empty instead of holding str(e) and source lines.
            if msg is None:
                request.set_result(result)
            else:
                from pyln.client.plugin import RpcException
                try:
                    request.set_exception(RpcException(msg))
                except ValueError:
                    pass
        threading.Thread(target=run, name=name, daemon=True).start()

    # --- the five backend methods: never a JSON-RPC error

    @plugin.method('getchaininfo')
    def getchaininfo(plugin, last_height=None, **kwargs):
        return guard('getchaininfo', lambda: backend().getchaininfo(), chaininfo_fallback)

    @plugin.method('getrawblockbyheight')
    def getrawblockbyheight(plugin, height, **kwargs):
        return guard('getrawblockbyheight', lambda: backend().getrawblockbyheight(height),
                     {'blockhash': None, 'block': None})

    @plugin.method('getutxout')
    def getutxout(plugin, txid, vout, **kwargs):
        return guard('getutxout', lambda: backend().getutxout(txid, vout), {'amount': None, 'script': None})

    @plugin.method('estimatefees')
    def estimatefees(plugin, **kwargs):
        return guard('estimatefees', lambda: backend().estimatefees(), Backend.estimatefees(None))

    @plugin.async_method('sendrawtransaction')
    def sendrawtransaction(plugin, request, tx, allowhighfees=False, **kwargs):
        def respond(result):
            request.set_result(result)
        try:
            backend().sendrawtransaction(tx, respond)
        except Exception as e:
            log('broken', 'sendrawtransaction: internal error ({})'.format(type(e).__name__))
            try:
                request.set_result(fail('internal error'))
            except ValueError:
                pass

    # --- hooks (5.4)

    @plugin.hook('peer_connected')
    def on_peer_connected(peer, plugin, **kwargs):
        return guard('peer_connected', lambda: backend().peer_connected(peer),
                     {'result': 'disconnect', 'error_message': 'internal error'})

    def on_rpc_command(plugin, rpc_command, **kwargs):
        refuse = {'return': {'error': {'code': ERR_REFUSED, 'message': 'evmbackend: internal error'}}}
        return guard('rpc_command', lambda: backend().rpc_command(rpc_command), refuse)
    plugin.add_hook('rpc_command', on_rpc_command, filters=sorted(RPC_FILTER))

    # Before funder: a bare 'continue' from a later plugin would wipe the
    # PSBT and amount funder contributed (dual_open_control.c).
    @plugin.hook('openchannel', before=['funder'])
    def on_openchannel(openchannel, plugin, **kwargs):
        return guard('openchannel', lambda: backend().openchannel(openchannel),
                     {'result': 'reject', 'error_message': 'internal error'})

    @plugin.hook('openchannel2', before=['funder'])
    def on_openchannel2(openchannel2, plugin, **kwargs):
        return guard('openchannel2', lambda: backend().openchannel2(openchannel2),
                     {'result': 'reject', 'error_message': 'internal error'})

    # Async: a refusal calls lightningd (openchannel_abort) before answering.
    @plugin.async_hook('openchannel2_changed')
    def on_openchannel2_changed(openchannel2_changed, plugin, request, **kwargs):
        in_thread('openchannel2_changed',
                  lambda: backend().openchannel2_psbt(openchannel2_changed, 'openchannel2_changed'),
                  request, {'result': 'reject'})

    @plugin.async_hook('openchannel2_sign')
    def on_openchannel2_sign(openchannel2_sign, plugin, request, **kwargs):
        in_thread('openchannel2_sign',
                  lambda: backend().openchannel2_psbt(openchannel2_sign, 'openchannel2_sign'),
                  request, {'result': 'reject'})

    @plugin.async_hook('htlc_accepted')
    def on_htlc_accepted(plugin, onion, htlc, request, forward_to=None, **kwargs):
        if not (forward_to or (onion or {}).get('short_channel_id') or (onion or {}).get('next_node_id')):
            request.set_result({'result': 'continue'})
            return
        in_thread('htlc_accepted', lambda: backend().htlc_accepted(onion, htlc, forward_to), request,
                  {'result': 'fail', 'failure_message': '2002'})

    # --- RPCs (5.5)

    @plugin.async_method('evm-deposit')
    def evm_deposit(plugin, request, amount, address=None, **kwargs):
        """Deposits {amount} sats from the node's ETH key to {address} (default: a new p2tr address)."""
        rpc_in_thread('evm-deposit', lambda: backend().deposit(amount, address), request)

    @plugin.async_method('evm-exitaddr')
    def evm_exitaddr(plugin, request, ethaddress, **kwargs):
        """The bcrt1s... address that `withdraw` pays out to {ethaddress}."""
        rpc_in_thread('evm-exitaddr', lambda: backend().exitaddr(ethaddress), request)

    @plugin.async_method('evm-withdraw')
    def evm_withdraw(plugin, request, **kwargs):
        """Withdraws the node key's withdrawable ETH (fees and exits) from the contract."""
        rpc_in_thread('evm-withdraw', lambda: backend().withdraw(), request)

    @plugin.async_method('evm-status')
    def evm_status(plugin, request, **kwargs):
        """Contract, chain, key, balances, tip, lag, pending sends, alarms."""
        rpc_in_thread('evm-status', lambda: backend().status(), request)

    plugin.run()


if __name__ == '__main__':
    main()
