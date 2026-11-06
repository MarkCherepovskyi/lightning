# SPDX-License-Identifier: MIT
"""btc.py and evmindex against the real contract on anvil (started with
run-anvil.sh): deposit txs and txids, real signed submits, blocks, the tip
following `safe`, utxo() through spends, exits and OP_RETURN, add_receipt,
and reorgs (evm_snapshot/evm_revert, anvil_reorg, anvil_rollback)."""
import pytest

import btc
import evm
import evmindex
import pyref
from conftest import SAT, Chain, Key, check_valid, sign
from test_btc import ref_block, ref_coinbase

A, B, C = Key(0xA11CE), Key(0xB0B), Key(0xCAFE)
EXIT_TO = '0x000000000000000000000000000000000000dEaD'


@pytest.fixture
def index(chain, tmp_path):
    ix = evmindex.Index(str(tmp_path / 'evmchan.sqlite3'), chain.rpc, chain.address)
    yield ix
    ix.close()


def check_blocks(ix, chain):
    """Every block <= tip against an independent construction from the node's
    own logs (EVM order), and the prev_hash chain."""
    logs = sorted(chain.logs(), key=lambda x: (int(x['blockNumber'], 16), int(x['logIndex'], 16)))
    by_height = {}
    for log in logs:
        raw = evm.abi_decode(['bytes'], evm.unhex(log['data']))[0]
        by_height.setdefault(int(log['topics'][1], 16), []).append((raw, evm.unhex(log['topics'][2])))
    prev = bytes(32)
    for h in range(ix.tip() + 1):
        txs = by_height.get(h, [])
        want, whash = ref_block(h, prev, [r for r, _ in txs], [t for _, t in txs], chain.G, chain.I)
        got_hash, got = ix.block(h)
        assert got == want, h
        assert got_hash == btc.display(whash)
        hdr, parsed = btc.parse_block(got)
        assert hdr[4:36] == prev
        assert parsed[0].serialize() == ref_coinbase(h)
        assert btc.merkle_root([t.txid() for t in parsed]) == hdr[36:68]
        prev = whash
    assert ix.block(ix.tip() + 1) is None
    return by_height


def deposit_log(chain, rc):
    logs = [x for x in rc['logs'] if x['topics'][0] == evmindex.TOPIC0]
    assert len(logs) == 1
    return int(logs[0]['topics'][1], 16), evm.unhex(logs[0]['topics'][2]), \
        evm.abi_decode(['bytes'], evm.unhex(logs[0]['data']))[0]


def test_deposit_txids_match_the_contract(chain):
    scripts = [A.p2wpkh, B.p2tr, b'\x00\x20' + bytes(range(32)), b'', b'\x51' * 252, b'\x52' * 253,
               b'\x53' * 10000]
    amounts = [1, 100000, 10**8, 123456789, 2, 3, 4]
    for nonce, (spk, sats) in enumerate(zip(scripts, amounts)):
        h = 1 + nonce // 2     # INTERVAL 2: two EVM blocks per height
        rc = chain.deposit(spk, sats, h)
        height, txid, raw = deposit_log(chain, rc)
        tx = btc.deposit_tx_for(chain.chain_id, chain.address, nonce, sats, spk)
        assert raw == tx.serialize()
        assert txid == tx.txid() == btc.dsha(raw)
        ref_raw, ref_txid = pyref.deposit_tx(chain.chain_id, chain.address, nonce, sats, spk)
        assert ref_raw == raw and ref_txid == btc.display(txid)
        assert height == h
        assert chain.is_unspent(txid, 0, sats, spk, h)
        assert not chain.is_unspent(txid, 0, sats + 1, spk, h)
    assert chain.const('depositNonce') == len(scripts)


def test_blocks_heights_and_tip(chain, index):
    ix = index
    assert ix.ibd and ix.tip() == 0
    d1 = chain.deposit(A.p2wpkh, 50000, 1)
    d2 = chain.deposit(B.p2tr, 60000, 1)
    # Two deposits in one EVM block: log index order.
    data = evm.calldata('deposit(bytes)', ['bytes'], [C.p2wpkh])
    many = chain.send_many([(70000 * SAT, data), (80000 * SAT, data)], 3)
    chain.confirm(5)
    r = ix.sync()
    assert not ix.ibd
    safe = chain.rpc.block('safe')
    assert ix.tip() == r['tip'] == ix.height_at(int(safe['timestamp'], 16)) - 1 >= 5
    by_height = check_blocks(ix, chain)
    assert [len(by_height.get(h, [])) for h in range(6)] == [0, 2, 0, 2, 0, 0]
    for rc in (d1, d2):
        assert btc.display(deposit_log(chain, rc)[1]) in [btc.display(t) for _, t in by_height[1]]
    _, raw = ix.block(3)
    txs = btc.parse_block(raw)[1]
    assert [t.outputs[0][0] for t in txs[1:]] == [70000, 80000]
    assert [t.txid() for t in txs[1:]] == [deposit_log(chain, rc)[1] for rc in many]
    assert ix.lag() == ix.height_at(chain.latest_ts()) - ix.tip()


