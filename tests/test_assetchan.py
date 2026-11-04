"""Asset channels with the contrib/assetchan plugin (DESIGN.md section 6,
steps 4 and 5): A-B-C on liquid-regtest, asset channels A-B and B-C funded
by elementsd, an L-BTC channel A-B alongside, an asset invoice on C paid from
A, a wrong-asset payment refused, a mutual close that lands the asset at the
elementsd close addresses; and a unilateral close by either side, each
sweeping its asset output with L-BTC from its wallet for the fee.

    TEST_NETWORK=liquid-regtest pytest tests/test_assetchan.py
"""
from decimal import Decimal
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from pyln.testing.utils import BITCOIND_CONFIG
from utils import only_one, sync_blockheight, wait_for, TEST_NETWORK

import os
import pytest
import unittest


PLUGIN = os.path.join(os.getcwd(), 'contrib/assetchan/assetchan.py')
CHANNEL_UNITS = 100_000     # asset base units in each asset channel
FEE_RESERVE = 2000          # L-BTC sats next to each asset channel
PAY_UNITS = 1000


def units(amount):
    """An elementsd decimal amount in base units."""
    return int(Decimal(str(amount)) * 10**8)


def wallet_units(bitcoind, asset):
    """Units of asset in elementsd's wallet."""
    return sum(units(u['amount']) for u in bitcoind.rpc.listunspent(0)
               if u['asset'] == asset)


def channel(node, channel_id):
    return only_one([c for c in node.rpc.listpeerchannels()['channels']
                     if c['channel_id'] == channel_id])


def wait_normal(node, channel_id):
    wait_for(lambda: channel(node, channel_id)['state'] == 'CHANNELD_NORMAL')


def settled(*nodes):
    for n in nodes:
        wait_for(lambda: all(c['htlcs'] == []
                             for c in n.rpc.listpeerchannels()['channels']))


def wallet_sats(node):
    """L-BTC sats in the node's CLN wallet."""
    return sum(o['amount_msat'] for o in node.rpc.listfunds()['outputs']) // 1000


def node_opts(bitcoind, asset):
    """Options for a node with the plugin, accepting channels in asset."""
    # listconfigs conceals bitcoin-rpcpassword, and the nodes' bitcoin-rpcport
    # is a proxy: point the plugin straight at elementsd.
    elements_rpc = 'http://{rpcuser}:{rpcpassword}@127.0.0.1:{port}'.format(
        port=bitcoind.rpcport, **BITCOIND_CONFIG)
    return {'plugin': PLUGIN, 'channel-asset': asset, 'assetchan-elements-rpc': elements_rpc,
            # Whole-unit routing fees keep channel balances in whole units
            # (regtest's defaults are 1 msat + 10 ppm).
            'fee-base': 1000, 'fee-per-satoshi': 0}


