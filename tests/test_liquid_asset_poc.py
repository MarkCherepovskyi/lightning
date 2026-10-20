"""PoC: Lightning channels denominated in a Liquid issued asset.

See common/liquid_asset.h.  `fundchannel_start ... asset=<id> fee_reserve=<sat>`
opens an asset channel: its funding tx pays the asset to the 2-of-2 at output n
and an L-BTC fee reserve to the same 2-of-2 at output n+1, and channel
transactions take on-chain fees from that reserve.  Ordinary L-BTC channels keep
working alongside.  The asset funding tx is built by elementsd's wallet (CLN's
wallet doesn't know about assets yet), and only the happy path is covered:
open, pay across a hop, cooperative close.

    TEST_NETWORK=liquid-regtest pytest tests/test_liquid_asset_poc.py
"""
from fixtures import *  # noqa: F401,F403
from decimal import Decimal
from pyln.client import RpcError
from utils import only_one, wait_for, TEST_NETWORK

import pytest
import struct


FEE_RESERVE_SAT = 100_000
CHANNEL_UNITS = 5_000_000   # asset base units in each asset channel
LBTC_CHANNEL_SAT = 5_000_000
PAY_MSAT = 1_000_000_000    # 1,000,000 units (asset) or sats (L-BTC)


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
    # (pyln-client's typed wrapper doesn't know the PoC parameters yet)
    start = opener.rpc.call('fundchannel_start',
                            {'id': peer.info['id'], 'amount': CHANNEL_UNITS,
                             'announce': False, 'asset': asset,
                             'fee_reserve': FEE_RESERVE_SAT})
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


def outputs_by_asset(bitcoind, txid, blockhash=None):
    tx = (bitcoind.rpc.getrawtransaction(txid, True, blockhash) if blockhash
          else bitcoind.rpc.getrawtransaction(txid, True))
    units = {}
    for v in tx['vout']:
        units.setdefault(v['asset'], []).append(int(Decimal(str(v['value'])) * 10**8))
    return tx, units


def channel_by_funding(node, txid):
    return only_one([c for c in node.rpc.listpeerchannels()['channels']
                     if c['funding_txid'] == txid])


def pay_via(l1, l2, l3, funding12, funding23, inv):
    """Pay inv from l1 to l3, forcing the l1->l2 and l2->l3 channels."""
    ch12 = channel_by_funding(l1, funding12)
    ch23 = channel_by_funding(l2, funding23)
    fee = ch23['fee_base_msat'] + PAY_MSAT * ch23['fee_proportional_millionths'] // 10**6 + 1000
    final = l3.rpc.decode(inv['bolt11'])['min_final_cltv_expiry'] + 1
    # Private channels: l2 only forwards via its own alias for l2->l3.
    route = [{'id': l2.info['id'], 'channel': ch12['short_channel_id'],
              'amount_msat': PAY_MSAT + fee,
              'delay': final + ch23['updates']['local']['cltv_expiry_delta']},
             {'id': l3.info['id'], 'channel': ch23['alias']['local'],
              'amount_msat': PAY_MSAT, 'delay': final}]
    l1.rpc.sendpay(route, inv['payment_hash'],
                   payment_secret=inv['payment_secret'],
                   amount_msat=PAY_MSAT)
    return l1.rpc.waitsendpay(inv['payment_hash'])


def asset_invoice(node, label, asset):
    return node.rpc.call('invoice', {'amount_msat': PAY_MSAT, 'label': label,
                                     'description': label, 'asset': asset})


