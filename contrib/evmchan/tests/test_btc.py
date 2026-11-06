# SPDX-License-Identifier: MIT
"""btc.py offline: the coinbase and blocks of SPEC 3.2 byte for byte (against
an independent construction and known Bitcoin vectors), the deposit tx of
SPEC 3.3, real CLN transactions, strict parsing and bech32/bech32m."""
import glob
import hashlib
import json
import os
import random
import struct

import pytest

import btc
import pyref

from conftest import EVMCHAN


def H(b):
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def core_scriptnum(n):
    """Bitcoin Core's CScriptNum::serialize, transcribed (handles negatives)."""
    if n == 0:
        return b''
    neg, a, out = n < 0, abs(n), bytearray()
    while a:
        out.append(a & 0xff)
        a >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if neg else 0)
    elif neg:
        out[-1] |= 0x80
    return bytes(out)


def ref_coinbase(h):
    """SPEC 3.2's coinbase written out field by field, independently of btc.py."""
    n = core_scriptnum(h) or b'\x00'
    ss = bytes([len(n)]) + n + b'\x07evmchan'
    return (b'\x02\x00\x00\x00' + b'\x01' + b'\x00' * 32 + b'\xff\xff\xff\xff'
            + bytes([len(ss)]) + ss + b'\xff\xff\xff\xff'
            + b'\x01' + b'\x00' * 8 + b'\x01\x6a' + b'\x00\x00\x00\x00')


def ref_merkle(ids):
    while len(ids) > 1:
        if len(ids) & 1:
            ids = ids + [ids[-1]]
        ids = [H(ids[i] + ids[i + 1]) for i in range(0, len(ids), 2)]
    return ids[0]


def ref_block(h, prev, raws, txids, G, I):  # noqa: E741
    cb = ref_coinbase(h)
    root = ref_merkle([H(cb)] + txids)
    hdr = struct.pack('<I32s32sIII', 0x20000000, prev, root, G + h * I, 0x207fffff, 0)
    n = 1 + len(raws)
    cnt = bytes([n]) if n < 0xfd else b'\xfd' + struct.pack('<H', n)
    return hdr + cnt + cb + b''.join(raws), H(hdr)


HEIGHTS = ([0] + list(range(1, 17)) + [17, 75, 76, 127, 128, 255, 256, 32767, 32768, 65535, 65536,
           2**23 - 1, 2**23, 2**24 - 1, 2**24, 2**24 + 1, 500000, 2**31 - 1, 2**31, 2**32 - 1])

# scriptSig height pushes, written by hand (and, above 16, Bitcoin's BIP34 ones:
# mainnet block 500000's coinbase starts 03 20a107).
PUSHES = {0: '0100', 1: '0101', 2: '0102', 16: '0110', 17: '0111', 127: '017f', 128: '028000',
          255: '02ff00', 256: '020001', 32767: '02ff7f', 32768: '03008000', 65535: '03ffff00',
          2**23 - 1: '03ffff7f', 2**23: '0400008000', 2**24 - 1: '04ffffff00', 2**24: '0400000001',
          500000: '0320a107', 2**31 - 1: '04ffffff7f', 2**31: '050000008000', 2**32 - 1: '05ffffffff00'}


@pytest.mark.parametrize('h', HEIGHTS)
def test_coinbase_byte_exact(h):
    cb = btc.coinbase(h)
    raw = cb.serialize()
    assert raw == cb.serialize(witness=False) == ref_coinbase(h)
    ss = cb.inputs[0].script_sig
    assert 1 <= ss[0] <= 5, 'always a direct push, never OP_0 or OP_1..OP_16'
    assert ss[1 + ss[0]:] == b'\x07evmchan'
    assert cb.txid() == H(raw) and cb.txid_hex() == H(raw)[::-1].hex()
    assert cb.is_coinbase()
    assert btc.Tx.parse(raw).serialize() == raw
    if h in PUSHES:
        assert ss[:1 + ss[0]].hex() == PUSHES[h]


def test_coinbase_full_hex():
    assert btc.coinbase(0).serialize().hex() == (
        '02000000' '01' + '00' * 32 + 'ffffffff' '0a' '0100' '07' + b'evmchan'.hex() + 'ffffffff'
        '01' '0000000000000000' '01' '6a' '00000000')
    assert btc.coinbase(128).serialize().hex() == (
        '02000000' '01' + '00' * 32 + 'ffffffff' '0b' '028000' '07' + b'evmchan'.hex() + 'ffffffff'
        '01' '0000000000000000' '01' '6a' '00000000')
    # Unique per height.
    assert len({btc.coinbase(h).txid() for h in HEIGHTS}) == len(HEIGHTS)
    with pytest.raises(ValueError):
        btc.coinbase(2**32)


