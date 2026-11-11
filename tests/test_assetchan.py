"""Asset channels with the contrib/assetchan plugin (DESIGN.md section 6,
steps 4 and 5): A-B-C on liquid-regtest, asset channels A-B and B-C funded
by elementsd, an L-BTC channel A-B alongside, an asset invoice on C paid from
A, a wrong-asset payment refused, a mutual close that lands the asset at the
elementsd close addresses; a unilateral close by either side, each sweeping
its asset output with L-BTC from elementsd's wallet for the fee; a unilateral close
with an HTLC in flight, resolved on-chain by HTLC txs or direct spends; and
a revoked commitment, penalized by the victim.  test_gossip (step 7): five
nodes with announced and private asset channels next to L-BTC ones, what
gossip makes of them and what payers with and without the plugin do.

    TEST_NETWORK=liquid-regtest pytest tests/test_assetchan.py
"""
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from pyln.testing.utils import BITCOIND_CONFIG
from utils import only_one, sync_blockheight, wait_for, TEST_NETWORK

import os
import pytest
import sys
import unittest


PLUGIN_DIR = os.path.join(os.getcwd(), 'contrib/assetchan')
PLUGIN = os.path.join(PLUGIN_DIR, 'assetchan.py')
CHANNEL_UNITS = 100_000     # asset base units in each asset channel
FEE_RESERVE = 2000          # L-BTC sats next to each asset channel
PAY_UNITS = 1000
HTLC_UNITS = 2000           # the HTLC the on-chain tests leave in flight

sys.path.insert(0, PLUGIN_DIR)
from elements import base_units as units  # noqa: E402  (elementsd decimal -> base units)


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
    return {'plugin': PLUGIN, 'assetchan-asset': asset, 'assetchan-elements-rpc': elements_rpc,
            # Whole-unit routing fees keep channel balances in whole units
            # (regtest's defaults are 1 msat + 10 ppm).
            'fee-base': 1000, 'fee-per-satoshi': 0}


def issue_asset(bitcoind):
    """A new asset, confirmed.  (elementsd's wallet learns of the block the
    fixture just mined asynchronously; the issuance needs its coinbase.)"""
    bitcoind.rpc.syncwithvalidationinterfacequeue()
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)
    return asset


def wallet_tx_vout(bitcoind, txid):
    """Outputs of a tx of elementsd's wallet, mined or not."""
    return bitcoind.rpc.decoderawtransaction(bitcoind.rpc.gettransaction(txid)['hex'])['vout']


def mine_spends_of(bitcoind, nodes, txid, want, maxblocks=30, since=None, vout=None):
    """Mine block by block (letting the nodes react to each) until `want`
    txs spending txid (its output vout, if given) got mined, counting those in
    blocks from height `since` on too; return them by txid."""
    mined = {}

    def scan(blockhash):
        for tx in bitcoind.rpc.getblock(blockhash, 2)['tx']:
            if any(vin.get('txid') == txid and vout in (None, vin.get('vout'))
                   for vin in tx['vin']):
                mined[tx['txid']] = bitcoind.rpc.getrawtransaction(tx['txid'], True, blockhash)

    if since is not None:
        for height in range(since, bitcoind.rpc.getblockcount() + 1):
            scan(bitcoind.rpc.getblockhash(height))
    for _ in range(maxblocks):
        if len(mined) >= want:
            break
        sync_blockheight(bitcoind, nodes)
        scan(bitcoind.generate_block(1)[0])
    if len(mined) < want:
        raise ValueError("only {} spends of {} after {} blocks".format(len(mined), txid, maxblocks))
    return mined


def spends_from(tx, txid):
    """(input index, output index) for each input of tx spending txid."""
    return [(i, vin['vout']) for i, vin in enumerate(tx['vin']) if vin.get('txid') == txid]


def close_units(bitcoind, asset, address):
    """Units of asset at address, in elementsd's wallet."""
    return sum(units(u['amount']) for u in bitcoind.rpc.listunspent(0)
               if u['asset'] == asset and u['address'] == address)


