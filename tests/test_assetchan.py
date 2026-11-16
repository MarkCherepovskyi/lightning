"""Asset channels with the contrib/assetchan plugin (DESIGN.md section 6,
steps 4 and 5): A-B-C on liquid-regtest, asset channels A-B and B-C funded
by elementsd, an L-BTC channel A-B alongside, an asset invoice on C paid from
A, a wrong-asset payment refused, a mutual close that lands the asset at the
elementsd close addresses; a unilateral close by either side, each sweeping
its asset output with L-BTC from its CLN wallet for the fee; a unilateral close
with an HTLC in flight, resolved on-chain by HTLC txs or direct spends; and
a revoked commitment, penalized by the victim.  test_gossip (step 7): five
nodes with private asset channels next to announced L-BTC ones, and what
payers with and without the plugin do.  Then the guards: core keeps
forwards within an asset whatever a plugin says, the plugin refuses to run
unless important, ordinary L-BTC channels between plugin nodes (anchors on
Elements) close on chain, and an asset sweep waits for L-BTC in the wallet.

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
# What a node's wallet may pay in fees for one close: a few txs, each at
# most ten fee reserves (near a deadline, at regtest's high feerates) plus
# dust change.
# (At regtest's default 15000 perkw an Elements change output costs ~4,000
# sat: leftover L-BTC short of that goes to the fee.)
CHANGE_SLACK = 5000
MAX_FEES = 4 * (10 * FEE_RESERVE + CHANGE_SLACK)

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
    # (It refuses to run as anything but an important plugin.)
    return {'important-plugin': PLUGIN, 'assetchan-asset': asset,
            'assetchan-elements-rpc': elements_rpc,
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


def paid_by_wallet(bitcoind, tx, *nodes):
    """A sweep of an asset output (input 0): core paid its L-BTC fee with
    coins of one of the nodes' CLN wallets (not elementsd's)."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    assert len(tx['vin']) >= 2 and units(tx['fee'][lbtc]) > 0
    # Its fee is capped: ten fee reserves at most (near a deadline), plus
    # change too small to be worth its own output.
    assert units(tx['fee'][lbtc]) <= 10 * FEE_RESERVE + CHANGE_SLACK
    coins = [{(o['txid'], o['output']) for o in n.rpc.listfunds(spent=True)['outputs']}
             for n in nodes]
    ins = {(vin['txid'], vin['vout']) for vin in tx['vin'][1:]}
    assert any(ins <= mine for mine in coins), (ins, coins)


