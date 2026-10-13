"""PoC: Lightning channels denominated in a Liquid issued asset.

See common/liquid_asset.h.  With CLN_DEV_LIQUID_ASSET set, every channel's
funding tx pays the asset to the 2-of-2 at output n and an L-BTC fee reserve to
the same 2-of-2 at output n+1; channel transactions take on-chain fees from that
reserve.  The funding tx is built by elementsd's wallet (CLN's wallet doesn't
know about assets in this PoC), and only the happy path is covered: open, pay
across a hop, cooperative close.

    TEST_NETWORK=liquid-regtest pytest tests/test_liquid_asset_poc.py
"""
from fixtures import *  # noqa: F401,F403
from decimal import Decimal
from utils import only_one, wait_for, TEST_NETWORK

import pytest
import struct


FEE_RESERVE_SAT = 100_000
CHANNEL_UNITS = 5_000_000   # asset base units in each channel
PAY_UNITS = 1_000_000


def varint(n):
    if n < 0xfd:
        return bytes([n])
    return b'\xfd' + struct.pack('<H', n)


def explicit_tx(ins, outs):
    """Unsigned Elements tx with explicit (unblinded) outputs.
    ins: [(txid, vout)], outs: [(asset_hex, amount, scriptpubkey_bytes)]"""
    b = struct.pack('<i', 2) + b'\x00' + varint(len(ins))
    for txid, n in ins:
        b += bytes.fromhex(txid)[::-1] + struct.pack('<I', n) + b'\x00' + struct.pack('<I', 0xfffffffd)
    b += varint(len(outs))
    for asset, amount, spk in outs:
        b += (b'\x01' + bytes.fromhex(asset)[::-1]
              + b'\x01' + struct.pack('>Q', amount)
              + b'\x00' + varint(len(spk)) + spk)
    return (b + struct.pack('<I', 0)).hex()


def unconfidential(bitcoind):
    info = bitcoind.rpc.getaddressinfo(bitcoind.rpc.getnewaddress())
    return info['unconfidential'], bytes.fromhex(info['scriptPubKey'])


def explicit_utxo(bitcoind, asset, amount):
    """Have elementsd's wallet pay itself an explicit output in `asset`."""
    addr, spk = unconfidential(bitcoind)
    txid = bitcoind.rpc.sendtoaddress(addr, str(amount), "", "", False, False,
                                      None, None, None, asset)
    # No -txindex: look it up while it's still in the mempool.
    vouts = bitcoind.rpc.getrawtransaction(txid, True)['vout']
    bitcoind.generate_block(1, wait_for_mempool=txid)
    for v in vouts:
        if v['scriptPubKey']['hex'] == spk.hex() and 'value' in v:
            return txid, v['n'], int(Decimal(str(v['value'])) * 10**8)
    raise ValueError("no explicit output found")


def fund_asset_channel(bitcoind, opener, peer, asset, lbtc):
    start = opener.rpc.fundchannel_start(peer.info['id'], CHANNEL_UNITS,
                                         announce=False)
    funding_spk = bytes.fromhex(start['scriptpubkey'])

    x_txid, x_n, x_amount = explicit_utxo(bitcoind, asset, 1)
    l_txid, l_n, l_amount = explicit_utxo(bitcoind, lbtc, Decimal('0.01'))
    fee = 1000
    raw = explicit_tx(
        [(x_txid, x_n), (l_txid, l_n)],
        [(asset, CHANNEL_UNITS, funding_spk),           # channel capacity
         (lbtc, FEE_RESERVE_SAT, funding_spk),          # fee reserve, same 2-of-2
         (asset, x_amount - CHANNEL_UNITS, unconfidential(bitcoind)[1]),
         (lbtc, l_amount - FEE_RESERVE_SAT - fee, unconfidential(bitcoind)[1]),
         (lbtc, fee, b'')])
    pset = bitcoind.rpc.utxoupdatepsbt(bitcoind.rpc.converttopsbt(raw))
    opener.rpc.fundchannel_complete(peer.info['id'], pset)

    signed = bitcoind.rpc.signrawtransactionwithwallet(raw)
    assert signed['complete']
    return bitcoind.rpc.sendrawtransaction(signed['hex'])