GENESIS_COINBASE = (
    '01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff4d04ffff001d01'
    '04455468652054696d65732030332f4a616e2f32303039204368616e63656c6c6f72206f6e206272696e6b206f662073'
    '65636f6e64206261696c6f757420666f722062616e6b73ffffffff0100f2052a01000000434104678afdb0fe55482719'
    '67f1a67130b7105cd6a828e03909a67962e0ea1f61deb649f6bc3f4cef38c4f35504e51ec112de5c384df7ba0b8d578a'
    '4c702b6bf11d5fac00000000')


def test_bitcoin_genesis_vector():
    tx = btc.Tx.parse(bytes.fromhex(GENESIS_COINBASE))
    assert tx.txid_hex() == '4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b'
    root = btc.merkle_root([tx.txid()])
    hdr = btc.header(bytes(32), root, 1231006505, version=1, bits=0x1d00ffff, nonce=2083236893)
    assert hdr.hex() == ('0100000000000000000000000000000000000000000000000000000000000000000000003ba3edfd'
                         '7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a29ab5f49ffff001d1dac2b7c')
    assert btc.display(btc.block_hash(hdr)) == '000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f'


def test_bitcoin_block_100000_vector():
    # Four txids, so the merkle tree has two levels.
    txids = ['8c14f0db3df150123e6f3dbbf30f8b955a8249b62ac1d1ff16284aefa3d06d87',
             'fff2525b8931402dd09222c50775608f75787bd2b87e56995a7bdd30f79702c4',
             '6359f0868171b1d194cbee1af2f16ea598ae8fad666d9b012c8ed2b79a236ec4',
             'e9a66845e05d5abc0ad04ec80f774a7e585c6e8db975962d069a522137b80c1d']
    root = btc.merkle_root([btc.internal(t) for t in txids])
    assert btc.display(root) == 'f3e94742aca4b5ef85488dc37c06c3282295ffec960994b2c0d5ac2a25a95766'
    hdr = btc.header(btc.internal('000000000002d01c1fccc21636b607dfd930d31d01c3a62104612a1719011250'),
                     root, 1293623863, version=1, bits=0x1b04864c, nonce=274148111)
    assert btc.display(btc.block_hash(hdr)) == '000000000003ba27aa200b1cecaad478d2b00432346c3f1f3986da1afd33e506'


@pytest.mark.parametrize('n', list(range(1, 12)) + [16, 17, 33])
def test_merkle_odd_duplication(n):
    rng = random.Random(n)
    ids = [rng.randbytes(32) for _ in range(n)]
    assert btc.merkle_root(ids) == ref_merkle(ids)
    if n == 1:
        assert btc.merkle_root(ids) == ids[0]


def test_blocks_against_independent_construction():
    rng = random.Random(3)
    G, I = 1793949807, 2  # noqa: E741
    prev = bytes(32)
    vectors = [json.loads(line) for f in sorted(glob.glob(os.path.join(
        EVMCHAN, 'spikes', 's1-toychain', 'vectors', '*.jsonl'))) for line in open(f)]
    raws = [bytes.fromhex(v['tx']) for v in vectors]
    for h in list(range(0, 20)) + [127, 128, 32767, 32768, 2**24]:
        if h == 0:
            txs = []
        elif h == 128:      # 261 txs: a 3-byte tx count
            txs = [rng.choice(raws) for _ in range(260)]
        else:
            txs = rng.sample(raws, rng.choice([0, 0, 1, 2, 3, 4, 5]))
        txids = [btc.txid_of(r) for r in txs]
        got, ghash = btc.build_block(h, prev, txs, G, I)
        want, whash = ref_block(h, prev, txs, txids, G, I)
        assert got == want and ghash == whash, h
        hdr, parsed = btc.parse_block(got)
        assert hdr == got[:80] and len(parsed) == 1 + len(txs)
        assert parsed[0].serialize() == ref_coinbase(h)
        assert [t.serialize() for t in parsed[1:]] == txs
        prev = ghash


def test_cln_vectors_round_trip():
    files = sorted(glob.glob(os.path.join(EVMCHAN, 'spikes', 's1-toychain', 'vectors', '*.jsonl')))
    assert files
    n = witness = 0
    for f in files:
        for line in open(f):
            v = json.loads(line)
            raw = bytes.fromhex(v['tx'])
            tx = btc.Tx.parse(raw)
            assert tx.serialize() == raw
            assert tx.txid_hex() == v['txid']
            assert tx.txid() == btc.internal(v['txid']) == H(tx.serialize(witness=False))
            stripped = btc.Tx.parse(tx.serialize(witness=False))
            assert stripped.txid() == tx.txid() and not stripped.has_witness()
            assert pyref.parse_tx(raw).txid() == v['txid']     # independent parser agrees
            witness += tx.has_witness()
            n += 1
    assert n >= 50 and witness >= 30