def open_asset_channel(bitcoind, l1, l2, asset, fund_wallets=True):
    """l1 opens an asset channel to l2, funded by elementsd; both get some
    L-BTC in their CLN wallets (unless fund_wallets is false), as core pays
    the fees of asset sweeps with it.  Returns assetchan-open's result once
    the channel is usable."""
    if fund_wallets:
        for n in (l1, l2):
            n.fundwallet(10**6)
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
    # The fee reserve pays the commitment's fee whatever it carries: each
    # side accepts only half the HTLCs whose outputs that pays for at
    # Elements' 0.1 sat/vB relay floor (2,000 sat: 164, so 82 each).
    for n in (l1, l2):
        assert channel(n, funding[l1])['max_accepted_htlcs'] == 82

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
    elementsd close address, core paying the L-BTC fee from its CLN
    wallet."""
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
        # fee is L-BTC from the node's CLN wallet, the rest of which comes back.
        assert units(asset_outs[0]['value']) == want_units[node]
        assert tx['vin'][0]['txid'] == commit_txid
        assert commit['vout'][tx['vin'][0]['vout']]['asset'] == asset
        assert all(vin['txid'] != commit_txid for vin in tx['vin'][1:])
        paid_by_wallet(bitcoind, tx, node)
    assert set(by_node) == {l1, l2}
    to_self_delay = channel(closing, chan)['our_to_self_delay']
    assert by_node[closing]['vin'][0]['sequence'] == to_self_delay
    assert by_node[other]['vin'][0]['sequence'] == 1

    # The assets are in elementsd's wallet; each node's wallet paid a little.
    assert wallet_units(bitcoind, asset) == elements_before + CHANNEL_UNITS
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    for n in (l1, l2):
        assert wallet_before[n] - 10000 < wallet_sats(n) < wallet_before[n]

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
    whoever broadcasts one adds L-BTC from its CLN wallet, as for a sweep of an
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
    # takes one output, as its input 0, and pays L-BTC from the CLN wallet.
    first_block = bitcoind.rpc.getblockcount() + 1
    spends = mine_spends_of(bitcoind, [l1, l2], commit_txid, 3, maxblocks=40)
    for tx in spends.values():
        assert [i for i, _ in spends_from(tx, commit_txid)] == [0]
        paid_by_wallet(bitcoind, tx, l1, l2)
    htlc_spend = only_one([tx for tx in spends.values()
                           if spends_from(tx, commit_txid) == [(0, htlc_n)]])
    witness = htlc_spend['vin'][0]['txinwitness']
    winner = l2 if outcome == 'success' else l1
    if (outcome == 'timeout') == (closer == 'payer'):
        # The closer's HTLC tx, pre-signed by the peer: `0 <remotehtlcsig>
        # <localhtlcsig> <preimage or empty> <wscript>`.  The peer's
        # signature is SIGHASH_SINGLE|ANYONECANPAY (it fixes only input 0
        # and output 0, the HTLC in the asset), so the broadcaster can add
        # its L-BTC; ours is SIGHASH_ALL, made after adding it (BOLT 5), so
        # nobody else can re-wrap the tx.
        assert closing == winner
        assert len(witness) == 5
        assert witness[1][-2:] == '83'
        assert witness[2][-2:] == '01'
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
        paid_by_wallet(bitcoind, sweep, winner)
        final = sweep
    else:
        # Taken directly from the peer's commitment: `<sig> <preimage or
        # empty> <wscript>`, after the 1-block CSV of anchor channels; our
        # only signature, SIGHASH_ALL over the tx with its L-BTC inputs.
        assert len(witness) == 3
        assert witness[0][-2:] == '01'
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
    # HTLC if it succeeded, and l1 the rest.  Their CLN wallets' L-BTC only
    # paid fees: at regtest's high default feerates each tx (the anchor
    # spend, the HTLC tx or sweep, the sweeps of delayed outputs) pays up to
    # the channel's fee reserve, and up to ten near an HTLC's deadline.
    l2_units = PAY_UNITS + (HTLC_UNITS if outcome == 'success' else 0)
    assert close_units(bitcoind, asset, close_to[l1]) == CHANNEL_UNITS - l2_units
    assert close_units(bitcoind, asset, close_to[l2]) == l2_units
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    for n in (l1, l2):
        assert wallet_before[n] - MAX_FEES < wallet_sats(n) < wallet_before[n]

    # A sweep whose input someone else spent never confirms: it must stop
    # holding wallet L-BTC (rebuilt every block, it used to keep its coins
    # reserved until the channel was forgotten, starving other sweeps).
    # Here: l1's timeout sweep of the HTLC l2 took with the preimage, which
    # would only become valid at the HTLC's expiry.
    if outcome == 'success' and closer == 'payee':
        l1.daemon.wait_for_log('its input is spent, no more rebuilds')
    height = bitcoind.rpc.getblockcount()
    bitcoind.generate_block(max(cltv_expiry + 3 - height, 1))
    sync_blockheight(bitcoind, [l1, l2])
    for n in (l1, l2):
        assert [o for o in n.rpc.listfunds()['outputs'] if o['reserved']] == []

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
    from its CLN wallet."""
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
        paid_by_wallet(bitcoind, tx, victim)
        out = only_one([v for v in tx['vout'] if v.get('asset') == asset])
        assert out['scriptPubKey']['address'] == close_to
        assert units(out['value']) == asset_outs[n]
        witness[asset_outs[n]] = tx['vin'][0]['txinwitness']
    assert len(witness[bad_units]) == 3 and witness[bad_units][1] == '01'
    assert len(witness[HTLC_UNITS]) == 3 and len(witness[HTLC_UNITS][1]) == 66
    assert len(witness[victim_units]) == 2

    # All of the asset went to the victim; its CLN wallet paid the fees.
    assert close_units(bitcoind, asset, close_to) == CHANNEL_UNITS
    assert wallet_units(bitcoind, asset) == elements_before + CHANNEL_UNITS
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [victim])
    assert wallet_before - MAX_FEES < wallet_sats(victim) < wallet_before

    bitcoind.generate_block(100)
    victim.daemon.wait_for_log('onchaind complete, forgetting peer')