def outputs_by_asset(bitcoind, txid, asset, lbtc, blockhash=None):
    tx = (bitcoind.rpc.getrawtransaction(txid, True, blockhash) if blockhash
          else bitcoind.rpc.getrawtransaction(txid, True))
    units = {asset: [], lbtc: []}
    for v in tx['vout']:
        units[v['asset']].append((int(Decimal(str(v['value'])) * 10**8),
                                  v['scriptPubKey'].get('hex', '')))
    return tx, units


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
def test_liquid_asset_channel_poc(node_factory, bitcoind, monkeypatch):
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    # Every node (and its subdaemons) runs asset channels in this asset.
    monkeypatch.setenv('CLN_DEV_LIQUID_ASSET', asset)
    monkeypatch.setenv('CLN_DEV_LIQUID_FEE_RESERVE_SAT', str(FEE_RESERVE_SAT))
    l1, l2, l3 = node_factory.get_nodes(3)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)

    # Open l1->l2 and l2->l3: asset output + L-BTC fee reserve to the same 2-of-2.
    for opener, peer in ((l1, l2), (l2, l3)):
        txid = fund_asset_channel(bitcoind, opener, peer, asset, lbtc)
        tx, units = outputs_by_asset(bitcoind, txid, asset, lbtc)
        assert (CHANNEL_UNITS, tx['vout'][0]['scriptPubKey']['hex']) in units[asset]
        assert tx['vout'][1]['asset'] == lbtc
        assert tx['vout'][1]['scriptPubKey']['hex'] == tx['vout'][0]['scriptPubKey']['hex']

    bitcoind.generate_block(6)
    for n in (l1, l2, l3):
        wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL'
                             for c in n.rpc.listpeerchannels()['channels']))
    # l3's invoice needs l2's (private) channel_update for its route hint.
    wait_for(lambda: 'remote' in only_one(l3.rpc.listpeerchannels()['channels']).get('updates', {}))

    # Pay across the hop; amounts are in milli-units of the asset.  All
    # channels are private, so name l3's channel to get it into the route
    # hint (otherwise it looks like a dead end from gossip).
    scid23 = only_one(l3.rpc.listpeerchannels()['channels'])['short_channel_id']
    inv = l3.rpc.invoice(PAY_UNITS * 1000, 'asset-payment', 'paid in the asset',
                         exposeprivatechannels=[scid23])
    l1.rpc.pay(inv['bolt11'])
    assert only_one(l3.rpc.listinvoices('asset-payment')['invoices'])['status'] == 'paid'

    # pay returns on the preimage; wait for the HTLCs to be settled.
    for n in (l1, l2, l3):
        wait_for(lambda: all(c['htlcs'] == []
                             for c in n.rpc.listpeerchannels()['channels']))

    expected_peer_units = {}
    for opener, peer in ((l1, l2), (l2, l3)):
        ch = only_one(opener.rpc.listpeerchannels(peer.info['id'])['channels'])
        expected_peer_units[peer.info['id']] = (ch['total_msat'] - ch['to_us_msat']) // 1000
    assert expected_peer_units[l2.info['id']] > PAY_UNITS     # l2 earned a routing fee
    assert expected_peer_units[l3.info['id']] == PAY_UNITS

    # Cooperative close: spends both outputs, fee comes from the L-BTC reserve.
    for opener, peer in ((l1, l2), (l2, l3)):
        res = opener.rpc.close(peer.info['id'])
        assert res['type'] == 'mutual'
        # The opener funded via fundchannel_complete and never broadcast the
        # funding itself, so it leaves broadcasting the close to the peer.
        wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
        txid = only_one(bitcoind.rpc.getrawmempool())
        blockhash = bitcoind.generate_block(1, wait_for_mempool=txid)[0]

        tx, units = outputs_by_asset(bitcoind, txid, asset, lbtc, blockhash)
        assert len(tx['vin']) == 2
        assert sum(u for u, _ in units[asset]) == CHANNEL_UNITS
        assert expected_peer_units[peer.info['id']] in [u for u, _ in units[asset]]
        # L-BTC: the opener's change plus the fee output use up the reserve.
        assert sum(u for u, _ in units[lbtc]) == FEE_RESERVE_SAT

    # Nobody crashed processing the closes (onchaind sees asset outputs).
    bitcoind.generate_block(1)
    for n in (l1, l2, l3):
        n.rpc.getinfo()