def test_strict_parse():
    good = bytes.fromhex(json.loads(open(glob.glob(os.path.join(
        EVMCHAN, 'spikes', 's1-toychain', 'vectors', 'cln-regtest-txs.jsonl'))[0]).readlines()[3])['tx'])
    tx = btc.Tx.parse(good)
    assert tx.has_witness()
    bad = {
        'trailing': good + b'\x00',
        'truncated': good[:-1],
        'flag': good[:5] + b'\x02' + good[6:],
        'empty witness with marker': btc.Tx(2, tx.inputs, tx.outputs, 0).serialize()[:4] + b'\x00\x01'
        + btc.Tx(2, tx.inputs, tx.outputs, 0).serialize()[4:-4] + b'\x00' * len(tx.inputs) + b'\x00' * 4,
        'non-canonical input count': good[:6] + b'\xfd\x01\x00' + good[7:],
        'no outputs': btc.Tx(2, tx.inputs, [], 0).serialize(),
    }
    for name, raw in bad.items():
        with pytest.raises(btc.ParseError):
            btc.Tx.parse(raw)
            pytest.fail(name)


def test_deposit_tx_offline():
    contract = '0x5FbDB2315678afecb367f032d93F642f64180aa3'
    for nonce, amount, spk in [(0, 1, b'\x00\x14' + bytes(20)), (1, 10**8, b'\x51\x20' + b'\x07' * 32),
                               (7, btc.MAX_MONEY, b''), (2**40, 123, b'\xab' * 253)]:
        tx = btc.deposit_tx_for(31337, contract, nonce, amount, spk)
        raw, txid = pyref.deposit_tx(31337, contract, nonce, amount, spk)
        assert tx.serialize() == raw and tx.txid_hex() == txid
        # Written out: version 2, one input (prevout:0, empty scriptSig, final), one output, locktime 0.
        prev = btc.deposit_prevout(31337, contract, nonce)
        assert raw == (b'\x02\x00\x00\x00\x01' + prev + b'\x00' * 4 + b'\x00\xff\xff\xff\xff\x01'
                       + struct.pack('<Q', amount) + btc.varint(len(spk)) + spk + b'\x00' * 4)
        assert not tx.is_coinbase()


def test_txid_byte_order_helpers():
    b = bytes(range(32))
    assert btc.display(b) == bytes(reversed(b)).hex()
    assert btc.internal(btc.display(b)) == b
    with pytest.raises(ValueError):
        btc.internal('00' * 31)


# BIP173 / BIP350 vectors: (address, scriptPubKey hex).
VALID = [
    ('BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4', '0014751e76e8199196d454941c45d1b3a323f1433bd6'),
    ('tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7',
     '00201863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262'),
    ('bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kt5nd6y',
     '5128751e76e8199196d454941c45d1b3a323f1433bd6751e76e8199196d454941c45d1b3a323f1433bd6'),
    ('BC1SW50QGDZ25J', '6002751e'),
    ('bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs', '5210751e76e8199196d454941c45d1b3a323'),
    ('tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesf3hn0c',
     '5120000000c4a5cad46221b2a187905e5266362b99d5e91c6ce24d165dab93e86433'),
]
INVALID = [
    'tc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq5zuyut',   # wrong hrp is fine to decode...
    'bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd',   # v1 with a bech32 checksum
    'BC1S0XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ54WELL',   # v16 with a bech32 checksum
    'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh',                         # v0 with a bech32m checksum
    'bc1zw508d6qejxtdg4y5r3zarvaryvqyzf3du',                              # bad padding
    'bc1gmk9yu',                                                          # empty program
    'bc1qr508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4',                         # bad checksum
    'tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sL5k7',    # mixed case
]


def test_bech32_vectors():
    for addr, spk in VALID:
        assert btc.address_script(addr).hex() == spk
        hrp = addr.lower().rsplit('1', 1)[0]
        assert btc.script_address(bytes.fromhex(spk), hrp) == addr.lower()
    for addr in INVALID[1:]:
        with pytest.raises(ValueError):
            btc.address_script(addr)
    with pytest.raises(ValueError):
        btc.address_script(INVALID[0], hrp='tb')


def test_exit_address():
    eth = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
    addr = btc.exit_address(eth)
    assert addr.startswith('bcrt1s')
    spk = btc.address_script(addr, 'bcrt')
    assert spk == b'\x60\x14' + bytes.fromhex(eth[2:]) == btc.exit_script(eth)
    assert btc.is_exit_script(spk) and not btc.is_op_return(spk)
    assert btc.decode_segwit_address(addr) == ('bcrt', 16, bytes.fromhex(eth[2:]))
    assert not btc.is_exit_script(b'\x60\x15' + bytes(21)) and not btc.is_exit_script(b'\x60\x14' + bytes(19))
    assert btc.is_op_return(b'\x6a') and not btc.is_op_return(b'')