def scid_of(node, chan):
    return channel(node, chan)['short_channel_id']


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_gossip(node_factory, bitcoind):
    """Five nodes.  Asset channels l1-l2, l2-l3 and l2-l4, all private: gossip can't say which asset a channel is in, so asset channels
    are never announced (and core only verifies announcements whose funding
    output is L-BTC).  L-BTC channels l1-l5 and l5-l4, announced.  What
    payers, with and without the plugin, do with that."""
    asset = issue_asset(bitcoind)
    # l5 charges 50 sat a forward (the others 1).
    opts = [node_opts(bitcoind, asset) for _ in range(5)]
    opts[4]['fee-base'] = 50000
    nodes = l1, l2, l3, l4, l5 = node_factory.get_nodes(5, opts=opts)
    for a, b in ((l1, l2), (l2, l3), (l2, l4), (l1, l5), (l5, l4)):
        a.rpc.connect(b.info['id'], 'localhost', b.port)

    # 0. No announced asset channel: assetchan-open refuses one, and the
    #    peer's plugin refuses an offer of one.
    with pytest.raises(RpcError, match='asset channels are private'):
        l1.rpc.call('assetchan-open', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                       'asset': asset, 'announce': True})
    with pytest.raises(RpcError, match='asset channels are private'):
        l1.rpc.call('fundchannel_start', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                          'asset': asset, 'fee_reserve': FEE_RESERVE,
                                          'announce': True})
    l2.daemon.wait_for_log('openchannel hook rejects')
    #    Nor an "asset channel" in L-BTC itself (core refuses that tag).
    with pytest.raises(RpcError, match="fee asset"):
        l1.rpc.call('fundchannel_start', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                          'asset': bitcoind.rpc.dumpassetlabels()['bitcoin'],
                                          'fee_reserve': FEE_RESERVE})

    chans = {}
    for opener, peer in ((l1, l2), (l2, l3), (l2, l4)):
        opener.rpc.connect(peer.info['id'], 'localhost', peer.port)
        res = opener.rpc.call('assetchan-open', {'id': peer.info['id'], 'amount': CHANNEL_UNITS,
                                                 'asset': asset, 'fee_reserve': FEE_RESERVE})
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
        return scid_of(pair[0], chans[pair])

    def alias(pair):
        """What pair[0] calls a private channel in an onion it forwards:
        never its real scid (that would give the channel away)."""
        return channel(pair[0], chans[pair])['alias']['local']

    # 1. Gossip: the L-BTC channels reach every node, the asset ones none
    #    (l5 has no channel of its own in the asset to see).
    public = {scid((l1, l5)), scid((l5, l4))}
    private = {scid(p) for p in ((l1, l2), (l2, l3), (l2, l4))}
    for n in nodes:
        wait_for(lambda: public <= {c['short_channel_id'] for c in n.rpc.listchannels()['channels']})
    assert not private & {c['short_channel_id'] for c in l5.rpc.listchannels()['channels']}
    assert l5.rpc.call('assetchan-listchannels', {'asset': asset})['channels'] == []
    assert {c['short_channel_id'] for c in
            l5.rpc.call('assetchan-listchannels', {'asset': 'lbtc'})['channels']} == public

    # 2. An asset payment l1 -> l4: l1's own asset channel, then l4's route
    #    hint from l2; never the L-BTC channels.
    wait_for(lambda: [i for i in l4.rpc.listincoming()['incoming'] if i['id'] == l2.info['id']])
    inv = l4.rpc.call('assetchan-invoice', {'amount': PAY_UNITS, 'asset': asset,
                                            'label': 'asset', 'description': 'in the asset'})
    assert [h[0]['pubkey'] for h in l1.rpc.decode(inv['bolt11'])['routes']] == [l2.info['id']]
    layer = l1.rpc.call('assetchan-layer', {'asset': asset})['layer']
    res = l1.rpc.call('assetchan-pay', {'bolt11': inv['bolt11'], 'asset': asset})
    assert res['failed_parts'] == 0
    settled(*nodes)
    assert channel(l4, chans[(l2, l4)])['to_us_msat'] == PAY_UNITS * 1000
    assert channel(l1, chans[(l1, l5)])['to_us_msat'] == 10**6 * 1000
    #    (Its askrene layer has every L-BTC channel off, both directions.)
    disabled = {c['short_channel_id_dir']
                for c in only_one(l1.rpc.call('askrene-listlayers', {'layer': layer})['layers'])
                ['channel_updates'] if c.get('enabled') is False}
    assert {'{}/{}'.format(s, d) for s in public for d in (0, 1)} <= disabled

    # 3. An HTLC for an L-BTC invoice on l4 sent over the asset channels
    #    (a hand-made route): l2 forwards it asset to asset, and l4's plugin
    #    fails it as a temporary failure of the channel it came by.
    l2_before = channel(l2, chans[(l1, l2)])['to_us_msat']
    inv = l4.rpc.invoice(1000 * 1000, 'lbtc', 'in L-BTC', exposeprivatechannels=False)
    route = [{'id': l2.info['id'], 'channel': scid((l1, l2)), 'amount_msat': 1001 * 1000, 'delay': 24},
             {'id': l4.info['id'], 'channel': alias((l2, l4)), 'amount_msat': 1000 * 1000, 'delay': 18}]
    l1.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'],
                   amount_msat=1000 * 1000, bolt11=inv['bolt11'])
    with pytest.raises(RpcError) as err:
        l1.rpc.waitsendpay(inv['payment_hash'])
    assert err.value.error['data']['failcodename'] == 'WIRE_TEMPORARY_CHANNEL_FAILURE'
    assert err.value.error['data']['erring_node'] == l4.info['id']
    l4.daemon.wait_for_log('arrived in {} but the invoice is in lbtc'.format(asset))
    settled(*nodes)
    assert channel(l2, chans[(l1, l2)])['to_us_msat'] == l2_before
    #    An HTLC for no invoice at all is none of the plugin's business (a
    #    swap provider's plugin may take it): core fails it as unknown.
    route[-1]['amount_msat'] = 1000 * 1000
    l1.rpc.sendpay(route, '42' * 32, payment_secret='42' * 32, amount_msat=1000 * 1000)
    with pytest.raises(RpcError) as err:
        l1.rpc.waitsendpay('42' * 32)
    assert err.value.error['data']['failcodename'] == 'WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS'
    settled(*nodes)
    #    Plain xpay can't stray onto them: it only knows l1's own asset
    #    channel, which leads nowhere in gossip, so it goes over l5 at once.
    res = l1.rpc.call('xpay', {'invstring': inv['bolt11'], 'maxfee': 100000})
    assert res['failed_parts'] == 0
    settled(*nodes)
    assert channel(l1, chans[(l1, l5)])['to_us_msat'] == (10**6 - 1000 - 50) * 1000

    # 4. assetchan-pay in 'lbtc' keeps off our asset channels: over l5.
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
             {'id': l2.info['id'], 'channel': alias((l1, l2)), 'amount_msat': (PAY_UNITS + 1) * 1000, 'delay': 24},
             {'id': l4.info['id'], 'channel': alias((l2, l4)), 'amount_msat': PAY_UNITS * 1000, 'delay': 18}]
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