def mine_spends_of(bitcoind, nodes, txid, want, maxblocks=30):
    """Mine block by block (letting the nodes react to each) until `want`
    txs spending txid got mined; return them by txid."""
    mined = {}
    for _ in range(maxblocks):
        sync_blockheight(bitcoind, nodes)
        blockhash = bitcoind.generate_block(1)[0]
        for tx in bitcoind.rpc.getblock(blockhash, 2)['tx']:
            if any(vin.get('txid') == txid for vin in tx['vin']):
                mined[tx['txid']] = bitcoind.rpc.getrawtransaction(tx['txid'], True, blockhash)
        if len(mined) >= want:
            return mined
    raise ValueError("only {} spends of {} after {} blocks".format(len(mined), txid, maxblocks))


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_open_pay_close(node_factory, bitcoind):
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2, l3 = node_factory.get_nodes(3, opts=node_opts(bitcoind, asset))
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)

    # Asset channels l1-l2 and l2-l3, funded by elementsd's wallet (one at a
    # time, so the second can spend the first one's change).
    funding = {}
    for opener, peer in ((l1, l2), (l2, l3)):
        res = opener.rpc.call('assetchan-open', {'id': peer.info['id'], 'amount': CHANNEL_UNITS,
                                                 'asset': asset, 'fee_reserve': FEE_RESERVE})
        assert res['asset'] == asset and res['fee_reserve'] == FEE_RESERVE
        # The asset to the 2-of-2 at output n, the L-BTC fee reserve at n+1.
        vout = bitcoind.rpc.getrawtransaction(res['txid'], True)['vout']
        out = res['outnum']
        assert vout[out]['asset'] == asset and units(vout[out]['value']) == CHANNEL_UNITS
        assert vout[out + 1]['asset'] == lbtc and units(vout[out + 1]['value']) == FEE_RESERVE
        assert vout[out + 1]['scriptPubKey']['hex'] == vout[out]['scriptPubKey']['hex']
        bitcoind.generate_block(1, wait_for_mempool=res['txid'])
        funding[opener] = res['channel_id']
    bitcoind.generate_block(5)
    for opener, peer in ((l1, l2), (l2, l3)):
        wait_normal(opener, funding[opener])
        wait_normal(peer, funding[opener])

    ch12 = channel(l1, funding[l1])
    assert ch12['asset'] == asset
    assert ch12['fee_reserve_msat'] == FEE_RESERVE * 1000
    # Both sides close to elementsd's wallet: close_to at open, and the
    # openchannel hook on the other side.
    for node in (l1, l2):
        info = bitcoind.rpc.getaddressinfo(channel(node, funding[l1])['close_to_addr'])
        assert info['ismine']

    # An ordinary L-BTC channel l1-l2 alongside.
    l1.fundwallet(10**7)
    lbtc12 = l1.rpc.fundchannel(l2.info['id'], 10**6, announce=False)['channel_id']
    bitcoind.generate_block(6, wait_for_mempool=1)
    wait_normal(l1, lbtc12)
    wait_normal(l2, lbtc12)
    assert 'asset' not in channel(l1, lbtc12)
    listed = l1.rpc.call('assetchan-list', {})['channels']
    assert [c['channel_id'] for c in listed] == [funding[l1]]
    assert only_one(listed)['fee_reserve'] == FEE_RESERVE
    assert len(l2.rpc.call('assetchan-list', {'asset': asset})['channels']) == 2
    # Routes' CLTVs count from l1's height: l3 mustn't be ahead of it.
    sync_blockheight(bitcoind, [l1, l2, l3])

    # 1. An invoice in the asset on l3, paid from l1 over the asset channels
    #    (the l2-l3 hop is private: it comes from the invoice's route hint).
    wait_for(lambda: [i for i in l3.rpc.listincoming()['incoming']
                      if i['id'] == l2.info['id']])
    inv = l3.rpc.call('assetchan-invoice', {'amount': PAY_UNITS, 'asset': asset,
                                            'label': 'asset-ok', 'description': 'in the asset'})
    assert inv['asset'] == asset
    pay = l1.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': asset})
    assert pay['amount_msat'] == PAY_UNITS * 1000
    assert only_one(l3.rpc.listinvoices('asset-ok')['invoices'])['status'] == 'paid'
    settled(l1, l2, l3)
    # It went over the asset channels (l2 charged its base fee, 1 unit), not
    # the L-BTC one.
    assert channel(l2, funding[l1])['to_us_msat'] == (PAY_UNITS + 1) * 1000
    assert channel(l3, funding[l2])['to_us_msat'] == PAY_UNITS * 1000
    assert channel(l1, lbtc12)['to_us_msat'] == 10**6 * 1000
    # Paid: the plugin drops its note of the invoice's asset.
    wait_for(lambda: l3.rpc.listdatastore(
        key=['assetchan', 'invoice', inv['payment_hash']])['datastore'] == [])

    # 2. An ordinary (L-BTC) invoice on l3 can't be paid in the asset: there
    #    is no L-BTC path to l3, and l3 refuses an HTLC that arrives on an
    #    asset channel.
    inv = l3.rpc.invoice(1000, 'lbtc-wrong', 'in L-BTC',
                         exposeprivatechannels=[channel(l3, funding[l2])['short_channel_id']])
    with pytest.raises(RpcError, match='incorrect_or_unknown_payment_details'):
        l1.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': asset})
    l3.daemon.wait_for_log('arrived in {} but the invoice is in lbtc'.format(asset))
    assert only_one(l3.rpc.listinvoices('lbtc-wrong')['invoices'])['status'] == 'unpaid'
    settled(l1, l2, l3)

    # 3. Mutual close of the l1-l2 asset channel: each side's asset goes to
    #    its elementsd close address, the L-BTC reserve pays the fee.
    before = wallet_units(bitcoind, asset)
    l1_units = channel(l1, funding[l1])['to_us_msat'] // 1000
    l2_units = channel(l2, funding[l1])['to_us_msat'] // 1000
    assert l1_units + l2_units == CHANNEL_UNITS
    res = l1.rpc.call('assetchan-close', {'id': funding[l1]})
    assert res['type'] == 'mutual'
    close_txid = only_one(res['txids'])
    blockhash = bitcoind.generate_block(1, wait_for_mempool=close_txid)[0]
    for node in (l1, l2):
        wait_for(lambda: channel(node, funding[l1])['state'] == 'ONCHAIN')
    tx = bitcoind.rpc.getrawtransaction(close_txid, True, blockhash)
    asset_outs = [v for v in tx['vout'] if v.get('asset') == asset]
    assert sorted(units(v['value']) for v in asset_outs) == sorted([l1_units, l2_units])
    close_to = {channel(node, funding[l1])['close_to_addr'] for node in (l1, l2)}
    assert {v['scriptPubKey']['address'] for v in asset_outs} == close_to
    assert sum(units(v['value']) for v in tx['vout'] if v.get('asset') == lbtc) == FEE_RESERVE
    assert wallet_units(bitcoind, asset) == before + CHANNEL_UNITS

    # The elementsd side of the plugin: an address, a send, the balances.
    addr = l1.rpc.call('assetchan-newaddr', {})['address']
    txid = l1.rpc.call('assetchan-send', {'addr': addr, 'asset': asset, 'amount': 10})['txid']
    vout = bitcoind.rpc.getrawtransaction(txid, True)['vout']
    assert any(v.get('asset') == asset and units(v['value']) == 10
               and v['scriptPubKey']['address'] == addr for v in vout)
    bitcoind.generate_block(1, wait_for_mempool=txid)
    funds = l1.rpc.call('assetchan-funds', {})
    assert funds['elements'][asset] == wallet_units(bitcoind, asset)
    assert [c['asset'] for c in funds['channels'] if c['state'] == 'CHANNELD_NORMAL'] == ['lbtc']

    # Nobody crashed processing all that.
    bitcoind.generate_block(1)
    for n in (l1, l2, l3):
        n.rpc.getinfo()


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
@pytest.mark.parametrize("closer", ["opener", "peer"])
def test_unilateral_close(node_factory, bitcoind, closer):
    """Force-close an asset channel (no HTLC in flight).  The commitment
    spends both funding outputs, the asset and the L-BTC fee reserve, which
    pays its anchors and fee.  Then each side sweeps its asset output to its
    elementsd close address, paying the L-BTC fee from its CLN wallet."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts=node_opts(bitcoind, asset))
    # Both need some L-BTC in their CLN wallets: sweeps pay fees with it.
    l1.fundwallet(10**6)
    l2.fundwallet(10**6)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    res = l1.rpc.call('assetchan-open', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                         'asset': asset, 'fee_reserve': FEE_RESERVE})
    funding_txid, outnum, chan = res['txid'], res['outnum'], res['channel_id']
    bitcoind.generate_block(6, wait_for_mempool=funding_txid)
    wait_normal(l1, chan)
    wait_normal(l2, chan)
    close_to = {n: channel(n, chan)['close_to_addr'] for n in (l1, l2)}
    assert all(bitcoind.rpc.getaddressinfo(a)['ismine'] for a in close_to.values())

    # Give l2 a balance, and wait until nothing is in flight.
    wait_for(lambda: [i for i in l2.rpc.listincoming()['incoming']
                      if i['id'] == l1.info['id']])
    inv = l2.rpc.call('assetchan-invoice', {'amount': PAY_UNITS, 'asset': asset,
                                            'label': 'balance', 'description': 'balance'})
    l1.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': asset})
    settled(l1, l2)
    want_units = {l1: CHANNEL_UNITS - PAY_UNITS, l2: PAY_UNITS}
    wallet_before = {n: wallet_sats(n) for n in (l1, l2)}
    elements_before = wallet_units(bitcoind, asset)

    # The closer's peer is offline, so `close` goes unilateral.
    closing, other = (l1, l2) if closer == 'opener' else (l2, l1)
    other.stop()
    res = closing.rpc.close(chan, unilateraltimeout=1)
    assert res['type'] == 'unilateral'
    commit_txid = only_one(res['txids'])
    blockhash = bitcoind.generate_block(1, wait_for_mempool=commit_txid)[0]
    commit = bitcoind.rpc.getrawtransaction(commit_txid, True, blockhash)
    # It spends the asset funding output and the L-BTC fee reserve: the
    # balances in the asset, two 330 sat anchors and the fee in L-BTC.
    assert sorted((vin['txid'], vin['vout']) for vin in commit['vin']) == \
        [(funding_txid, outnum), (funding_txid, outnum + 1)]
    asset_outs = [v for v in commit['vout'] if v.get('asset') == asset]
    assert sorted(units(v['value']) for v in asset_outs) == sorted(want_units.values())
    lbtc_outs = [v for v in commit['vout'] if v.get('asset') == lbtc]
    assert sorted(units(v['value']) for v in lbtc_outs) == [330, 330, FEE_RESERVE - 660]
    assert units(commit['fee'][lbtc]) == FEE_RESERVE - 660

    # The other side comes back and sweeps its to_remote (1 block CSV); the
    # closer its to_local once its CSV delay is over.
    other.start()
    sweeps = mine_spends_of(bitcoind, [l1, l2], commit_txid, 2)
    by_node = {}
    for tx in sweeps.values():
        asset_outs = [v for v in tx['vout'] if v.get('asset') == asset]
        node = only_one([n for n in (l1, l2)
                         if only_one(asset_outs)['scriptPubKey']['address'] == close_to[n]])
        by_node[node] = tx
        # Input 0 is the asset output, in full to the close address; the
        # fee is L-BTC from the node's wallet, the rest of which comes back.
        assert units(asset_outs[0]['value']) == want_units[node]
        assert tx['vin'][0]['txid'] == commit_txid
        assert commit['vout'][tx['vin'][0]['vout']]['asset'] == asset
        assert all(vin['txid'] != commit_txid for vin in tx['vin'][1:])
        assert len(tx['vin']) >= 2
        assert units(tx['fee'][lbtc]) > 0
        assert [v for v in tx['vout']
                if v.get('asset') == lbtc and v['scriptPubKey']['type'] != 'fee']
    assert set(by_node) == {l1, l2}
    to_self_delay = channel(closing, chan)['our_to_self_delay']
    assert by_node[closing]['vin'][0]['sequence'] == to_self_delay
    assert by_node[other]['vin'][0]['sequence'] == 1

    # The assets are in elementsd's wallet; each node spent a little L-BTC.
    assert wallet_units(bitcoind, asset) == elements_before + CHANNEL_UNITS
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    for n in (l1, l2):
        assert wallet_before[n] - 5000 < wallet_sats(n) < wallet_before[n]

    # Bookkeeper: the channel accounts (in asset units) close out at zero.
    for n in (l1, l2):
        wait_for(lambda: any(a['account'] == chan and a['balances'][0]['balance_msat'] == 0
                             for a in n.rpc.call('bkpr-listbalances', {})['accounts']))

    # Everything resolved; nobody logged anything broken.
    bitcoind.generate_block(100)
    for n in (l1, l2):
        n.daemon.wait_for_log('onchaind complete, forgetting peer')