def paid_by_elementsd(bitcoind, tx):
    """A sweep of an asset output (input 0): the plugin paid its L-BTC fee
    with elementsd's coins, not the node's CLN wallet."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    assert len(tx['vin']) >= 2 and units(tx['fee'][lbtc]) > 0
    for vin in tx['vin'][1:]:
        prev = bitcoind.rpc.decoderawtransaction(
            bitcoind.rpc.gettransaction(vin['txid'])['hex'])['vout'][vin['vout']]
        assert prev['asset'] == lbtc
        assert bitcoind.rpc.getaddressinfo(prev['scriptPubKey']['address'])['ismine']


def open_asset_channel(bitcoind, l1, l2, asset):
    """l1 opens an asset channel to l2, funded by elementsd.  Returns
    assetchan-open's result once the channel is usable."""
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    res = l1.rpc.call('assetchan-open', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                         'asset': asset, 'fee_reserve': FEE_RESERVE})
    chan = res['channel_id']
    bitcoind.generate_block(6, wait_for_mempool=res['txid'])
    wait_normal(l1, chan)
    wait_normal(l2, chan)
    # Both sides close to elementsd's wallet: close_to at open, and the
    # openchannel hook on the other side.
    for n in (l1, l2):
        assert bitcoind.rpc.getaddressinfo(channel(n, chan)['close_to_addr'])['ismine']
    return res


def pay(l1, l2, asset, amount, label):
    """l1 pays an invoice of l2 for amount units, through the plugin."""
    wait_for(lambda: [i for i in l2.rpc.listincoming()['incoming']
                      if i['id'] == l1.info['id']])
    inv = l2.rpc.call('assetchan-invoice', {'amount': amount, 'asset': asset,
                                            'label': label, 'description': label})
    l1.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': asset})
    settled(l1, l2)


def send_htlc(payer, payee, chan, amount, payment_hash, payment_secret):
    """A single-hop sendpay of amount units over chan; doesn't wait."""
    route = [{'id': payee.info['id'], 'channel': channel(payer, chan)['short_channel_id'],
              'amount_msat': amount * 1000, 'delay': 20}]
    payer.rpc.sendpay(route, payment_hash, payment_secret=payment_secret,
                      amount_msat=amount * 1000)