FORWARD_TO = os.path.join(os.getcwd(), 'tests/plugins/forward_to_channel.py')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_forward_stays_in_asset(node_factory, bitcoind):
    """l2 has two channels to l3 at the same fees, one in the asset and an
    L-BTC one.  A forward that arrives in the asset but names the L-BTC
    channel leaves by the asset one (the plugin picks it).  A plugin whose
    hook has the last word and insists on the L-BTC channel is overruled by
    core: the HTLC is failed as unknown_next_peer, nothing crosses assets."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    l1, l2, l3 = node_factory.get_nodes(3, opts=[opts, {**opts, 'plugin': FORWARD_TO}, opts])
    a12 = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']
    a23 = open_asset_channel(bitcoind, l2, l3, asset)['channel_id']
    l2.fundwallet(10**7)
    b23 = l2.rpc.fundchannel(l3.info['id'], 10**6, announce=False)['channel_id']
    bitcoind.generate_block(6, wait_for_mempool=1)
    wait_normal(l2, b23)
    wait_normal(l3, b23)
    sync_blockheight(bitcoind, [l1, l2, l3])

    def send(label):
        inv = l3.rpc.call('assetchan-invoice', {'amount': PAY_UNITS, 'asset': asset,
                                                'label': label, 'description': label})
        route = [{'id': l2.info['id'], 'channel': scid_of(l1, a12),
                  'amount_msat': (PAY_UNITS + 1) * 1000, 'delay': 24},
                 # (A private channel: l2 forwards by its alias, not its scid.)
                 {'id': l3.info['id'], 'channel': channel(l2, b23)['alias']['local'],
                  'amount_msat': PAY_UNITS * 1000, 'delay': 18}]
        l1.rpc.sendpay(route, inv['payment_hash'], payment_secret=inv['payment_secret'],
                       amount_msat=PAY_UNITS * 1000)
        return inv['payment_hash']

    # The plugin redirects it to the asset channel.
    l1.rpc.waitsendpay(send('redirected'))
    settled(l1, l2, l3)
    assert channel(l3, a23)['to_us_msat'] == PAY_UNITS * 1000
    assert channel(l3, b23)['to_us_msat'] == 0

    # A later hook sends it to the L-BTC channel anyway: core says no.
    l2.rpc.call('forward-to', {'channel_id': b23})
    payment_hash = send('overruled')
    l2.daemon.wait_for_log('forwarding to {}'.format(b23))
    with pytest.raises(RpcError) as err:
        l1.rpc.waitsendpay(payment_hash)
    assert err.value.error['data']['failcodename'] == 'WIRE_UNKNOWN_NEXT_PEER'
    assert err.value.error['data']['erring_node'] == l2.info['id']
    l2.daemon.wait_for_log(r'Not forwarding HTLC .* to channel {} in asset \(chain'.format(b23))
    settled(l1, l2, l3)
    assert channel(l3, a23)['to_us_msat'] == PAY_UNITS * 1000
    assert channel(l3, b23)['to_us_msat'] == 0


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_plugin_must_be_important(node_factory, bitcoind):
    """The plugin runs only as an important plugin, so lightningd stops if
    it dies.  Loaded as an ordinary one, core ignores its feature bit (no
    plugin but an important one may offer asset channels) and it disables
    itself: the node then offers no asset channels, and core won't start
    the one it has."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    l1, l2 = node_factory.get_nodes(2, opts=[{**opts, 'may_reconnect': True},
                                             {**opts, 'may_reconnect': True,
                                              'broken_log': 'Asset channel, but no asset channel plugin'}])
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']

    l2.stop()
    del l2.daemon.opts['important-plugin']
    l2.daemon.opts['plugin'] = PLUGIN
    l2.start()
    # (Logged before "Server started", which start() waited for.)
    assert l2.daemon.is_in_log('may only be offered by an important-plugin: ignoring it')
    assert l2.daemon.is_in_log('disabled itself at init: must run as important-plugin')
    with pytest.raises(RpcError, match='Unknown command'):
        l2.rpc.call('assetchan-list', {})
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.daemon.wait_for_log('Asset channel, but no asset channel plugin')
    assert channel(l2, chan)['state'] == 'CHANNELD_NORMAL'
    assert not channel(l2, chan)['peer_connected']


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_hook_reserve_stays_above_dust(node_factory, bitcoind):
    """An asset channel's reserves stay at least the dust limit, whatever an
    openchannel hook asks for (here 0, which dev-allowdustreserve would
    otherwise let through): a commitment's leftover asset goes to a direct
    output, and one always exists only while both reserves are above dust."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    l1, l2 = node_factory.get_nodes(2, opts=[opts, {**opts, 'dev-allowdustreserve': True,
                                                    'plugin': os.path.join(os.getcwd(), 'tests/plugins/zeroreserve.py'),
                                                    'reserve': '0sat'}])
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']
    l2.daemon.wait_for_log('Setting their reserve to 0sat')
    dust = channel(l2, chan)['dust_limit_msat']
    assert channel(l2, chan)['their_reserve_msat'] >= dust > 0
    assert channel(l1, chan)['our_reserve_msat'] >= dust


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_plain_payments_avoid_asset_channels(node_factory, bitcoind):
    """A plain payment is an L-BTC one: it never leaves over an asset
    channel, which would carry N units of the asset for an invoice of N sat.
    xpay, xpay-as-pay, sendamount and xkeysend are kept off them by
    assetchan's askrene layer, BOLT12 invoices included; pay and keysend
    become xpay-as-pay and xkeysend with that layer, and renepay gets our
    asset channels excluded.  (Core also refuses a blinded
    path that starts at us over an asset channel, the one case a layer can't
    reach: it names our first channel itself.)"""
    asset = issue_asset(bitcoind)
    l1, l2 = node_factory.get_nodes(2, opts=node_opts(bitcoind, asset))
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']
    before = channel(l1, chan)['to_us_msat']

    inv = l2.rpc.invoice(1000 * 1000, 'plain', 'plain')['bolt11']
    calls = [('xpay', {'invstring': inv}),
             ('pay', {'bolt11': inv}),
             # Positional, past the layers param: still gets the layer.
             ('xpay', [inv, None, None, None, 60]),
             ('pay', [inv]),
             ('xpay-as-pay', {'invstring': inv}),
             ('sendamount', {'invstring': inv, 'amount_msat': 1000 * 1000}),
             ('renepay', {'invstring': inv}),
             # keysend becomes xkeysend with the layer.
             ('keysend', {'destination': l2.info['id'], 'amount_msat': 1000 * 1000}),
             ('keysend', [l2.info['id'], 1000 * 1000]),
             ('xkeysend', {'destination': l2.info['id'], 'amount_msat': 1000 * 1000})]
    for method, params in calls:
        with pytest.raises(RpcError):
            l1.rpc.call(method, params)
    # More positionals than we know the names of: refused, not let through.
    with pytest.raises(RpcError, match='pass them by name'):
        l1.rpc.call('xpay', [inv] + [None] * 10)
    offer = l2.rpc.offer(1000 * 1000, 'plain offer')['bolt12']
    b12 = l1.rpc.fetchinvoice(offer)['invoice']
    with pytest.raises(RpcError):
        l1.rpc.xpay(b12)
    # No route at all: nothing was even sent over the asset channel.
    assert l1.rpc.listsendpays()['payments'] == []

    assert channel(l1, chan)['to_us_msat'] == before
    assert only_one(l2.rpc.listinvoices('plain')['invoices'])['status'] == 'unpaid'


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_trimmed_htlc_burned(node_factory, bitcoind):
    """A trimmed (dust) HTLC's units are burned to an OP_RETURN on the
    commitment, as a trimmed HTLC's sats go to the miners on Bitcoin: they
    used to top up the opener's output, so the opener profited from the
    other side's dust HTLCs.  onchaind ignores the burn, like the fee
    output, and everything else resolves as usual."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    # l2 hangs up instead of failing the HTLC, which stays in flight.
    l1, l2 = node_factory.get_nodes(2, opts=[opts, {**opts, 'disconnect': ['-WIRE_UPDATE_FAIL_HTLC']}])
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']
    dust = channel(l1, chan)['dust_limit_msat'] // 1000
    trimmed = dust - 1
    close_to = channel(l1, chan)['close_to_addr']

    send_htlc(l1, l2, chan, trimmed, '01' * 32, '01' * 32)
    l2.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_FAIL_HTLC')
    for n in (l1, l2):
        wait_for(lambda: not channel(n, chan)['peer_connected'])

    commit_txid = only_one(l1.rpc.close(chan, unilateraltimeout=1)['txids'])
    blockhash = bitcoind.generate_block(1, wait_for_mempool=commit_txid)[0]
    commit = bitcoind.rpc.getrawtransaction(commit_txid, True, blockhash)
    asset_outs = [v for v in commit['vout'] if v.get('asset') == asset]
    burn = only_one([v for v in asset_outs if v['scriptPubKey']['hex'] == '6a'])
    assert units(burn['value']) == trimmed
    # Only l1's balance, with no top-up: l2 has none, and no HTLC output.
    assert sorted(units(v['value']) for v in asset_outs) == sorted([trimmed, CHANNEL_UNITS - trimmed])

    # onchaind takes the burn for what it is, and l1 gets its balance.
    mine_spends_of(bitcoind, [l1, l2], commit_txid, 1, maxblocks=40)
    wait_for(lambda: close_units(bitcoind, asset, close_to) == CHANNEL_UNITS - trimmed)
    assert not l1.daemon.is_in_log('Could not find resolution')
    bitcoind.generate_block(100)
    l1.daemon.wait_for_log('onchaind complete, forgetting peer')