def test_submits_spends_exits_op_return(chain, index):
    ix = index
    rc = chain.deposit(A.p2wpkh, 100000, 1)
    _, dtxid, _ = deposit_log(chain, rc)
    ix.sync()
    assert ix.utxo(btc.display(dtxid), 0) == {'amount': 100000, 'script': A.p2wpkh, 'height': 1}

    # tx1: P2WPKH -> P2TR, OP_RETURN, an exit, P2WPKH; fee 1000.
    tx1 = btc.Tx(2, [btc.TxIn(dtxid, 0)],
                 [(50000, B.p2tr), (0, b'\x6a\x05hello'), (20000, btc.exit_script(EXIT_TO)), (29000, C.p2wpkh)])
    pv1 = ix.prevouts(tx1)
    assert pv1 == [(100000, A.p2wpkh, 1)]
    raw1 = sign(tx1, [A], pv1).serialize()
    assert check_valid(chain, raw1, pv1, 2) == 1000
    rc1 = chain.submit(raw1, pv1, 2)
    assert ix.add_receipt(rc1) == [tx1.txid_hex()]          # before any sync
    assert ix.utxo(btc.display(dtxid), 0) is None            # spent
    assert ix.utxo(tx1.txid_hex(), 0) == {'amount': 50000, 'script': B.p2tr, 'height': 2}
    assert ix.utxo(tx1.txid_hex(), 1) is None                # OP_RETURN
    assert ix.utxo(tx1.txid_hex(), 2) is None                # exit
    assert ix.utxo(tx1.txid_hex(), 3)['amount'] == 29000
    assert ix.utxo(tx1.txid_hex(), 4) is None
    assert ix.tx(tx1.txid_hex())['raw'] == raw1
    assert chain.withdrawable(EXIT_TO) == 20000 * SAT
    assert chain.withdrawable(chain.acct.address) == 1000 * SAT

    # tx2, a child in the same virtual block, found through add_receipt only:
    # P2TR key path + P2WPKH -> P2WPKH.
    tx2 = btc.Tx(2, [btc.TxIn(tx1.txid(), 0), btc.TxIn(tx1.txid(), 3)], [(78000, A.p2wpkh)])
    pv2 = ix.prevouts(tx2)
    assert pv2 == [(50000, B.p2tr, 2), (29000, C.p2wpkh, 2)]
    raw2 = sign(tx2, [B, C], pv2).serialize()
    check_valid(chain, raw2, pv2, 2)
    rc2 = chain.submit(raw2, pv2, 2)
    ix.add_receipt(rc2)
    assert ix.prevouts(tx2) is None
    assert ix.utxo(tx2.txid_hex(), 0) == {'amount': 78000, 'script': A.p2wpkh, 'height': 2}

    # The index's unspent set is the contract's.
    for txid, vout, amount, spk, h in [(dtxid, 0, 100000, A.p2wpkh, 1), (tx1.txid(), 0, 50000, B.p2tr, 2),
                                       (tx1.txid(), 3, 29000, C.p2wpkh, 2), (tx2.txid(), 0, 78000, A.p2wpkh, 2)]:
        assert chain.is_unspent(txid, vout, amount, spk, h) == (ix.utxo_internal(txid, vout) is not None)

    chain.confirm(3)
    assert ix.sync()['proven']
    check_blocks(ix, chain)
    # SPEC 4.1: txCount and logHash, recomputed here from the node's own logs
    # (Python keccak), equal the contract's; the index's checkpoint is the
    # contract's state at that block, read with EIP-1898.
    count, h = 0, bytes(32)
    for log in sorted(chain.logs(), key=lambda x: (int(x['blockNumber'], 16), int(x['logIndex'], 16))):
        count, h = count + 1, evmindex.log_step(h, int(log['topics'][1], 16), evm.unhex(log['topics'][2]))
    assert count == 3 == chain.const('txCount')
    assert evm.abi_decode(['bytes32'], chain.rpc.eth_call(chain.address, evm.selector('logHash()')))[0] == h
    number, bhash, cp_count, cp_hash = ix.checkpoint()
    at = {'blockHash': bhash}
    assert cp_count == evm.abi_decode(['uint256'], chain.rpc.eth_call(chain.address, evm.selector('txCount()'), at))[0]
    assert evm.unhex(cp_hash) == chain.rpc.eth_call(chain.address, evm.selector('logHash()'), at)
    _, raw = ix.block(2)
    txs = btc.parse_block(raw)[1]
    assert [t.serialize() for t in txs[1:]] == [raw1, raw2]     # with witness, in EVM order
    assert txs[1].has_witness()


