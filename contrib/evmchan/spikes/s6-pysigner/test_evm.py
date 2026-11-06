# SPDX-License-Identifier: MIT
"""Tests for evm.py: offline vectors, cross-checks against Foundry's cast, and a
live anvil started on a free port. Run: python -m unittest -v test_evm"""
import hashlib
import json
import os
import random
import shutil
import socket
import subprocess
import time
import unittest

import evm

FOUNDRY = os.path.expanduser('~/.foundry/bin')
CAST = shutil.which('cast') or os.path.join(FOUNDRY, 'cast')
ANVIL = shutil.which('anvil') or os.path.join(FOUNDRY, 'anvil')
# anvil's default accounts 0 and 1 (the well-known test mnemonic).
KEY0 = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80'
KEY1 = '0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d'
ADDR0 = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'
ADDR1 = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
EVENT = 'Tx(address,uint256,bytes)'
SUBMIT = 'submit(bytes,bytes,bytes)'


def cast(*args):
    return subprocess.run([CAST, *args], check=True, capture_output=True, text=True).stdout.strip()


def contract_initcode():
    """Hand-assembled contract. With value: emit Tx(msg.sender indexed, msg.value
    indexed, msg.data), i.e. LOG3 over abi.encode(bytes(calldata)). Without value:
    return the calldata (an echo, for eth_call)."""
    topic0 = evm.keccak256(EVENT.encode()).hex()
    emit = bytes.fromhex(
        '3433' '7f' + topic0 +              # CALLVALUE CALLER PUSH32 topic0
        '6020600052'                        # MSTORE(0, 0x20)
        '36602052'                          # MSTORE(32, calldatasize)
        '366000604037'                      # CALLDATACOPY(0x40, 0, calldatasize)
        '601f3601601f19166040016000'        # size = 0x40 + roundup32(cds), offset = 0
        'a300')                             # LOG3 STOP
    echo = bytes.fromhex('5b' '3660008037' '366000f3')  # JUMPDEST; CALLDATACOPY(0,0,cds); RETURN(0,cds)
    runtime = bytes.fromhex('341560{:02x}57'.format(5 + len(emit))) + emit + echo  # CALLVALUE ISZERO JUMPI
    init = bytes.fromhex('60{0:02x}600c60003960{0:02x}6000f3'.format(len(runtime)))  # CODECOPY, RETURN
    assert len(init) == 12 and runtime[5 + len(emit)] == 0x5b
    return init + runtime


