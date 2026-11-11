# SPDX-License-Identifier: MIT
"""evmbackend.py in token mode (SPEC 4.6), against anvil: a UtxoChainToken
on a mock ERC-20 (6 and 18 decimals, and a USDT-style token that returns no
bool). The plugin reads TOKEN() and UNITS_PER_SAT() at connect; evm-deposit
approves and then deposits token units; exits and fees are withdrawn in
token units; gas stays ETH."""
import json
import os
import threading
import time

import pytest

import btc
import evm
import evmbackend as eb
from conftest import CONTRACTS, EVMCHAN, KEY1, Chain, Key
from test_backend import (A, GOOD_CONFIGS, GOOD_INFO, CountingRpc, FakeLn, Log, StatusIx, make_cfg,
                          offline_backend, spend)

OUT = os.path.join(CONTRACTS, 'out')


def artifact(sol, name):
    with open(os.path.join(OUT, sol, name + '.json')) as f:
        return evm.unhex(json.load(f)['bytecode']['object'])


class TokenChain(Chain):
    """A fresh mock token and a UtxoChainToken on it (instead of Chain's
    UtxoChain), with the same helpers."""

    def __init__(self, anvil, kind='MockERC20', decimals=6, units=1, interval=2):
        self.rpc = anvil.rpc
        self.acct = evm.Account(KEY1)
        code = artifact('Tokens.sol', kind)
        if kind == 'MockERC20':
            code += evm.abi_encode(['uint8'], [decimals])
        rc = self.rpc.send(self.acct, None, data=code, gas=3_000_000)
        self.token = evm.checksum_address(rc['contractAddress'])
        code = artifact('UtxoChainToken.sol', 'UtxoChainToken') + evm.abi_encode(
            ['uint256', 'address', 'uint256'], [interval, self.token, units])
        rc = self.rpc.send(self.acct, None, data=code, gas=8_000_000)
        self.address = evm.checksum_address(rc['contractAddress'])
        self.deploy_block = int(rc['blockNumber'], 16)
        self.units = units
        self.G = self.const('GENESIS_TIME')
        self.I = self.const('INTERVAL')  # noqa: E741
        self.chain_id = self.rpc.chain_id()

    def tcall(self, sig, types, values):
        return evm.abi_decode(['uint256'], self.rpc.eth_call(self.token, evm.calldata(sig, types, values)))[0]

    def balance(self, who):
        return self.tcall('balanceOf(address)', ['address'], [who])

    def allowance(self, owner):
        return self.tcall('allowance(address,address)', ['address', 'address'], [owner, self.address])

    def mint(self, to, units):
        data = evm.calldata('mint(address,uint256)', ['address', 'uint256'], [to, units])
        self.rpc.send(self.acct, self.token, data=data, gas=200_000)

    def approve_from(self, account, units):
        data = evm.calldata('approve(address,uint256)', ['address', 'uint256'], [self.address, units])
        self.rpc.send(account, self.token, data=data, gas=200_000)

    def deposit(self, spk, sats, h=None):
        """From KEY1: mint, approve and deposit `sats` (in units)."""
        units = sats * self.units
        self.mint(self.acct.address, units)
        self.approve_from(self.acct, units)
        if h is not None:
            self.at(h)
        return self.rpc.send(self.acct, self.address, data=evm.calldata(
            'deposit(uint256,bytes)', ['uint256', 'bytes'], [units, spk]), gas=1_000_000)


TOKENS = [('MockERC20', 6, 1), ('MockERC20', 18, 10**10), ('NoBoolToken', 6, 1)]


@pytest.fixture(params=TOKENS, ids=['6-decimals', '18-decimals', 'no-bool'])
def tchain(anvil, request):
    kind, decimals, units = request.param
    return TokenChain(anvil, kind, decimals, units)


def start(chain, tmp_path):
    log = Log()
    b = eb.Backend(make_cfg(tmp_path, chain.rpc.url, chain.address), log)
    chain.rpc.call('anvil_setBalance', b.address, evm.qty(10**20))
    chain.mine_at(1)
    b.connect(retry_for=0)
    b.apply_option_check(GOOD_CONFIGS, GOOD_INFO)
    ts = [threading.Thread(target=f, daemon=True) for f in (b._worker_loop, b._tracker_loop)]
    for t in ts:
        t.start()
    b.log_capture = log
    b.test_threads = ts
    return b