def payment_result(bitcoind, payer, payment_hash, nodes):
    """The sendpay status once the chain decided it, mining as needed."""
    def status():
        return only_one(payer.rpc.listsendpays(payment_hash=payment_hash)['payments'])['status']
    for _ in range(20):
        try:
            wait_for(lambda: status() != 'pending', timeout=3)
            return status()
        except ValueError:
            bitcoind.generate_block(1)
            sync_blockheight(bitcoind, nodes)
    raise ValueError('payment {} still pending'.format(payment_hash))


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_open_pay_close(node_factory, bitcoind):
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = issue_asset(bitcoind)

    l1, l2, l3 = node_factory.get_nodes(3, opts=node_opts(bitcoind, asset))

    # Asset channels l1-l2 and l2-l3, funded by elementsd's wallet (one at a
    # time, so the second can spend the first one's change).
    funding = {}
    for opener, peer in ((l1, l2), (l2, l3)):
        res = open_asset_channel(bitcoind, opener, peer, asset)
        assert res['asset'] == asset and res['fee_reserve'] == FEE_RESERVE
        # The asset to the 2-of-2 at output n, the L-BTC fee reserve at n+1.
        vout = wallet_tx_vout(bitcoind, res['txid'])
        out = res['outnum']
        assert vout[out]['asset'] == asset and units(vout[out]['value']) == CHANNEL_UNITS
        assert vout[out + 1]['asset'] == lbtc and units(vout[out + 1]['value']) == FEE_RESERVE
        assert vout[out + 1]['scriptPubKey']['hex'] == vout[out]['scriptPubKey']['hex']
        funding[opener] = res['channel_id']

    ch12 = channel(l1, funding[l1])
    assert ch12['asset'] == asset
    assert ch12['fee_reserve_msat'] == FEE_RESERVE * 1000

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
    #    l3 fails the HTLC as a temporary failure of the channel it came by
    #    (so a payer with other paths would try them); here there is none.
    with pytest.raises(RpcError, match='Failed after'):
        l1.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': asset})
    l3.daemon.wait_for_log('arrived in {} but the invoice is in lbtc'.format(asset))
    l1.daemon.wait_for_log('Unexpected error .* from final node: disabling')
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
    vout = wallet_tx_vout(bitcoind, txid)
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
    elementsd close address, the plugin paying the L-BTC fee from
    elementsd's wallet."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = issue_asset(bitcoind)

    l1, l2 = node_factory.get_nodes(2, opts=node_opts(bitcoind, asset))
    res = open_asset_channel(bitcoind, l1, l2, asset)
    funding_txid, outnum, chan = res['txid'], res['outnum'], res['channel_id']
    close_to = {n: channel(n, chan)['close_to_addr'] for n in (l1, l2)}

    # Give l2 a balance.
    pay(l1, l2, asset, PAY_UNITS, 'balance')
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
        # fee is L-BTC from elementsd's wallet, the rest of which comes back.
        assert units(asset_outs[0]['value']) == want_units[node]
        assert tx['vin'][0]['txid'] == commit_txid
        assert commit['vout'][tx['vin'][0]['vout']]['asset'] == asset
        assert all(vin['txid'] != commit_txid for vin in tx['vin'][1:])
        paid_by_elementsd(bitcoind, tx)
    assert set(by_node) == {l1, l2}
    to_self_delay = channel(closing, chan)['our_to_self_delay']
    assert by_node[closing]['vin'][0]['sequence'] == to_self_delay
    assert by_node[other]['vin'][0]['sequence'] == 1

    # The assets are in elementsd's wallet; the nodes' wallets paid nothing.
    assert wallet_units(bitcoind, asset) == elements_before + CHANNEL_UNITS
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    for n in (l1, l2):
        assert wallet_sats(n) == wallet_before[n]

    # Bookkeeper: the channel accounts (in asset units) close out at zero.
    for n in (l1, l2):
        wait_for(lambda: any(a['account'] == chan and a['balances'][0]['balance_msat'] == 0
                             for a in n.rpc.call('bkpr-listbalances', {})['accounts']))

    # Everything resolved; nobody logged anything broken.
    bitcoind.generate_block(100)
    for n in (l1, l2):
        n.daemon.wait_for_log('onchaind complete, forgetting peer')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
