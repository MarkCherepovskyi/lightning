# SPDX-License-Identifier: MIT
"""Fixtures for the plugin unit tests: an anvil started with run-anvil.sh (one
per session), a fresh UtxoChain per test, and helpers to move time, deposit,
sign and submit."""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

import coincurve
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
EVMCHAN = os.path.dirname(HERE)
sys.path.insert(0, EVMCHAN)
sys.path.insert(1, os.path.join(EVMCHAN, 'reference'))   # pyref: read-only helpers

import btc      # noqa: E402
import evm      # noqa: E402
import pyref    # noqa: E402

# anvil's default account 0 (the deployer in run-anvil.sh) and 1.
KEY0 = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80'
KEY1 = '0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d'
ARTIFACT = os.path.join(EVMCHAN, 'contracts', 'out', 'UtxoChain.sol', 'UtxoChain.json')
SAT = 10**10
SUBMIT_SIG = 'submit(bytes,(uint64,bytes,uint32)[])'
SUBMIT_TYPES = ['bytes', '(uint64,bytes,uint32)[]']


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Anvil:
    def __init__(self):
        self.port = free_port()
        self.url = 'http://127.0.0.1:{}'.format(self.port)
        self.proc = subprocess.Popen([os.path.join(EVMCHAN, 'run-anvil.sh'), '--port', str(self.port)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     start_new_session=True)
        self.address = None
        self.anvil_pid = None
        try:
            line = self.proc.stdout.readline().strip()
            if not re.fullmatch(r'0x[0-9a-fA-F]{40}', line):
                raise RuntimeError('run-anvil.sh printed {!r}; stderr: {}'.format(line, self._stderr()))
            self.address = evm.checksum_address(line)
            for _ in range(200):     # forge's own stderr lines come first
                m = re.search(r'\(pid (\d+)\)', self.proc.stderr.readline())
                if m:
                    self.anvil_pid = int(m.group(1))
                    break
            self.rpc = evm.EvmRpc(self.url)
        except BaseException:
            self.stop()
            raise

    def _stderr(self):
        try:
            self.proc.terminate()
            return self.proc.communicate(timeout=10)[1]
        except Exception:
            return ''

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(10)
        if self.anvil_pid:   # belt and braces: the script's trap kills anvil
            try:
                os.kill(self.anvil_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture(scope='session')
def anvil():
    a = Anvil()
    try:
        yield a
    finally:
        a.stop()


class Chain:
    """A fresh UtxoChain on the session's anvil, with time under control:
    every block the helpers mine gets an explicit timestamp."""

    def __init__(self, anvil, interval=2):
        self.rpc = anvil.rpc
        self.acct = evm.Account(KEY1)
        with open(ARTIFACT) as f:
            code = evm.unhex(json.load(f)['bytecode']['object'])
        rc = self.rpc.send(self.acct, None, data=code + evm.abi_encode(['uint256'], [interval]),
                           gas=8_000_000)
        self.address = evm.checksum_address(rc['contractAddress'])
        self.deploy_block = int(rc['blockNumber'], 16)
        self.G = self.const('GENESIS_TIME')
        self.I = self.const('INTERVAL')  # noqa: E741
        assert self.const('DEPLOY_BLOCK') == self.deploy_block
        self.chain_id = self.rpc.chain_id()

    def const(self, name):
        return evm.abi_decode(['uint256'], self.rpc.eth_call(self.address, evm.selector(name + '()')))[0]

    def T(self, h):
        return self.G + h * self.I

    def latest(self):
        return self.rpc.block('latest')

    def latest_ts(self):
        return int(self.latest()['timestamp'], 16)

    def at(self, h):
        """Makes the next block's timestamp fall at height h (as early as possible)."""
        ts = max(self.T(h), self.latest_ts() + 1)
        assert ts < self.T(h + 1), 'height {} is already past'.format(h)
        self.rpc.call('evm_setNextBlockTimestamp', evm.qty(ts))
        return ts

    def mine(self, ts=None):
        if ts is not None:
            self.rpc.call('evm_setNextBlockTimestamp', evm.qty(ts))
        self.rpc.call('evm_mine')
        return self.latest()

    def mine_at(self, h):
        self.at(h)
        return self.mine()

    def confirm(self, h):
        """Mines until the `safe` block (latest - 1) is at a height > h, so the
        tip can reach h."""
        self.mine_at(max(h + 1, self.height_now()))
        self.mine(self.latest_ts() + 1)

    def height_now(self):
        return (self.latest_ts() - self.G) // self.I

    def deposit(self, spk, sats, h=None):
        if h is not None:
            self.at(h)
        return self.rpc.send(self.acct, self.address, value=sats * SAT,
                             data=evm.calldata('deposit(bytes)', ['bytes'], [spk]), gas=1_000_000)

    def submit_data(self, raw, prevouts):
        return evm.calldata(SUBMIT_SIG, SUBMIT_TYPES, [raw, [tuple(p) for p in prevouts]])

    def submit(self, raw, prevouts, h=None):
        if h is not None:
            self.at(h)
        return self.rpc.send(self.acct, self.address, data=self.submit_data(raw, prevouts), gas=5_000_000)

    def send_many(self, calls, h):
        """Several contract calls [(value_wei, data)] mined in one EVM block at
        height h; returns their receipts in order."""
        self.rpc.call('evm_setAutomine', False)
        try:
            nonce = self.rpc.nonce(self.acct.address)
            tip, max_fee = self.rpc.fees()
            hashes = []
            for i, (value, data) in enumerate(calls):
                raw, txhash = self.acct.sign_tx({
                    'chainId': self.chain_id, 'nonce': nonce + i, 'maxPriorityFeePerGas': tip,
                    'maxFeePerGas': max_fee, 'gas': 5_000_000, 'to': self.address, 'value': value,
                    'data': data})
                self.rpc.send_raw(raw)
                hashes.append(txhash)
            self.mine_at(h)
        finally:
            self.rpc.call('evm_setAutomine', True)
        receipts = [self.rpc.wait_receipt(x, 10) for x in hashes]
        assert all(int(r['status'], 16) == 1 for r in receipts), receipts
        assert len({r['blockHash'] for r in receipts}) == 1
        return receipts

    def logs(self):
        """Every TxApplied log of this contract, straight from the node."""
        import evmindex
        return self.rpc.get_logs(self.address, [evmindex.TOPIC0], self.deploy_block, 'latest')

    def is_unspent(self, txid, vout, amount, spk, height):
        data = evm.calldata('isUnspent(bytes32,uint32,uint64,bytes,uint32)',
                            ['bytes32', 'uint32', 'uint64', 'bytes', 'uint32'],
                            [txid, vout, amount, spk, height])
        return evm.abi_decode(['bool'], self.rpc.eth_call(self.address, data))[0]

    def withdrawable(self, addr):
        data = evm.calldata('withdrawable(address)', ['address'], [addr])
        return evm.abi_decode(['uint256'], self.rpc.eth_call(self.address, data))[0]


@pytest.fixture
def chain(anvil):
    return Chain(anvil)


# ------------------------------------------------------------ keys and signing

class Key:
    """A secp256k1 key with its P2WPKH and P2TR (key path, output key = this
    key's x-only point, untweaked: the contract verifies against the output
    key directly) scriptPubKeys."""

    def __init__(self, secret):
        self.priv = coincurve.PrivateKey(secret.to_bytes(32, 'big'))
        self.pub = self.priv.public_key.format(True)
        self.h160 = pyref.hash160(self.pub)
        self.xonly = self.priv.public_key_xonly.format()
        self.p2wpkh = b'\x00\x14' + self.h160
        self.p2tr = b'\x51\x20' + self.xonly


def sign(tx, keys, prevouts):
    """Signs every input of a btc.Tx in place: keys[i] spends prevouts[i] =
    (amount, script, height), P2WPKH with SIGHASH_ALL or P2TR with DEFAULT."""
    ptx = pyref.parse_tx(btc.Tx(tx.version, tx.inputs, tx.outputs, tx.locktime).serialize())
    sh = pyref._Sighash(ptx, [(a, s) for a, s, _ in prevouts])
    for i, (key, (amount, spk, _)) in enumerate(zip(keys, prevouts)):
        if spk == key.p2wpkh:
            code = b'\x76\xa9\x14' + key.h160 + b'\x88\xac'
            sig = key.priv.sign(sh.bip143(i, code, amount, 1), hasher=None) + b'\x01'
            tx.witnesses[i] = [sig, key.pub]
        elif spk == key.p2tr:
            tx.witnesses[i] = [key.priv.sign_schnorr(sh.bip341(i, 0), bytes(32))]
        else:
            raise ValueError('key does not match input {}'.format(i))
    return tx


def check_valid(chain, raw, prevouts, h):
    ok, err, _, fee = pyref.validate(raw, prevouts, h, chain.G, chain.I)
    assert ok, err
    return fee


def wait_until(pred, timeout=10):
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise TimeoutError
        time.sleep(0.05)