HOLD = os.path.join(os.getcwd(), 'contrib/evmchan/hold.py')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_lbtc_anchor_channel_onchain(node_factory, bitcoind):
    """Between two plugin nodes an ordinary L-BTC channel is an anchor
    channel too (the plugin turns anchors on, which asset channels need).
    Force-closed with an HTLC in flight it still resolves on chain: l1
    CPFPs its commitment through its anchor as on Bitcoin (the HTLC is
    L-BTC, worth the fee), and its zero-fee HTLC-timeout tx gets its fee
    from l1's wallet; both with P2WPKH change and an explicit fee output."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    l1, l2 = node_factory.get_nodes(2, opts=[opts, {**opts, 'plugin': HOLD, 'hold-all': False}])
    l1.fundwallet(10**7)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    chan = l1.rpc.fundchannel(l2.info['id'], 10**6, announce=False)['channel_id']
    bitcoind.generate_block(6, wait_for_mempool=1)
    wait_normal(l1, chan)
    wait_normal(l2, chan)
    assert any('anchors' in name for name in channel(l1, chan)['channel_type']['names'])

    payment_hash = '02' * 32
    l2.rpc.call('hold-add', {'payment_hash': payment_hash})
    # Big enough to be worth the HTLC-timeout tx's fee at regtest's rates
    # (lightningd won't pay more in fees than an HTLC is worth).
    send_htlc(l1, l2, chan, 100_000, payment_hash, '02' * 32)
    wait_for(lambda: len(channel(l2, chan)['htlcs']) == 1)

    l1.rpc.disconnect(l2.info['id'], force=True)
    commit = only_one(l1.rpc.close(chan, unilateraltimeout=1)['txids'])
    l1.daemon.wait_for_log('Creating anchor spend for local commit tx')
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 2)
    anchor = only_one([t for t in bitcoind.rpc.getrawmempool() if t != commit])
    tx = bitcoind.rpc.getrawtransaction(anchor, True)
    assert spends_from(tx, commit)
    assert [v['scriptPubKey']['type'] for v in tx['vout']] == ['witness_v0_keyhash', 'fee']
    bitcoind.generate_block(1, wait_for_mempool=[commit, anchor])
    for _ in range(40):
        if l1.daemon.is_in_log('Resolved OUR_UNILATERAL/OUR_HTLC by our proposal OUR_HTLC_TIMEOUT_TX'):
            break
        sync_blockheight(bitcoind, [l1, l2])
        bitcoind.generate_block(1)
    else:
        raise ValueError("l1's HTLC-timeout tx never got mined")
    assert payment_result(bitcoind, l1, payment_hash, [l1, l2]) == 'failed'
    # The HTLC-timeout tx's output comes home after l1's delay.
    _, txid, blocks = l1.wait_for_onchaind_tx('OUR_DELAYED_RETURN_TO_WALLET',
                                              'OUR_HTLC_TIMEOUT_TX/DELAYED_OUTPUT_TO_US')
    bitcoind.generate_block(blocks)
    bitcoind.generate_block(1, wait_for_mempool=txid)
    l1.daemon.wait_for_log('Resolved OUR_HTLC_TIMEOUT_TX/DELAYED_OUTPUT_TO_US by our proposal')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_sweep_waits_for_wallet_lbtc(node_factory, bitcoind):
    """l1 force-closes; l2's asset output (to_remote) can't pay its own fee,
    and l2's CLN wallet has no L-BTC: core keeps rebuilding the sweep every
    block.  onchaind doesn't wait for a to_remote sweep, but lightningd keeps
    the channel past onchaind's 100 blocks (and a restart) until it's mined:
    once l2's wallet has some L-BTC the sweep pays with it, brings the asset
    home, and only then is the channel forgotten."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    l1, l2 = node_factory.get_nodes(2, opts=opts)
    l1.fundwallet(10**6)
    chan = open_asset_channel(bitcoind, l1, l2, asset, fund_wallets=False)['channel_id']
    pay(l1, l2, asset, PAY_UNITS, 'balance')
    close_to = channel(l2, chan)['close_to_addr']
    assert wallet_sats(l2) == 0

    l1.rpc.disconnect(l2.info['id'], force=True)
    commit = only_one(l1.rpc.close(chan, unilateraltimeout=1)['txids'])
    bitcoind.generate_block(1, wait_for_mempool=commit)
    l2.daemon.wait_for_log('Asset sweep of .* L-BTC in our wallet for a fee of .*: will retry every block')
    bitcoind.generate_block(110)
    sync_blockheight(bitcoind, [l1, l2])
    l2.daemon.wait_for_log(r'onchaind complete, but our sweep of \S+ is not mined yet')
    assert not l2.daemon.is_in_log('onchaind complete, forgetting peer')
    assert channel(l2, chan)['state'] == 'ONCHAIN'
    assert close_units(bitcoind, asset, close_to) == 0

    # A restart replays it all and still keeps the channel.
    l2.restart()
    l2.daemon.wait_for_log(r'onchaind complete, but our sweep of \S+ is not mined yet')

    l2.fundwallet(10**6)
    for _ in range(10):
        if close_units(bitcoind, asset, close_to) == PAY_UNITS:
            break
        bitcoind.generate_block(1)
        sync_blockheight(bitcoind, [l2])
    assert close_units(bitcoind, asset, close_to) == PAY_UNITS
    # (close_units counts the mempool too: mine it.)
    bitcoind.generate_block(1)
    l2.daemon.wait_for_log(r'Asset sweep of \S+ is mined')
    l2.daemon.wait_for_log('onchaind complete, forgetting peer')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