@pytest.mark.parametrize("closer", ["payer", "payee"])
@pytest.mark.parametrize("outcome", ["timeout", "success"])
def test_htlc_onchain(node_factory, bitcoind, closer, outcome):
    """Force-close an asset channel with an HTLC in flight from l1 (the
    opener) to l2.  HTLC outputs are in the asset: HTLC txs pay no fee and
    whoever broadcasts one adds L-BTC (from elementsd), as for a sweep of an
    HTLC output of the peer's commitment.  Depending on who closes and
    whether l2 knows the preimage, the HTLC is resolved by:

    - timeout, payer closes: l1's HTLC-timeout tx, then its delayed output;
    - timeout, payee closes: l1 takes the timed-out HTLC directly;
    - success, payee closes: l2's HTLC-success tx, then its delayed output;
    - success, payer closes: l2 takes it directly, with the preimage.

    l1 learns the outcome from the chain, so the payment fails or succeeds."""
    asset = issue_asset(bitcoind)

    # l2 hangs up instead of failing / fulfilling the HTLC (the second one:
    # the first gives l2 a balance), so it stays in flight.
    disconnect = (['-WIRE_UPDATE_FAIL_HTLC'] if outcome == 'timeout'
                  else ['-WIRE_UPDATE_FULFILL_HTLC*2'])
    opts = node_opts(bitcoind, asset)
    l1, l2 = node_factory.get_nodes(2, opts=[opts, {**opts, 'disconnect': disconnect}])
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']
    pay(l1, l2, asset, PAY_UNITS, 'balance')
    close_to = {n: channel(n, chan)['close_to_addr'] for n in (l1, l2)}
    wallet_before = {n: wallet_sats(n) for n in (l1, l2)}

    if outcome == 'timeout':
        # l2 doesn't know this payment.
        payment_hash = payment_secret = '01' * 32
    else:
        inv = l2.rpc.call('assetchan-invoice', {'amount': HTLC_UNITS, 'asset': asset,
                                                'label': 'in-flight', 'description': 'in flight'})
        payment_hash, payment_secret = inv['payment_hash'], inv['payment_secret']
    send_htlc(l1, l2, chan, HTLC_UNITS, payment_hash, payment_secret)
    l2.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_')
    for n in (l1, l2):
        wait_for(lambda: not channel(n, chan)['peer_connected'])
    cltv_expiry = only_one(channel(l1, chan)['htlcs'])['expiry']

    closing, other = (l1, l2) if closer == 'payer' else (l2, l1)
    res = closing.rpc.close(chan, unilateraltimeout=1)
    assert res['type'] == 'unilateral'
    commit_txid = only_one(res['txids'])
    blockhash = bitcoind.generate_block(1, wait_for_mempool=commit_txid)[0]
    commit = bitcoind.rpc.getrawtransaction(commit_txid, True, blockhash)
    asset_outs = {v['n']: units(v['value']) for v in commit['vout'] if v.get('asset') == asset}
    assert sorted(asset_outs.values()) == sorted([CHANNEL_UNITS - PAY_UNITS - HTLC_UNITS,
                                                  PAY_UNITS, HTLC_UNITS])
    htlc_n = only_one([n for n, u in asset_outs.items() if u == HTLC_UNITS])

    # Every asset output of the commitment gets spent: to_local by the
    # closer after its delay and to_remote by the other side, as in
    # test_unilateral_close, and the HTLC by whoever wins it.  Each spend
    # takes one output, as its input 0, and pays L-BTC from elementsd.
    first_block = bitcoind.rpc.getblockcount() + 1
    spends = mine_spends_of(bitcoind, [l1, l2], commit_txid, 3, maxblocks=40)
    for tx in spends.values():
        assert [i for i, _ in spends_from(tx, commit_txid)] == [0]
        paid_by_elementsd(bitcoind, tx)
    htlc_spend = only_one([tx for tx in spends.values()
                           if spends_from(tx, commit_txid) == [(0, htlc_n)]])
    witness = htlc_spend['vin'][0]['txinwitness']
    winner = l2 if outcome == 'success' else l1
    if (outcome == 'timeout') == (closer == 'payer'):
        # The closer's HTLC tx, pre-signed by the peer: `0 <remotehtlcsig>
        # <localhtlcsig> <preimage or empty> <wscript>`.  Both signatures
        # are SIGHASH_SINGLE|ANYONECANPAY: they fix only its input 0 and
        # output 0, the HTLC in the asset; the broadcaster adds the L-BTC.
        assert closing == winner
        assert len(witness) == 5
        assert witness[1][-2:] == witness[2][-2:] == '83'
        assert len(witness[3]) == (64 if outcome == 'success' else 0)
        assert htlc_spend['vout'][0]['asset'] == asset
        assert units(htlc_spend['vout'][0]['value']) == HTLC_UNITS
        assert htlc_spend['vin'][0]['sequence'] == 1
        assert htlc_spend['locktime'] == (cltv_expiry if outcome == 'timeout' else 0)
        # After the delay, its output goes to the winner's close address
        # (the other inputs are wallet L-BTC, maybe the HTLC tx's change).
        # (maybe while the other spends were still being waited for).
        # (Its L-BTC change may fund another sweep: we want output 0's.)
        sweep = only_one(list(mine_spends_of(bitcoind, [l1, l2], htlc_spend['txid'], 1,
                                             since=first_block, vout=0).values()))
        assert spends_from(sweep, htlc_spend['txid'])[0] == (0, 0)
        assert sweep['vin'][0]['sequence'] == channel(winner, chan)['our_to_self_delay']
        paid_by_elementsd(bitcoind, sweep)
        final = sweep
    else:
        # Taken directly from the peer's commitment: `<sig> <preimage or
        # empty> <wscript>`, after the 1-block CSV of anchor channels.
        assert len(witness) == 3
        assert len(witness[1]) == (64 if outcome == 'success' else 0)
        assert htlc_spend['vin'][0]['sequence'] == 1
        assert htlc_spend['locktime'] == (cltv_expiry if outcome == 'timeout' else 0)
        final = htlc_spend
    asset_out = only_one([v for v in final['vout'] if v.get('asset') == asset])
    assert asset_out['scriptPubKey']['address'] == close_to[winner]
    assert units(asset_out['value']) == HTLC_UNITS

    # The payment's outcome came from the chain.
    status = payment_result(bitcoind, l1, payment_hash, [l1, l2])
    if outcome == 'timeout':
        assert status == 'failed'
        with pytest.raises(RpcError, match='timed out'):
            l1.rpc.waitsendpay(payment_hash)
    else:
        assert status == 'complete'
        assert only_one(l2.rpc.listinvoices('in-flight')['invoices'])['status'] == 'paid'

    # Each side's asset is at its close address: l2 its balance, plus the
    # HTLC if it succeeded, and l1 the rest.  L-BTC only paid fees.
    l2_units = PAY_UNITS + (HTLC_UNITS if outcome == 'success' else 0)
    assert close_units(bitcoind, asset, close_to[l1]) == CHANNEL_UNITS - l2_units
    assert close_units(bitcoind, asset, close_to[l2]) == l2_units
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    for n in (l1, l2):
        assert wallet_sats(n) == wallet_before[n]

    bitcoind.generate_block(100)
    for n in (l1, l2):
        n.daemon.wait_for_log('onchaind complete, forgetting peer')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