def stop(b):
    b.stop()
    for t in b.test_threads:
        t.join(5)
    b.ix.close()


@pytest.fixture
def tbackend(tchain, tmp_path):
    b = start(tchain, tmp_path)
    yield b
    stop(b)


def test_eth_mode_read_at_connect(chain, tmp_path):
    b = start(chain, tmp_path)
    try:
        assert b.token is None and b.units_per_sat == eb.SAT_WEI
        s = b.status()
        assert 'token' not in s and 'withdrawable_wei' in s and 'withdrawable_units' not in s
    finally:
        stop(b)


def test_token_mode_read_at_connect(tchain, tbackend):
    assert tbackend.token == tchain.token
    assert tbackend.units_per_sat == tchain.units
    assert any('token mode' in m for m in tbackend.log_capture.at('info'))


def eth_balance(chain, who):
    return int(chain.rpc.request('eth_getBalance', who, 'latest'), 16)


def gas_paid(chain, txhash):
    rc = chain.rpc.request('eth_getTransactionReceipt', txhash)
    return int(rc['gasUsed'], 16) * int(rc['effectiveGasPrice'], 16)


def test_token_deposit_withdraw_status(tchain, tbackend):
    b, u = tbackend, tchain.units
    ln = FakeLn({'newaddr': {'p2tr': btc.segwit_address('bcrt', 1, A.xonly)}})
    b.lnrpc = lambda: ln
    with pytest.raises(eb.UserError, match='holds 0 token units'):
        b.deposit(50_000)
    tchain.mint(b.address, 200_000 * u)
    # A small allowance left over: reset to 0, then set (as USDT requires).
    tchain.approve_from(b.account, 7)
    eth0 = eth_balance(tchain, b.address)
    nonce0 = tchain.rpc.nonce(b.address)
    d = b.deposit(50_000)
    assert d['status'] == 'mined' and d['amount_sat'] == 50_000 and d['amount_units'] == 50_000 * u
    assert d['token'] == tchain.token
    assert tchain.rpc.nonce(b.address) == nonce0 + 3            # approve(0), approve(units), deposit
    assert tchain.balance(b.address) == 150_000 * u
    assert tchain.balance(tchain.address) == 50_000 * u
    assert tchain.allowance(b.address) == 0
    assert b.ix.utxo(d['txid'], 0) == {'amount': 50_000, 'script': A.p2tr, 'height': d['height']}
    assert tchain.is_unspent(btc.internal(d['txid']), 0, 50_000, A.p2tr, d['height'])
    assert eth0 - eth_balance(tchain, b.address) > 0             # gas only, in ETH
    # No allowance left: one approve, then the deposit.
    nonce0 = tchain.rpc.nonce(b.address)
    d2 = b.deposit(1, btc.segwit_address('bcrt', 0, A.h160))
    assert d2['status'] == 'mined' and tchain.rpc.nonce(b.address) == nonce0 + 2
    with pytest.raises(eb.UserError, match='BadDepositScript'):
        b.deposit(1000, b.exitaddr(b.address)['address'])
    with pytest.raises(eb.UserError, match='nothing to withdraw'):
        b.withdraw()

    # A deposit (by someone else) to A's P2WPKH pays an exit to the node
    # key, which withdraws it.
    tchain.deposit(A.p2wpkh, 100_000)
    b.ix.sync()
    dep = evm.unhex(tchain.logs()[-1]['topics'][2])
    tx = spend(b, dep, 0, 100_000, out_script=btc.exit_script(b.address), fee=500)
    assert b.send_now(tx.serialize().hex())['success']
    assert b.withdrawable() == 100_000 * u                      # 99,500 exit + 500 fee to the submitter
    tok0, eth0 = tchain.balance(b.address), eth_balance(tchain, b.address)
    held0 = tchain.balance(tchain.address)
    w = b.withdraw()
    assert w['status'] == 'mined' and w['amount_units'] == 100_000 * u and w['token'] == tchain.token
    assert tchain.balance(b.address) - tok0 == 100_000 * u
    assert held0 - tchain.balance(tchain.address) == 100_000 * u
    assert eth0 - eth_balance(tchain, b.address) == gas_paid(tchain, w['evm_tx'])
    assert b.withdrawable() == 0

    s = b.status()
    assert s['token'] == tchain.token and s['units_per_sat'] == u
    assert s['token_balance_units'] == tchain.balance(b.address)
    assert s['withdrawable_units'] == 0 and 'withdrawable_wei' not in s
    assert s['balance_wei'] == eth_balance(tchain, b.address)
    assert s['pending_sends'] == [] and s['opens_refused'] is None