def settled(*nodes):
    for n in nodes:
        wait_for(lambda: all(c['htlcs'] == []
                             for c in n.rpc.listpeerchannels()['channels']))


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
def test_liquid_asset_channel_poc(node_factory, bitcoind):
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2, l3 = node_factory.get_nodes(3)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)

    # Each hop gets an ordinary L-BTC channel *and* an asset channel.
    lbtc_funding, asset_funding = {}, {}
    for opener, peer in ((l1, l2), (l2, l3)):
        opener.fundwallet(10**7)
        lbtc_funding[opener] = opener.rpc.fundchannel(peer.info['id'],
                                                      LBTC_CHANNEL_SAT,
                                                      announce=False)['txid']
        txid = fund_asset_channel(bitcoind, opener, peer, asset, lbtc)
        asset_funding[opener] = txid
        tx, _ = outputs_by_asset(bitcoind, txid)
        # Asset output and L-BTC fee reserve, both to the channel's 2-of-2.
        assert tx['vout'][0]['asset'] == asset
        assert tx['vout'][1]['asset'] == lbtc
        assert tx['vout'][1]['scriptPubKey']['hex'] == tx['vout'][0]['scriptPubKey']['hex']

    bitcoind.generate_block(6)
    for n in (l1, l2, l3):
        wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL'
                             for c in n.rpc.listpeerchannels()['channels']))
    funding = {'lbtc12': lbtc_funding[l1], 'lbtc23': lbtc_funding[l2],
               'asset12': asset_funding[l1], 'asset23': asset_funding[l2]}

    # 1. Asset invoice, paid over the asset channels.
    inv = asset_invoice(l3, 'asset-ok', asset)
    assert pay_via(l1, l2, l3, funding['asset12'], funding['asset23'], inv)['status'] == 'complete'

    # 2. Ordinary invoice, paid over the L-BTC channels.
    inv = l3.rpc.invoice(PAY_MSAT, 'lbtc-ok', 'in L-BTC')
    assert pay_via(l1, l2, l3, funding['lbtc12'], funding['lbtc23'], inv)['status'] == 'complete'

    # 3. Asset invoice, paid in L-BTC: the receiver refuses.
    inv = asset_invoice(l3, 'asset-wrong', asset)
    with pytest.raises(RpcError, match=r'WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS'):
        pay_via(l1, l2, l3, funding['lbtc12'], funding['lbtc23'], inv)
    l3.daemon.wait_for_log('arrived in the wrong Liquid asset')

    # 4. In on the asset channel, out on the L-BTC channel: l2 refuses to
    #    swap them 1:1.
    inv = l3.rpc.invoice(PAY_MSAT, 'cross', 'in L-BTC')
    with pytest.raises(RpcError, match=r'WIRE_UNKNOWN_NEXT_PEER'):
        pay_via(l1, l2, l3, funding['asset12'], funding['lbtc23'], inv)
    l2.daemon.wait_for_log('Refusing to forward between different Liquid assets')

    settled(l1, l2, l3)
    # Only the two good payments moved funds: one on each kind of channel.
    for name, peer in (('asset12', l2), ('asset23', l3), ('lbtc12', l2), ('lbtc23', l3)):
        ch = channel_by_funding(peer, funding[name])
        assert ch['to_us_msat'] >= PAY_MSAT
        assert ch['to_us_msat'] < 2 * PAY_MSAT

    # Cooperative closes.  Asset channels spend both outputs and pay the fee
    # from the L-BTC reserve.
    for opener, peer, name in ((l1, l2, 'asset12'), (l2, l3, 'asset23'),
                               (l1, l2, 'lbtc12'), (l2, l3, 'lbtc23')):
        peer_balance = channel_by_funding(peer, funding[name])['to_us_msat'] // 1000
        res = opener.rpc.close(channel_by_funding(opener, funding[name])['channel_id'])
        assert res['type'] == 'mutual'
        # An externally-funded opener leaves broadcasting the close to the peer.
        wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
        txid = only_one(bitcoind.rpc.getrawmempool())
        blockhash = bitcoind.generate_block(1, wait_for_mempool=txid)[0]
        tx, units = outputs_by_asset(bitcoind, txid, blockhash)

        if name.startswith('asset'):
            assert len(tx['vin']) == 2
            assert sum(units[asset]) == CHANNEL_UNITS
            assert peer_balance in units[asset]
            assert sum(units[lbtc]) == FEE_RESERVE_SAT
        else:
            assert len(tx['vin']) == 1
            assert asset not in units
            assert peer_balance in units[lbtc]

    # Nobody crashed processing the closes.
    bitcoind.generate_block(1)
    for n in (l1, l2, l3):
        n.rpc.getinfo()