@pytest.mark.parametrize("cheater", ["opener", "peer"])
def test_penalty(node_factory, bitcoind, cheater):
    """The cheater broadcasts a revoked commitment of an asset channel, with
    an HTLC from it to the victim in it.  The victim takes everything to its
    elementsd close address: the cheater's to_local and the HTLC with the
    revocation key, and its own to_remote, each in a sweep paid with L-BTC
    from elementsd."""
    asset = issue_asset(bitcoind)

    # Identical feerates: no gratuitous commitments to update them.
    opts = {**node_opts(bitcoind, asset), 'may_reconnect': True,
            'feerates': (7500, 7500, 7500, 7500)}
    l1, l2 = node_factory.get_nodes(2, opts=opts)
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']
    bad, victim = (l1, l2) if cheater == 'opener' else (l2, l1)
    # Both get a balance.
    pay(l1, l2, asset, 10 * PAY_UNITS, 'balance')

    # From the next commitment on, commitments stop: the cheater's HTLC
    # gets added, not fulfilled.
    for n in (l1, l2):
        n.stop()
        n.daemon.opts['dev-disable-commit-after'] = 1
        n.start()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    for n in (l1, l2):
        wait_for(lambda: channel(n, chan)['peer_connected'])
        wait_normal(n, chan)
    inv = victim.rpc.call('assetchan-invoice', {'amount': HTLC_UNITS, 'asset': asset,
                                                'label': 'cheat', 'description': 'cheat'})
    send_htlc(bad, victim, chan, HTLC_UNITS, inv['payment_hash'], inv['payment_secret'])
    for n in (l1, l2):
        n.daemon.wait_for_log('dev-disable-commit-after: disabling')
    # The cheater got the victim's commitment_signed with the HTLC: keep
    # that commitment.
    bad.daemon.wait_for_log('got commitsig')
    cheat = bad.rpc.dev_sign_last_tx(victim.info['id'])['tx']

    bad.rpc.dev_reenable_commit(victim.info['id'])
    victim.rpc.dev_reenable_commit(bad.info['id'])
    assert bad.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    # On the revoked commitment: the cheater's balance (the HTLC already
    # taken from it), the HTLC, and the victim's balance before it.
    bad_units = channel(bad, chan)['to_us_msat'] // 1000
    victim_units = channel(victim, chan)['to_us_msat'] // 1000 - HTLC_UNITS
    close_to = channel(victim, chan)['close_to_addr']
    wallet_before = wallet_sats(victim)
    elements_before = wallet_units(bitcoind, asset)

    bad.stop()
    commit_txid = bitcoind.rpc.sendrawtransaction(cheat)
    blockhash = bitcoind.generate_block(1, wait_for_mempool=commit_txid)[0]
    commit = bitcoind.rpc.getrawtransaction(commit_txid, True, blockhash)
    asset_outs = {v['n']: units(v['value']) for v in commit['vout'] if v.get('asset') == asset}
    assert sorted(asset_outs.values()) == sorted([bad_units, HTLC_UNITS, victim_units])
    victim.daemon.wait_for_logs([
        'Telling lightningd about OUR_PENALTY_TX to resolve'
        ' THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM',
        'Telling lightningd about OUR_PENALTY_TX to resolve'
        ' THEIR_REVOKED_UNILATERAL/THEIR_HTLC'])

    # Each output is taken in a sweep of its own, as its input 0, to the
    # victim's close address: the cheater's to_local and the HTLC with
    # the revocation key (`<sig> <1 or revocationpubkey> <wscript>`), the
    # victim's to_remote (`<sig> <wscript>`) after its 1-block CSV.
    spends = mine_spends_of(bitcoind, [victim], commit_txid, 3)
    witness = {}
    for tx in spends.values():
        (innum, n), = spends_from(tx, commit_txid)
        assert innum == 0
        paid_by_elementsd(bitcoind, tx)
        out = only_one([v for v in tx['vout'] if v.get('asset') == asset])
        assert out['scriptPubKey']['address'] == close_to
        assert units(out['value']) == asset_outs[n]
        witness[asset_outs[n]] = tx['vin'][0]['txinwitness']
    assert len(witness[bad_units]) == 3 and witness[bad_units][1] == '01'
    assert len(witness[HTLC_UNITS]) == 3 and len(witness[HTLC_UNITS][1]) == 66
    assert len(witness[victim_units]) == 2

    # All of the asset went to the victim; its CLN wallet paid nothing.
    assert close_units(bitcoind, asset, close_to) == CHANNEL_UNITS
    assert wallet_units(bitcoind, asset) == elements_before + CHANNEL_UNITS
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [victim])
    assert wallet_sats(victim) == wallet_before

    bitcoind.generate_block(100)
    victim.daemon.wait_for_log('onchaind complete, forgetting peer')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_gossip(node_factory, bitcoind):
    """Five nodes.  Asset channels l1-l2, l2-l3 and l3-l4 announced, l2-l4
    private; L-BTC channels l1-l5 and l5-l4 announced.  What gossip makes of
    asset channels, and what payers, with and without the plugin, do with
    that."""
    asset = issue_asset(bitcoind)
    # l5 charges 50 sat a forward (the others 1), see 3.
    opts = [node_opts(bitcoind, asset) for _ in range(5)]
    opts[4]['fee-base'] = 50000
    nodes = l1, l2, l3, l4, l5 = node_factory.get_nodes(5, opts=opts)
    for a, b in ((l1, l2), (l2, l3), (l3, l4), (l2, l4), (l1, l5), (l5, l4)):
        a.rpc.connect(b.info['id'], 'localhost', b.port)
    chans = {}
    for opener, peer, announce in ((l1, l2, True), (l2, l3, True), (l3, l4, True), (l2, l4, False)):
        res = opener.rpc.call('assetchan-open', {'id': peer.info['id'], 'amount': CHANNEL_UNITS,
                                                 'asset': asset, 'fee_reserve': FEE_RESERVE,
                                                 'announce': announce})
        bitcoind.generate_block(1, wait_for_mempool=res['txid'])
        chans[(opener, peer)] = res['channel_id']
    for opener, peer in ((l1, l5), (l5, l4)):
        opener.fundwallet(10**7)
        chans[(opener, peer)] = opener.rpc.fundchannel(peer.info['id'], 10**6)['channel_id']
        bitcoind.generate_block(1, wait_for_mempool=1)
    bitcoind.generate_block(6)
    for (opener, peer), chan in chans.items():
        wait_normal(opener, chan)
        wait_normal(peer, chan)
    sync_blockheight(bitcoind, nodes)

    def scid(pair):
        return channel(pair[0], chans[pair])['short_channel_id']

    def gossiped(node):
        return [c for c in node.rpc.listchannels()['channels'] if c['short_channel_id'] in public]

    # 1. Gossip.  The announced asset channels reach every node, l5 (which
    #    has none) included, looking like any other channel: their capacity
    #    is the asset units, and nothing says which asset.  The private one
    #    is nowhere.  The plugin tells them apart by the funding output.
    public = {scid(p) for p in ((l1, l2), (l2, l3), (l3, l4), (l1, l5), (l5, l4))}
    for n in nodes:
        wait_for(lambda: len(gossiped(n)) == 2 * len(public))
    seen = l5.rpc.listchannels(short_channel_id=scid((l1, l2)))['channels']
    assert len(seen) == 2
    assert all(c['amount_msat'] == CHANNEL_UNITS * 1000 and 'asset' not in c for c in seen)
    assert l5.rpc.listchannels(short_channel_id=scid((l2, l4)))['channels'] == []
    assert scid((l2, l4)) not in {c['short_channel_id']
                                  for c in l1.rpc.listchannels()['channels']}
    by_asset = {a: {c['short_channel_id']
                    for c in l5.rpc.call('assetchan-listchannels', {'asset': a})['channels']}
                for a in (asset, 'lbtc')}
    assert by_asset[asset] == {scid(p) for p in ((l1, l2), (l2, l3), (l3, l4))}
    assert by_asset['lbtc'] == {scid(p) for p in ((l1, l5), (l5, l4))}
    assert all(c['asset'] in (asset, 'lbtc')
               for c in l5.rpc.call('assetchan-listchannels', {})['channels'])

    # 2. An asset payment l1 -> l4: over the announced asset channels and
    #    l4's hint (core picks one of the asset channels the plugin offers,
    #    the private one by its alias), never the L-BTC ones.
    wait_for(lambda: len([i for i in l4.rpc.listincoming()['incoming']
                          if i['id'] in (l2.info['id'], l3.info['id'])]) == 2)
    inv = l4.rpc.call('assetchan-invoice', {'amount': PAY_UNITS, 'asset': asset,
                                            'label': 'asset', 'description': 'in the asset'})
    hints = [h[0] for h in l1.rpc.decode(inv['bolt11'])['routes']]
    assert hints and {h['pubkey'] for h in hints} <= {l2.info['id'], l3.info['id']}
    assert all(h['short_channel_id'] in (scid((l2, l4)), scid((l3, l4)),
                                         channel(l4, chans[(l2, l4)])['alias']['remote'])
               for h in hints)
    res = l1.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': asset})
    assert res['failed_parts'] == 0
    settled(*nodes)
    assert channel(l4, chans[(l2, l4)])['to_us_msat'] + channel(l4, chans[(l3, l4)])['to_us_msat'] \
        == PAY_UNITS * 1000
    assert channel(l1, chans[(l1, l5)])['to_us_msat'] == 10**6 * 1000

    # 3. An L-BTC invoice on l4 sent over the asset channels, as a payer
    #    that can't tell them apart might (gossip shows them as the cheapest
    #    path: l5 charges 50 sat): l1 sends 1,000 units that way, l2 and l3
    #    forward them (asset to asset), and l4's plugin fails the HTLC as a
    #    temporary failure of the last channel, which is what makes a payer
    #    route around it rather than give up on the invoice.
    wait_for(lambda: [c for c in l1.rpc.listchannels(short_channel_id=scid((l5, l4)))['channels']
                      if c['source'] == l5.info['id'] and c['base_fee_millisatoshi'] == 50000])
    l2_before = channel(l2, chans[(l1, l2)])['to_us_msat']
    inv = l4.rpc.invoice(1000 * 1000, 'lbtc', 'in L-BTC', exposeprivatechannels=False)
    route = [{'id': l2.info['id'], 'channel': scid((l1, l2)), 'amount_msat': 1002 * 1000, 'delay': 30},
             {'id': l3.info['id'], 'channel': scid((l2, l3)), 'amount_msat': 1001 * 1000, 'delay': 24},
             {'id': l4.info['id'], 'channel': scid((l3, l4)), 'amount_msat': 1000 * 1000, 'delay': 18}]
    l1.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'],
                   amount_msat=1000 * 1000, bolt11=inv['bolt11'])
    with pytest.raises(RpcError) as err:
        l1.rpc.waitsendpay(inv['payment_hash'])
    assert err.value.error['data']['failcodename'] == 'WIRE_TEMPORARY_CHANNEL_FAILURE'
    assert err.value.error['data']['erring_node'] == l4.info['id']
    l4.daemon.wait_for_log('arrived in {} but the invoice is in lbtc'.format(asset))
    settled(*nodes)
    assert channel(l2, chans[(l1, l2)])['to_us_msat'] == l2_before
    assert only_one(l4.rpc.listinvoices('lbtc')['invoices'])['status'] == 'unpaid'
    #    Plain xpay gets there whichever path it tries first (50 sat on 1,000
    #    is over its default fee cap, so one is given): at most one failed
    #    part, the money over l5, the asset channels untouched.
    res = l1.rpc.call('xpay', {'invstring': inv['bolt11'], 'maxfee': 100000})
    assert res['failed_parts'] <= 1 and res['successful_parts'] == 1
    settled(*nodes)
    assert channel(l2, chans[(l1, l2)])['to_us_msat'] == l2_before
    assert channel(l1, chans[(l1, l5)])['to_us_msat'] == (10**6 - 1000 - 50) * 1000

    # 4. The same through the plugin: assetchan-pay in 'lbtc' keeps off every
    #    channel it knows to be in an asset, so it goes over l5 at once.
    inv = l4.rpc.invoice(1000 * 1000, 'lbtc2', 'in L-BTC', exposeprivatechannels=False)
    res = l1.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': 'lbtc', 'maxfee': 100000})
    assert res['failed_parts'] == 0
    settled(*nodes)
    assert channel(l1, chans[(l1, l5)])['to_us_msat'] == (10**6 - 2 * 1050) * 1000

    # 5. l5 has no asset channel, so it can't pay l4's asset invoice: not
    #    through the plugin (nothing to leave by), nor by plain xpay (l4
    #    refuses the HTLC that arrives over L-BTC), nor around through l1,
    #    whose plugin refuses to forward from its L-BTC channel into an
    #    asset one.
    inv = l4.rpc.call('assetchan-invoice', {'amount': PAY_UNITS, 'asset': asset,
                                            'label': 'asset-l5', 'description': 'in the asset'})
    with pytest.raises(RpcError):
        l5.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': asset})
    with pytest.raises(RpcError, match='Failed after'):
        l5.rpc.call('xpay', {'invstring': inv['bolt11']})
    l4.daemon.wait_for_log('arrived in lbtc but the invoice is in {}'.format(asset))
    #    (l5 needs something on its side of l1-l5 to try the way round.)
    l1.rpc.pay(l5.rpc.invoice(10000 * 1000, 'l5-lbtc', 'in L-BTC')['bolt11'])
    settled(l1, l5)
    route = [{'id': l1.info['id'], 'channel': scid((l1, l5)), 'amount_msat': (PAY_UNITS + 2) * 1000, 'delay': 30},
             {'id': l2.info['id'], 'channel': scid((l1, l2)), 'amount_msat': (PAY_UNITS + 1) * 1000, 'delay': 24},
             {'id': l4.info['id'], 'channel': scid((l2, l4)), 'amount_msat': PAY_UNITS * 1000, 'delay': 18}]
    l5.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'],
                   amount_msat=PAY_UNITS * 1000, bolt11=inv['bolt11'])
    with pytest.raises(RpcError) as err:
        l5.rpc.waitsendpay(inv['payment_hash'])
    assert err.value.error['data']['failcodename'] == 'WIRE_UNKNOWN_NEXT_PEER'
    assert err.value.error['data']['erring_node'] == l1.info['id']
    l1.daemon.wait_for_log(r'Refusing to forward .* not in lbtc')
    settled(*nodes)
    assert only_one(l4.rpc.listinvoices('asset-l5')['invoices'])['status'] == 'unpaid'

    # Nobody crashed processing all that.
    for n in nodes:
        n.rpc.getinfo()