def test_token_deposit_value_rules(tchain, tbackend):
    """Sats become units exactly: the smallest deposit is 1 sat = UNITS_PER_SAT units."""
    b, u = tbackend, tchain.units
    tchain.mint(b.address, 10 * u)
    addr = btc.segwit_address('bcrt', 1, Key(0xB0B).xonly)
    with pytest.raises(eb.UserError, match='holds {} token units, below {}'.format(10 * u, 11 * u)):
        b.deposit(11, addr)
    d = b.deposit(10, addr)
    assert d['amount_units'] == 10 * u and tchain.balance(b.address) == 0


class TokenCountingRpc(CountingRpc):
    """CountingRpc with the token's balanceOf apart: eth_calls to the token
    are counted as 'balanceOf', the rest (withdrawable) as 'eth_call'."""

    def __init__(self, token):
        super().__init__()
        self.token, self.tokens = token, 200 * 10**6

    def call(self, method, *params):
        if method == 'eth_call' and params[0].get('to') == self.token:
            with self._calls_lock:
                self.calls['balanceOf'] += 1
            return '0x' + evm.abi_encode(['uint256'], [self.tokens]).hex()
        return super().call(method, *params)


def test_status_cached_token_balance(tmp_path):
    """Token mode, evm-status cached=true: the token balance, like
    withdrawable, is read at most once per evm-poll or after a change
    (cached_token_balance), never on every call."""
    b = offline_backend(tmp_path)
    b.cfg.poll = 60.0
    b.token, b.units_per_sat = '0x' + 'ab' * 20, 1
    b.ix, b.rpc = StatusIx(), TokenCountingRpc(b.token)
    b.balance, b.balance_at = 10**18, time.time()

    def reads():
        return b.rpc.calls['balanceOf'], b.rpc.calls['eth_call']
    s = b.status()
    assert reads() == (1, 1) and s['token_balance_units'] == 200 * 10**6 and s['withdrawable_units'] == 5 * 10**10
    b.status()
    assert reads() == (2, 2), 'uncached: fresh every time'
    for _ in range(10):
        s = b.status(cached=True)
    assert reads() == (2, 2), 'cached: the values just read'
    assert s['cached'] is True and s['token_balance_units'] == 200 * 10**6 and s['token'] == b.token
    # Older than evm-poll: one read of the token balance (withdrawable is still fresh).
    b.token_balance_cache = (b.token_balance_cache[0], time.time() - 61)
    b.rpc.tokens = 150 * 10**6
    assert b.status(cached=True)['token_balance_units'] == 150 * 10**6
    b.status(cached=True)
    assert reads() == (3, 2)
    # A send, a receipt or new logs mark it dirty: both read once more, then cached.
    b.withdrawable_dirty = True
    b.rpc.tokens = 100 * 10**6
    assert b.status(cached=True)['token_balance_units'] == 100 * 10**6
    b.status(cached=True)
    assert reads() == (4, 3)
    # A failed read is not cached: the next call reads again.
    b.token_balance_cache = (b.token_balance_cache[0], time.time() - 61)
    real = b.rpc.call

    def failing(method, *params):
        if method == 'eth_call' and params[0].get('to') == b.token:
            raise evm.EvmError('eth_call: JSON-RPC error -32000', -32000)
        return real(method, *params)
    b.rpc.call = failing
    s = b.status(cached=True)
    assert 'token_balance_error' in s and 'token_balance_units' not in s
    b.rpc.call = real
    assert b.status(cached=True)['token_balance_units'] == 100 * 10**6 and reads() == (5, 3)
    before = sum(b.rpc.calls.values())
    for _ in range(10):
        b.status(cached=True)
    assert sum(b.rpc.calls.values()) == before