@unittest.skipIf(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3', "deletes database, which is assumed sqlite3")
def test_recover_asset_channel(node_factory, bitcoind):
    """l2 loses its database and restores its asset channel from a static
    channel backup.  The backup carries the channel's asset, fee reserve and
    close script, so the restored stub is an asset channel: when l1 closes
    it, l2 sweeps its asset to the close address it had (an address of the
    asset wallet), paying the fee from its CLN wallet."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    # (A restored stub knows neither their per-commitment point nor their
    # funding key: l2 can't tell their outputs apart, as upstream's
    # test_restorefrompeer expects too.)
    l1, l2 = node_factory.get_nodes(2, opts=[{**opts, 'may_reconnect': True},
                                             {**opts, 'may_reconnect': True,
                                              'broken_log': r"onchaind-chan#[0-9]*: Could not find"
                                                            r" resolution for output .*: did \*we\* cheat\?"}])
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']
    pay(l1, l2, asset, PAY_UNITS, 'balance')
    close_to = channel(l2, chan)['close_to_addr']
    scb = l2.rpc.staticbackup()['scb']

    l2.stop()
    os.unlink(os.path.join(l2.daemon.lightning_dir, TEST_NETWORK, 'lightningd.sqlite3'))
    l2.start()
    sync_blockheight(bitcoind, [l2])
    # The new database has no wallet L-BTC (core pays the sweep's fee).
    l2.fundwallet(10**6)

    assert l2.rpc.recoverchannel(scb)['stubs'] == [chan]
    stub = channel(l2, chan)
    assert stub['asset'] == asset
    assert stub['fee_reserve_msat'] == FEE_RESERVE * 1000
    assert stub['close_to_addr'] == close_to

    # l2 asks l1 to close (it can't reestablish a stub).  (l1 may be
    # reconnecting by itself: two connects at once can tear one down.)
    try:
        l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    except RpcError as e:
        if 'disconnected during connection' not in str(e):
            raise
    wait_for(lambda: channel(l1, chan)['state'] == 'AWAITING_UNILATERAL')
    for _ in range(40):
        if close_units(bitcoind, asset, close_to) == PAY_UNITS:
            break
        bitcoind.generate_block(1)
        sync_blockheight(bitcoind, [l1, l2])
    assert close_units(bitcoind, asset, close_to) == PAY_UNITS


def mempool_package(bitcoind, txid):
    """(fee in L-BTC sats, weight) of a mempool tx with its unconfirmed
    ancestors, as a miner sees it (CPFP)."""
    e = bitcoind.rpc.getmempoolentry(txid)
    return units(e['fees']['ancestor']), e['ancestorsize'] * 4


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_anchor_bump_near_htlc_deadline(node_factory, bitcoind):
    """l1 force-closes an asset channel with its HTLC to l2 in flight, at a
    feerate the commitment's fixed fee (the fee reserve less the anchors)
    can't meet.  l1 CPFPs it through its anchor with L-BTC from its CLN
    wallet: the HTLCs are in the asset, so their amount can't say what's
    worth paying in L-BTC.  While the HTLC's deadline is far the anchor
    spend pays at most one fee reserve; once it's close (12 blocks) up to
    ten, and the package meets the feerate and gets mined."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    # l2 hangs up instead of failing the HTLC, so it stays in flight.
    l1, l2 = node_factory.get_nodes(2, opts=[opts, {**opts, 'disconnect': ['-WIRE_UPDATE_FAIL_HTLC']}])
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']

    # l2 doesn't know this payment.
    send_htlc(l1, l2, chan, HTLC_UNITS, '01' * 32, '01' * 32)
    l2.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_FAIL_HTLC')
    for n in (l1, l2):
        wait_for(lambda: not channel(n, chan)['peer_connected'])
    cltv_expiry = only_one(channel(l1, chan)['htlcs'])['expiry']
    # l1's deadline for its own payment: the expiry plus its cltv-delta.
    deadline = cltv_expiry + 6

    feerate = 2000
    l1.set_feerates((feerate,) * 4)
    commit_txid = only_one(l1.rpc.close(chan, unilateraltimeout=1)['txids'])
    assert deadline - bitcoind.rpc.getblockcount() > 12
    l1.daemon.wait_for_log('Anchor spend for local commit tx: capping its fee of .* at {}sat'.format(FEE_RESERVE))
    l1.daemon.wait_for_log('Creating anchor spend for local commit tx')
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 2)
    anchor_txid = only_one([t for t in bitcoind.rpc.getrawmempool() if t != commit_txid])
    fee, weight = mempool_package(bitcoind, anchor_txid)
    assert fee - units(bitcoind.rpc.getmempoolentry(commit_txid)['fees']['base']) <= FEE_RESERVE + CHANGE_SLACK
    assert fee * 1000 // weight < feerate

    # Nothing mines it at that feerate.  Once the deadline is close the
    # anchor spend is replaced by one paying the full feerate.
    while True:
        assert commit_txid in bitcoind.rpc.getrawmempool()
        if bitcoind.rpc.getblockcount() + 12 > deadline:
            break
        bitcoind.generate_block(1, needfeerate=feerate)
        sync_blockheight(bitcoind, [l1])
    l1.daemon.wait_for_log('RBF anchor local commit tx spend')
    wait_for(lambda: anchor_txid not in bitcoind.rpc.getrawmempool()
             and len(bitcoind.rpc.getrawmempool()) == 2)
    anchor_txid = only_one([t for t in bitcoind.rpc.getrawmempool() if t != commit_txid])
    fee, weight = mempool_package(bitcoind, anchor_txid)
    assert fee * 1000 // weight >= feerate * 0.99
    assert fee <= 11 * FEE_RESERVE
    blockhash = bitcoind.generate_block(1, needfeerate=int(feerate * 0.99))[0]
    mined = bitcoind.rpc.getblock(blockhash)['tx']
    assert commit_txid in mined and anchor_txid in mined

    # The anchor spend: the anchor and wallet L-BTC in, P2WPKH change and
    # an explicit fee out.
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    tx = bitcoind.rpc.getrawtransaction(anchor_txid, True, blockhash)
    commit = bitcoind.rpc.getrawtransaction(commit_txid, True, blockhash)
    (_, anchor_n), = spends_from(tx, commit_txid)
    assert commit['vout'][anchor_n]['asset'] == lbtc
    assert units(commit['vout'][anchor_n]['value']) == 330
    coins = {(o['txid'], o['output']) for o in l1.rpc.listfunds(spent=True)['outputs']}
    others = {(vin['txid'], vin['vout']) for vin in tx['vin'] if vin['txid'] != commit_txid}
    assert others and others <= coins
    assert units(tx['fee'][lbtc]) > FEE_RESERVE
    assert [v['scriptPubKey']['type'] for v in tx['vout']] == ['witness_v0_keyhash', 'fee']

    # The HTLC still times out on chain.
    assert payment_result(bitcoind, l1, '01' * 32, [l1, l2]) == 'failed'


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_penalty_fee_cap_near_deadline(node_factory, bitcoind):
    """The cheater's revoked to_local is ours until its delay runs out (30
    blocks, the victim's watchtime): until then the penalty, like any sweep,
    pays at most the channel's fee reserve, which at a high feerate gets
    nothing mined (censors, a fee spike).  Within 12 blocks of the deadline
    it may pay up to ten fee reserves, meets the feerate and gets mined.
    The victim's own to_remote, which nobody else can spend, is swept
    slowly, within one fee reserve."""
    asset = issue_asset(bitcoind)
    opts = node_opts(bitcoind, asset)
    l1, l2 = node_factory.get_nodes(2, opts=[{**opts, 'watchtime-blocks': 30}, opts])
    chan = open_asset_channel(bitcoind, l1, l2, asset)['channel_id']
    victim, bad = l1, l2
    # A wallet coin for each of its two sweeps.
    victim.fundwallet(10**6)
    pay(l1, l2, asset, 10 * PAY_UNITS, 'balance')
    cheat = bad.rpc.dev_sign_last_tx(victim.info['id'])['tx']
    # That commitment gets revoked.
    pay(l1, l2, asset, PAY_UNITS, 'more')

    feerate = 3000
    victim.set_feerates((feerate,) * 4)
    bad.stop()
    commit_txid = bitcoind.rpc.sendrawtransaction(cheat)
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)
    close_height = bitcoind.rpc.getblockcount()
    deadline = close_height + 30
    victim.daemon.wait_for_log('Telling lightningd about OUR_PENALTY_TX')

    # The penalty is capped at the fee reserve, so it doesn't get mined;
    # nor does the victim's to_remote sweep, at the low feerate of a slow
    # (300-block) target.
    victim.daemon.wait_for_log(r'Asset sweep of {}:\d+: capping its fee of .* at {}sat'
                               .format(commit_txid, FEE_RESERVE))
    while bitcoind.rpc.getblockcount() + 12 <= deadline:
        assert not victim.daemon.is_in_log('over the fee reserve')
        bitcoind.generate_block(1, needfeerate=feerate)
        sync_blockheight(bitcoind, [victim])

    # Close to the deadline the penalty may pay more: it meets the feerate.
    victim.daemon.wait_for_log(r'Asset sweep of {}:\d+: capping its fee of .* at {}sat'
                               .format(commit_txid, 10 * FEE_RESERVE)
                               + '|over the fee reserve')
    spends = {}
    for _ in range(3):
        if spends:
            break
        sync_blockheight(bitcoind, [victim])
        blockhash = bitcoind.generate_block(1, needfeerate=int(feerate * 0.99))[0]
        for txid in bitcoind.rpc.getblock(blockhash)['tx']:
            tx = bitcoind.rpc.getrawtransaction(txid, True, blockhash)
            if spends_from(tx, commit_txid):
                spends[txid] = tx
    assert len(spends) == 2
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    assert bitcoind.rpc.getblockcount() <= deadline
    fees = sorted(units(tx['fee'][lbtc]) for tx in spends.values())
    # (Change too small for an output goes to the fee.)
    assert fees[0] <= FEE_RESERVE + CHANGE_SLACK
    assert FEE_RESERVE < fees[1] <= 10 * FEE_RESERVE + CHANGE_SLACK
    for tx in spends.values():
        paid_by_wallet(bitcoind, tx, victim)

    bitcoind.generate_block(100)
    victim.daemon.wait_for_log('onchaind complete, forgetting peer')