class Offline(unittest.TestCase):
    def test_keccak_vectors(self):
        def k(b):
            return evm.keccak256(b).hex()
        self.assertEqual(k(b''), 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470')
        self.assertEqual(k(b'abc'), '4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45')
        self.assertEqual(k(b'abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq'),
                         '45d3b367a6904e6e8d502ee04999a7c27647f91fa845d456525fd352ae3d7371')
        self.assertEqual(k(b'a' * 1000000),
                         'fadae6b49f129bbb812be8407b7b2894f34aecf6dbd1f9b0f0c7e9853098fc96')
        self.assertEqual(k(b'transfer(address,uint256)')[:8], 'a9059cbb')
        # Not SHA3-256: same permutation, different padding byte.
        self.assertNotEqual(k(b''), hashlib.sha3_256(b'').hexdigest())

    def test_keccak_against_cast(self):
        rng = random.Random(6)
        lens = [1, 31, 32, 135, 136, 137, 271, 272, 273, 1000] + [rng.randrange(600) for _ in range(5)]
        for n in lens:
            data = bytes(rng.randrange(256) for _ in range(n))
            self.assertEqual('0x' + evm.keccak256(data).hex(), cast('keccak', '0x' + data.hex()), n)

    def test_rlp_vectors(self):
        def r(x):
            return evm.rlp(x).hex()
        self.assertEqual(r(b'dog'), '83646f67')
        self.assertEqual(r([b'cat', b'dog']), 'c88363617483646f67')
        self.assertEqual(r(b''), '80')
        self.assertEqual(r([]), 'c0')
        self.assertEqual(r(0), '80')
        self.assertEqual(r(b'\x00'), '00')
        self.assertEqual(r(15), '0f')
        self.assertEqual(r(1024), '820400')
        self.assertEqual(r([[], [[]], [[], [[]]]]), 'c7c0c1c0c3c0c1c0')
        lorem = b'Lorem ipsum dolor sit amet, consectetur adipisicing elit'
        self.assertEqual(r(lorem), 'b838' + lorem.hex())
        self.assertEqual(r([b'x' * 60]), 'f83e' + 'b83c' + '78' * 60)

    def test_address_and_create(self):
        self.assertEqual(evm.Account(KEY0).address, ADDR0)
        self.assertEqual(evm.Account(KEY1).address, ADDR1)
        self.assertEqual(evm.Account(KEY1).address, cast('wallet', 'address', '--private-key', KEY1))
        self.assertEqual(evm.create_address(ADDR0, 5),
                         cast('compute-address', ADDR0, '--nonce', '5').split()[-1])

    def test_abi_against_cast(self):
        a, b, c = b'\x01\x02', bytes(range(70)), b''
        self.assertEqual('0x' + evm.calldata(SUBMIT, ['bytes'] * 3, [a, b, c]).hex(),
                         cast('calldata', SUBMIT, '0x' + a.hex(), '0x' + b.hex(), '0x'))
        sig = 'f(uint256,bytes32,address,bytes[],bytes)'
        vals = [2**200 + 5, b'\xab' * 32, ADDR1, [b'', b'\xff' * 33, b'z'], b'tail']
        types = ['uint256', 'bytes32', 'address', 'bytes[]', 'bytes']
        data = evm.calldata(sig, types, vals)
        theirs = cast('calldata', sig, str(vals[0]), '0x' + 'ab' * 32, ADDR1,
                      '[0x,0x' + 'ff' * 33 + ',0x7a]', '0x' + b'tail'.hex())
        self.assertEqual('0x' + data.hex(), theirs)
        self.assertEqual(evm.abi_decode(types, data[4:]), vals)
        with self.assertRaises(ValueError):
            evm.abi_decode(['bytes'], data[4:40])

    def test_sign_against_cast(self):
        tx = {'chainId': 8453, 'nonce': 7, 'maxPriorityFeePerGas': 1000, 'maxFeePerGas': 3 * 10**9,
              'gas': 100000, 'to': ADDR1, 'value': 12345, 'data': b'\xde\xad\xbe\xef'}
        raw, txhash = evm.Account(KEY0).sign_tx(tx)
        theirs = cast('mktx', ADDR1, '0xdeadbeef', '--private-key', KEY0, '--chain', '8453',
                      '--nonce', '7', '--priority-gas-price', '1000', '--gas-price', str(3 * 10**9),
                      '--gas-limit', '100000', '--value', '12345')
        self.assertEqual('0x' + raw.hex(), theirs)  # RFC6979: deterministic, so byte-identical
        dec = json.loads(cast('decode-transaction', '0x' + raw.hex(), '--json'))['data']
        self.assertEqual(dec['signer'].lower(), ADDR0.lower())
        self.assertEqual(dec['hash'], txhash)


class Anvil(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        cls.proc = subprocess.Popen([ANVIL, '--port', str(port), '--silent'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.rpc = evm.EvmRpc('http://127.0.0.1:{}'.format(port))
        for _ in range(100):
            try:
                cls.rpc.chain_id()
                break
            except evm.EvmError:
                time.sleep(0.05)
        else:
            raise RuntimeError('anvil did not start')
        cls.a0, cls.a1 = evm.Account(KEY0), evm.Account(KEY1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)

    def balance(self, addr):
        return int(self.rpc.call('eth_getBalance', addr, 'latest'), 16)

    def test_chain_and_fees(self):
        self.assertEqual(self.rpc.chain_id(), 31337)
        tip, max_fee = self.rpc.fees()
        self.assertGreaterEqual(max_fee, tip)

    def test_send_eth(self):
        before0, before1 = self.balance(ADDR0), self.balance(ADDR1)
        t = time.monotonic()
        rc = self.rpc.send(self.a0, ADDR1, value=10**18)
        print('\n  send_eth round trip: {:.1f} ms'.format(1000 * (time.monotonic() - t)))
        self.assertEqual(int(rc['gasUsed'], 16), 21000)
        self.assertEqual(rc['type'], '0x2')
        fee = int(rc['gasUsed'], 16) * int(rc['effectiveGasPrice'], 16)
        self.assertEqual(self.balance(ADDR1), before1 + 10**18)
        self.assertEqual(self.balance(ADDR0), before0 - 10**18 - fee)
        # And back, from account 1.
        rc = self.rpc.send(self.a1, ADDR0, value=10**17)
        fee = int(rc['gasUsed'], 16) * int(rc['effectiveGasPrice'], 16)
        self.assertEqual(self.balance(ADDR1), before1 + 9 * 10**17 - fee)

    def test_contract_event(self):
        rc = self.rpc.send(self.a0, None, data=contract_initcode())
        addr = evm.checksum_address(rc['contractAddress'])
        self.assertEqual(len(evm.unhex(self.rpc.call('eth_getCode', addr, 'latest'))), 80)
        args = [b'\x02\x00' + bytes(range(200)), b'prevouts' * 5, b'']
        data = evm.calldata(SUBMIT, ['bytes'] * 3, args)
        # eth_call without value: the echo path returns the calldata.
        self.assertEqual(self.rpc.eth_call(addr, data), data)
        t = time.monotonic()
        rc = self.rpc.send(self.a1, addr, value=777, data=data)
        print('\n  contract call round trip: {:.1f} ms, gas {}'.format(
            1000 * (time.monotonic() - t), int(rc['gasUsed'], 16)))
        height = int(rc['blockNumber'], 16)
        topic0 = '0x' + evm.keccak256(EVENT.encode()).hex()
        logs = self.rpc.get_logs(addr, [topic0], height, height)
        self.assertEqual(len(logs), 1)
        (sender, value), (payload,) = evm.decode_log(logs[0], EVENT, ['address', 'uint256'], ['bytes'])
        self.assertEqual((sender, value, payload), (ADDR1, 777, data))
        self.assertEqual(payload[:4], evm.selector(SUBMIT))
        self.assertEqual(evm.abi_decode(['bytes'] * 3, payload[4:]), args)
        # Filtering by an indexed topic.
        topic1 = '0x' + bytes(12).hex() + evm.address_bytes(ADDR0).hex()
        self.assertEqual(self.rpc.get_logs(addr, [topic0, topic1]), [])
        with self.assertRaises(ValueError):
            evm.decode_log(logs[0], 'Other(bytes)', [], ['bytes'])

    def test_block_tags_and_time(self):
        latest = self.rpc.block('latest')
        ts = int(latest['timestamp'], 16) + 1000
        for i in range(3):
            self.rpc.call('evm_setNextBlockTimestamp', evm.qty(ts + 60 * i))
            self.rpc.call('evm_mine')
        self.assertEqual(int(self.rpc.block('latest')['timestamp'], 16), ts + 120)
        with self.assertRaises(evm.EvmError):  # time cannot go backwards (equal is accepted)
            self.rpc.call('evm_setNextBlockTimestamp', evm.qty(ts))
        tags = {t: int(self.rpc.block(t)['number'], 16) for t in ('latest', 'safe', 'finalized', 'earliest')}
        print('\n  anvil block tags:', tags)
        self.assertEqual(tags['earliest'], 0)
        # anvil: safe = latest - 32, finalized = latest - 64 (slots-in-an-epoch), floored at 0.
        self.rpc.call('anvil_mine', evm.qty(100))
        self.assertEqual(int(self.rpc.block('safe')['number'], 16), tags['latest'] + 100 - 32)
        self.assertEqual(int(self.rpc.block('finalized')['number'], 16), tags['latest'] + 100 - 64)
        tags = {t: int(self.rpc.block(t)['number'], 16) for t in ('latest', 'safe', 'finalized')}
        self.assertLessEqual(tags['finalized'], tags['safe'])
        self.assertLessEqual(tags['safe'], tags['latest'])
        # A block fetched by number and by its tag agree.
        self.assertEqual(self.rpc.block(tags['safe'])['hash'], self.rpc.block('safe')['hash'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
