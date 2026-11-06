# SPDX-License-Identifier: MIT
"""evm.py offline: keccak, ABI with tuples (against Foundry's cast), the
"too many results" classification and eth_getLogs range splitting."""
import os
import shutil
import subprocess

import pytest

import evm

CAST = shutil.which('cast') or os.path.expanduser('~/.foundry/bin/cast')


def cast(*args):
    return subprocess.run([CAST, *args], check=True, capture_output=True, text=True).stdout.strip()


def test_keccak():
    assert evm.keccak256(b'').hex() == 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470'
    assert evm.keccak256(b'abc').hex() == '4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45'
    assert evm.selector('transfer(address,uint256)').hex() == 'a9059cbb'


def test_submit_calldata_against_cast():
    sig = 'submit(bytes,(uint64,bytes,uint32)[])'
    raw = bytes(range(100))
    prevouts = [(100000, b'\x00\x14' + b'\x11' * 20, 7), (2**64 - 1, b'', 2**32 - 1),
                (1, b'\x51\x20' + b'\x22' * 32, 1)]
    ours = evm.calldata(sig, ['bytes', '(uint64,bytes,uint32)[]'], [raw, prevouts])
    theirs = cast('calldata', sig, '0x' + raw.hex(),
                  '[' + ','.join('({},0x{},{})'.format(a, s.hex(), h) for a, s, h in prevouts) + ']')
    assert '0x' + ours.hex() == theirs
    assert evm.abi_decode(['bytes', '(uint64,bytes,uint32)[]'], ours[4:]) == [raw, prevouts]
    # A static tuple is inline.
    ours = evm.calldata('f((uint256,address),uint8)', ['(uint256,address)', 'uint8'],
                        [(5, '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'), 3])
    assert '0x' + ours.hex() == cast('calldata', 'f((uint256,address),uint8)',
                                     '(5,0x70997970C51812dc3A010C7d01b50e0d17dc79C8)', '3')
    with pytest.raises(ValueError):
        evm.abi_encode(['(uint64,bytes'], [(1, b'')])


@pytest.mark.parametrize('msg,code,split', [
    ('eth_getLogs: query returned more than 10000 results. Try with this block range [0x1, 0x5].', -32005, True),
    ('eth_getLogs: Log response size exceeded. You can make eth_getLogs requests with up to a 2K block range',
     -32602, True),
    ('eth_getLogs: exceed maximum block range: 5000', -32000, True),
    ('eth_getLogs: block range is too wide', -32000, True),
    ('eth_getLogs: too many logs', None, True),
    ('eth_getLogs: daily request count exceeded, request rate limited', -32005, False),
    ('eth_getLogs: project ID request rate exceeded', -32005, False),
    ('eth_getLogs: header not found', -32000, False),
    ('eth_getLogs: timed out', None, False),
])
def test_too_many_results(msg, code, split):
    assert evm.too_many_results(evm.EvmError(msg, code)) is split


class LogsNode(evm.EvmRpc):
    """eth_getLogs over blocks 0..n-1 with logs_per_block[i] logs each, failing
    above `cap` results like Infura."""

    def __init__(self, logs_per_block, cap=10000):
        super().__init__('http://fake.invalid')
        self.lpb, self.cap, self.ranges = logs_per_block, cap, []

    def call(self, method, *params):
        assert method == 'eth_getLogs'
        lo, hi = int(params[0]['fromBlock'], 16), int(params[0]['toBlock'], 16)
        self.ranges.append((lo, hi))
        n = sum(self.lpb[lo:hi + 1])
        if n > self.cap:
            raise evm.EvmError('eth_getLogs: query returned more than {} results'.format(self.cap), -32005)
        return [{'blockNumber': evm.qty(b), 'logIndex': evm.qty(i)}
                for b in range(lo, hi + 1) for i in range(self.lpb[b])]


def test_get_logs_ranges_start_at_2000():
    node = LogsNode([0] * 5000)
    chunks = list(node.get_logs_chunked('0x' + '00' * 20, None, 0, 4999))
    assert [(a, b) for a, b, _ in chunks] == [(0, 1999), (2000, 3999), (4000, 4999)]
    assert node.ranges == [(0, 1999), (2000, 3999), (4000, 4999)]
    assert list(node.get_logs_chunked('0x' + '00' * 20, None, 10, 9)) == []


def test_get_logs_splits_on_too_many_results():
    lpb = [0] * 6000
    for b in range(1000, 1100):
        lpb[b] = 300                        # 30,000 logs in 100 blocks
    lpb[3000] = 9000
    node = LogsNode(lpb, cap=10000)
    chunks = list(node.get_logs_chunked('0x' + '00' * 20, None, 0, 5999))
    got = [(int(x['blockNumber'], 16), int(x['logIndex'], 16)) for _, _, logs in chunks for x in logs]
    assert got == [(b, i) for b in range(6000) for i in range(lpb[b])]     # all, in order, once
    spans = [(a, b) for a, b, _ in chunks]
    assert spans[0][0] == 0 and spans[-1][1] == 5999
    assert all(spans[i][1] + 1 == spans[i + 1][0] for i in range(len(spans) - 1))
    assert all(sum(lpb[a:b + 1]) <= 10000 for a, b in spans)
    assert max(b - a + 1 for a, b in node.ranges) == 2000
    failed = [r for r in node.ranges if sum(lpb[r[0]:r[1] + 1]) > 10000]
    assert failed and all(r[1] - r[0] + 1 > 1 for r in failed)
    assert len(node.ranges) < 40


def test_get_logs_single_block_too_big_raises():
    node = LogsNode([0, 20000, 0], cap=10000)
    with pytest.raises(evm.EvmError):
        list(node.get_logs_chunked('0x' + '00' * 20, None, 0, 2))
    assert node.ranges[-1] == (1, 1)


def test_get_logs_other_errors_propagate():
    class Down(evm.EvmRpc):
        def call(self, method, *params):
            raise evm.EvmError('eth_getLogs: daily request count exceeded, request rate limited', -32005)
    node = Down('http://fake.invalid')
    with pytest.raises(evm.EvmError):
        list(node.get_logs_chunked('0x' + '00' * 20, None, 0, 100000))
