# SPDX-License-Identifier: MIT
"""An in-memory EVM node for the index tests: blocks with timestamps and
TxApplied logs, tags (safe = latest - 1, as anvil --slots-in-an-epoch 1),
reorgs at will, and an optional cap on eth_getLogs results that fails the way
Infura does."""
import copy
import os

import btc
import evm
import evmindex

CONTRACT = '0x00000000000000000000000000000000000C0FFE'


class FakeChain(evm.EvmRpc):
    def __init__(self, genesis_time=1793949807, interval=2, deploy_block=5, max_logs=None):
        super().__init__('http://fake.invalid')
        self.G, self.I, self.deploy_block, self.max_logs = genesis_time, interval, deploy_block, max_logs
        self.blocks = []        # [{'number', 'hash', 'timestamp', 'logs': [log dicts]}]
        self.calls = []         # (method, params)
        self.nonce = 0
        self.hide = 0           # report `latest` (and the tags) this many blocks back: a lagging backend
        for n in range(deploy_block + 1):
            self.add_block(genesis_time - (deploy_block - n))

    # --- building the chain

    def _hash(self):
        return '0x' + os.urandom(32).hex()

    def add_block(self, timestamp, txs=()):
        """Appends a block; txs are raw txs (or (raw, txid bytes) to fake the
        event's txid), all applied at H(timestamp)."""
        n = len(self.blocks)
        assert not self.blocks or timestamp >= self.blocks[-1]['timestamp']
        b = {'number': n, 'hash': self._hash(), 'timestamp': timestamp, 'logs': []}
        h = (timestamp - self.G) // self.I
        for i, t in enumerate(txs):
            raw, txid = t if isinstance(t, tuple) else (t, btc.txid_of(t))
            b['logs'].append(self.make_log(b, i, h, txid, raw))
        self.blocks.append(b)
        return b

    @staticmethod
    def make_log(b, i, h, txid, raw):
        return {'address': CONTRACT.lower(), 'blockNumber': evm.qty(b['number']), 'blockHash': b['hash'],
                'logIndex': evm.qty(i), 'removed': False,
                'topics': [evmindex.TOPIC0, '0x' + h.to_bytes(32, 'big').hex(), '0x' + bytes(txid).hex()],
                'data': '0x' + evm.abi_encode(['bytes'], [raw]).hex()}

    def deposit(self, amount=1000, spk=None):
        """A fresh deposit tx (raw)."""
        self.nonce += 1
        spk = spk if spk is not None else b'\x00\x14' + self.nonce.to_bytes(20, 'big')
        return btc.deposit_tx_for(31337, CONTRACT, self.nonce, amount, spk).serialize()

    def clone(self):
        """A plain FakeChain with the same blocks (another backend, or a fork)."""
        c = FakeChain(self.G, self.I, self.deploy_block)
        c.blocks = copy.deepcopy(self.blocks)
        c.nonce = self.nonce
        return c

    def T(self, h):
        return self.G + h * self.I

    def reorg(self, number):
        """Drops blocks >= number (the caller appends replacements)."""
        del self.blocks[number:]

    def receipt(self, block):
        return {'status': '0x1', 'blockNumber': evm.qty(block['number']), 'blockHash': block['hash'],
                'logs': block['logs']}

    # --- JSON-RPC

    def _block(self, tag):
        top = len(self.blocks) - 1 - self.hide
        if tag == 'latest':
            n = top
        elif tag == 'safe':
            n = max(top - 1, 0)
        elif tag == 'finalized':
            n = max(top - 2, 0)
        else:
            n = int(tag, 16)
        if n >= len(self.blocks):
            return None
        b = self.blocks[n]
        return {'number': evm.qty(b['number']), 'hash': b['hash'], 'timestamp': evm.qty(b['timestamp'])}

    def call(self, method, *params):
        self.calls.append((method, params))
        if method == 'eth_chainId':
            return '0x7a69'
        if method == 'eth_getCode':
            return '0x6000' if params[0].lower() == CONTRACT.lower() else '0x'
        if method == 'eth_call':
            sel = evm.unhex(params[0]['data'])
            if sel in (evm.selector('txCount()'), evm.selector('logHash()')):
                count, loghash = self.state_at(params[1])
                if sel == evm.selector('txCount()'):
                    return '0x' + evm.abi_encode(['uint256'], [count]).hex()
                return '0x' + loghash.hex()
            for name, v in (('DEPLOY_BLOCK', self.deploy_block), ('GENESIS_TIME', self.G),
                            ('INTERVAL', self.I)):
                if sel == evm.selector(name + '()'):
                    return '0x' + evm.abi_encode(['uint256'], [v]).hex()
            raise evm.EvmError('execution reverted')
        if method == 'eth_getBlockByNumber':
            return self._block(params[0])
        if method == 'eth_getLogs':
            f = params[0]
            lo, hi = int(f['fromBlock'], 16), int(f['toBlock'], 16)
            if f['address'].lower() != CONTRACT.lower():
                return []
            out = [log for b in self.blocks[lo:hi + 1] for log in b['logs']
                   if not f.get('topics') or log['topics'][0] == f['topics'][0]]
            if self.max_logs is not None and len(out) > self.max_logs:
                raise evm.EvmError('eth_getLogs: query returned more than {} results. Try with this block '
                                   'range [{}, {}].'.format(self.max_logs, f['fromBlock'], f['fromBlock']),
                                   -32005)
            return [dict(log) for log in out]
        raise evm.EvmError('{}: method not found'.format(method), -32601)

    def state_at(self, block):
        """The contract's (txCount, logHash) at a block tag, number or
        EIP-1898 {'blockHash': ...} (unknown hash: anvil's error)."""
        if isinstance(block, dict):
            hit = [b for b in self.blocks if b['hash'] == block['blockHash']]
            if not hit:
                raise evm.EvmError('eth_call: JSON-RPC error -32001', -32001)
            n = hit[0]['number']
        else:
            n = int(self._block(block)['number'], 16)
        count, h = 0, bytes(32)
        for b in self.blocks[:n + 1]:
            for log in b['logs']:
                count += 1
                h = evmindex.log_step(h, int(log['topics'][1], 16), evm.unhex(log['topics'][2]))
        return count, h

    def count(self, method):
        return sum(1 for m, _ in self.calls if m == method)