def test_tip_follows_safe_and_never_decreases(chain, index, tmp_path):
    ix = index
    latest_ix = evmindex.Index(str(tmp_path / 'latest.sqlite3'), chain.rpc, chain.address, tag='latest')
    seen = []
    for h in (1, 2, 4, 7, 8):
        chain.mine_at(h)
        ix.sync()
        latest_ix.sync()
        safe = chain.rpc.block('safe')
        assert ix.tip() == max(ix.height_at(int(safe['timestamp'], 16)) - 1, 0)
        assert latest_ix.tip() == max(h - 1, 0)
        seen.append(ix.tip())
    assert seen == sorted(seen) and seen[-1] == 6
    snap = chain.rpc.call('evm_snapshot')
    chain.mine_at(20)
    chain.mine_at(21)
    ix.sync()
    latest_ix.sync()
    high, high_latest = ix.tip(), latest_ix.tip()
    assert high == 19 and high_latest == 20
    blocks = [ix.block(h) for h in range(high + 1)]
    assert chain.rpc.call('evm_revert', snap)
    chain.mine(chain.latest_ts() + 1)
    ix.sync()
    latest_ix.sync()
    assert ix.tip() == high and latest_ix.tip() == high_latest    # the chain went back, the tip didn't
    assert [ix.block(h) for h in range(high + 1)] == blocks
    latest_ix.close()


def test_reorg_snapshot_revert(chain, index):
    ix = index
    chain.mine_at(1)
    ix.sync()
    snap = chain.rpc.call('evm_snapshot')
    gone = deposit_log(chain, chain.deposit(A.p2wpkh, 1111, 2))[1]
    ix.sync()
    assert ix.utxo_internal(gone, 0)
    gone_block = ix.tx(btc.display(gone))['evm_block']
    assert chain.rpc.call('evm_revert', snap)
    # Same deposit nonce, another script: another txid, in the same EVM block number.
    rc = chain.deposit(B.p2tr, 2222, 2)
    new = deposit_log(chain, rc)[1]
    assert int(rc['blockNumber'], 16) == gone_block and new != gone
    r = ix.sync()
    assert r['dropped'] == 1
    assert ix.utxo_internal(gone, 0) is None and ix.tx(btc.display(gone)) is None
    assert ix.utxo_internal(new, 0) == {'amount': 2222, 'script': B.p2tr, 'height': 2}
    chain.confirm(3)
    ix.sync()
    check_blocks(ix, chain)


def test_reorg_anvil_reorg_and_rollback(chain, index):
    ix = index
    chain.mine_at(1)
    ix.sync()
    d = deposit_log(chain, chain.deposit(A.p2wpkh, 3333, 2))[1]
    chain.mine(chain.latest_ts() + 1)
    ix.sync()
    assert ix.utxo_internal(d, 0)
    # anvil_reorg: the last 2 blocks replaced by empty ones.
    chain.rpc.call('anvil_reorg', 2, [])
    r = ix.sync()
    assert r['proven'] and r['dropped'] == 1     # the log hash at the tag disagreed: repaired
    assert ix.utxo_internal(d, 0) is None
    # Again, then anvil_rollback: the chain gets shorter than the deposit's block.
    d2 = deposit_log(chain, chain.deposit(B.p2tr, 4444, chain.height_now() + 1))[1]
    ix.sync()
    assert ix.utxo_internal(d2, 0)
    chain.rpc.call('anvil_rollback', 1)
    # `latest` went down: a stale backend for SPEC 5.2, so the poll is skipped
    # and nothing is deleted...
    assert ix.sync()['skipped'] == 'stale'
    assert ix.utxo_internal(d2, 0)
    # ...until the chain is that long again and a proof covers the block.
    chain.mine(chain.latest_ts() + 1)
    ix.sync()
    chain.mine(chain.latest_ts() + 1)
    r = ix.sync()
    assert r['proven'] and r['dropped'] >= 1
    assert ix.utxo_internal(d2, 0) is None
    chain.confirm(chain.height_now() + 1)
    ix.sync()
    check_blocks(ix, chain)


def test_persistence_on_anvil(anvil, chain, tmp_path):
    path = str(tmp_path / 'p.sqlite3')
    ix = evmindex.Index(path, chain.rpc, chain.address)
    d = deposit_log(chain, chain.deposit(C.p2wpkh, 5555, 1))[1]
    chain.confirm(2)
    ix.sync()
    tip, blocks = ix.tip(), [ix.block(h) for h in range(ix.tip() + 1)]
    ix.close()
    ix = evmindex.Index(path, chain.rpc, chain.address)
    assert ix.ibd and ix.tip() == tip
    assert [ix.block(h) for h in range(tip + 1)] == blocks
    assert ix.utxo_internal(d, 0)['amount'] == 5555
    ix.sync()
    assert not ix.ibd
    ix.close()
    with pytest.raises(evmindex.EvmIndexError):     # the file belongs to another contract
        evmindex.Index(path, chain.rpc, Chain(anvil).address)
