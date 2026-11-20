"""Dual-funded channels (open_channel2) on Liquid: L-BTC channels funded by
both sides from their CLN wallets, and asset channels (contrib/assetchan)
funded by both sides from their elementsd wallets.

    TEST_NETWORK=liquid-regtest pytest tests/test_dualfund_liquid.py
"""
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from utils import only_one, sync_blockheight, wait_for, TEST_NETWORK

import pytest
import unittest


# Dual funding is opt-in: the nodes turn it on themselves (the suite's
# EXPERIMENTAL_DUAL_FUND switch would skip the asset channel tests).
DUAL_FUND = {'experimental-dual-fund': None}


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_lbtc_dual_fund(node_factory, bitcoind):
    """Both sides put L-BTC into a channel: l2's funder plugin matches l1."""
    l1, l2 = node_factory.get_nodes(2, opts=DUAL_FUND)
    l1.fundwallet(10**7)
    l2.fundwallet(10**7)

    l2.rpc.funderupdate(policy='match', policy_mod=100,
                        fuzz_percent=0, leases_only=False)

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    amount = 10**6
    res = l1.rpc.fundchannel(l2.info['id'], amount)

    bitcoind.generate_block(1, wait_for_mempool=res['txid'])
    sync_blockheight(bitcoind, [l1, l2])
    l1.daemon.wait_for_log(' to CHANNELD_NORMAL')
    l2.daemon.wait_for_log(' to CHANNELD_NORMAL')

    chan = only_one(l1.rpc.listpeerchannels()['channels'])
    assert chan['total_msat'] == amount * 2 * 1000
    assert chan['funding']['local_funds_msat'] == amount * 1000
    assert chan['funding']['remote_funds_msat'] == amount * 1000

    # Both sides can pay the other straight away.
    inv = l2.rpc.invoice(100_000_000, 'l1-to-l2', 'desc')
    l1.rpc.xpay(inv['bolt11'])
    inv = l1.rpc.invoice(200_000_000, 'l2-to-l1', 'desc')
    l2.rpc.xpay(inv['bolt11'])

    # And close it again.
    l1.rpc.close(l2.info['id'])
    bitcoind.generate_block(1, wait_for_mempool=1)
    wait_for(lambda: only_one(l1.rpc.listpeerchannels()['channels'])['state']
             == 'ONCHAIN')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_asset_dual_fund(node_factory, bitcoind):
    """l1 opens an asset channel with open_channel2 (assetchan-open
    dual_fund=true) and l2's plugin matches it from elementsd's wallet
    (assetchan-dual-fund-match): both can pay at once, and a mutual close
    pays each its share at its elementsd close address."""
    from test_assetchan import (CHANNEL_UNITS, FEE_RESERVE, PAY_UNITS, channel, issue_asset,
                                node_opts, pay, units, wait_normal, wallet_tx_vout)
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = issue_asset(bitcoind)
    opts = dict(node_opts(bitcoind, asset), **DUAL_FUND)
    l1 = node_factory.get_node(options=opts)
    l2 = node_factory.get_node(options=dict(opts, **{'assetchan-dual-fund-match': 100}))

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    res = l1.rpc.call('assetchan-open', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                         'asset': asset, 'fee_reserve': FEE_RESERVE,
                                         'dual_fund': True})
    chan = res['channel_id']
    assert res['asset'] == asset and res['fee_reserve'] == FEE_RESERVE

    # The funding transaction: both sides' asset to the 2-of-2 at output n,
    # l1's L-BTC fee reserve to it at n+1, and the fee output last.
    vout = wallet_tx_vout(bitcoind, res['txid'])
    out = res['outnum']
    assert vout[out]['asset'] == asset and units(vout[out]['value']) == 2 * CHANNEL_UNITS
    assert vout[out + 1]['asset'] == lbtc and units(vout[out + 1]['value']) == FEE_RESERVE
    assert vout[out]['scriptPubKey']['hex'] == vout[out + 1]['scriptPubKey']['hex']
    assert vout[-1]['scriptPubKey']['type'] == 'fee'

    bitcoind.generate_block(6, wait_for_mempool=res['txid'])
    wait_normal(l1, chan)
    wait_normal(l2, chan)
    for n in (l1, l2):
        c = channel(n, chan)
        assert c['asset'] == asset
        assert c['total_msat'] == 2 * CHANNEL_UNITS * 1000
        assert c['to_us_msat'] == CHANNEL_UNITS * 1000
        assert bitcoind.rpc.getaddressinfo(c['close_to_addr'])['ismine']

    # Both can pay straight away: l2 put its half in.
    pay(l1, l2, asset, PAY_UNITS, 'l1-to-l2')
    pay(l2, l1, asset, 3 * PAY_UNITS, 'l2-to-l1')
    l1_units = CHANNEL_UNITS + 2 * PAY_UNITS
    l2_units = CHANNEL_UNITS - 2 * PAY_UNITS
    assert channel(l1, chan)['to_us_msat'] == l1_units * 1000
    assert channel(l2, chan)['to_us_msat'] == l2_units * 1000

    # A mutual close pays each its units, the fee reserve pays the fee.
    res = l1.rpc.call('assetchan-close', {'id': chan})
    close_txid = only_one(res['txids'])
    blockhash = bitcoind.generate_block(1, wait_for_mempool=close_txid)[0]
    for n in (l1, l2):
        wait_for(lambda: channel(n, chan)['state'] == 'ONCHAIN')
    tx = bitcoind.rpc.getrawtransaction(close_txid, True, blockhash)
    asset_outs = {v['scriptPubKey']['address']: units(v['value'])
                  for v in tx['vout'] if v.get('asset') == asset}
    assert asset_outs == {channel(l1, chan)['close_to_addr']: l1_units,
                          channel(l2, chan)['close_to_addr']: l2_units}
    assert sum(units(v['value']) for v in tx['vout'] if v.get('asset') == lbtc) == FEE_RESERVE


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_asset_dual_fund_one_sided(node_factory, bitcoind):
    """An asset channel opened with open_channel2 that only the opener puts
    the asset into (the peer's plugin doesn't match: the default), and
    whose funding can't be RBF'd."""
    from test_assetchan import (CHANNEL_UNITS, FEE_RESERVE, PAY_UNITS, channel, issue_asset,
                                node_opts, pay, units, wait_normal, wallet_tx_vout)
    asset = issue_asset(bitcoind)
    l1, l2 = node_factory.get_nodes(2, opts=dict(node_opts(bitcoind, asset), **DUAL_FUND))
    # An L-BTC PSBT to try to bump with (fundwallet mines a block).
    l1.fundwallet(10**6)
    psbt = l1.rpc.fundpsbt(100000, 'opening', 0, reserve=0)['psbt']

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    res = l1.rpc.call('assetchan-open', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                         'asset': asset, 'fee_reserve': FEE_RESERVE,
                                         'dual_fund': True})
    chan = res['channel_id']
    vout = wallet_tx_vout(bitcoind, res['txid'])
    assert units(vout[res['outnum']]['value']) == CHANNEL_UNITS

    # Not while it's in the mempool, nor ever: no RBF for asset channels.
    with pytest.raises(RpcError, match="Asset channels can't be RBF'd"):
        l1.rpc.openchannel_bump(chan, 10000, psbt)

    bitcoind.generate_block(6, wait_for_mempool=res['txid'])
    wait_normal(l1, chan)
    wait_normal(l2, chan)
    assert channel(l1, chan)['to_us_msat'] == CHANNEL_UNITS * 1000
    assert channel(l2, chan)['to_us_msat'] == 0
    pay(l1, l2, asset, PAY_UNITS, 'l1-to-l2')
    assert channel(l2, chan)['to_us_msat'] == PAY_UNITS * 1000


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_asset_dual_fund_refused(node_factory, bitcoind):
    """A peer that doesn't take channels in the asset refuses the
    open_channel2; the opener's elementsd inputs are unlocked again."""
    from test_assetchan import CHANNEL_UNITS, FEE_RESERVE, issue_asset, node_opts
    asset, other = issue_asset(bitcoind), issue_asset(bitcoind)
    l1 = node_factory.get_node(options=dict(node_opts(bitcoind, asset), **DUAL_FUND))
    l2 = node_factory.get_node(options=dict(node_opts(bitcoind, other), **DUAL_FUND),
                               allow_warning=True)

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    with pytest.raises(RpcError, match='asset {} is not one we take channels in'.format(asset)):
        l1.rpc.call('assetchan-open', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                       'asset': asset, 'fee_reserve': FEE_RESERVE,
                                       'dual_fund': True})
    assert bitcoind.rpc.listlockunspent() == []
    assert l1.rpc.listpeerchannels()['channels'] == [] or \
        all(c['state'] != 'DUALOPEND_AWAITING_LOCKIN' for c in l1.rpc.listpeerchannels()['channels'])


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_asset_dual_fund_reconnect(node_factory, bitcoind):
    """The peer drops the connection while sending its tx_signatures: on
    reconnecting both resend their commitment_signed (with the fee reserve's
    signature) and tx_signatures, and the channel opens all the same."""
    from test_assetchan import (CHANNEL_UNITS, FEE_RESERVE, PAY_UNITS, channel, issue_asset,
                                node_opts, pay, wait_normal)
    asset = issue_asset(bitcoind)
    opts = dict(node_opts(bitcoind, asset), **DUAL_FUND)
    l1 = node_factory.get_node(options=opts, may_reconnect=True)
    l2 = node_factory.get_node(options=dict(opts, **{'assetchan-dual-fund-match': 100}),
                               disconnect=['-WIRE_TX_SIGNATURES'], may_reconnect=True)

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    try:
        l1.rpc.call('assetchan-open', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                       'asset': asset, 'fee_reserve': FEE_RESERVE,
                                       'dual_fund': True})
    except RpcError:
        # The command may see the disconnect: the open goes on regardless.
        pass
    # It reconnects and finishes the exchange: both broadcast the funding.
    for n in (l1, l2):
        wait_for(lambda: n.daemon.is_in_log('peer_in WIRE_CHANNEL_REESTABLISH')
                 and n.daemon.is_in_log('Broadcasting funding tx'))
    chan = only_one(l2.rpc.listpeerchannels()['channels'])['channel_id']
    bitcoind.generate_block(6, wait_for_mempool=channel(l2, chan)['funding_txid'])
    wait_normal(l1, chan)
    wait_normal(l2, chan)
    assert channel(l1, chan)['to_us_msat'] == channel(l2, chan)['to_us_msat'] == \
        CHANNEL_UNITS * 1000
    pay(l1, l2, asset, PAY_UNITS, 'l1-to-l2')
    pay(l2, l1, asset, PAY_UNITS, 'l2-to-l1')


@unittest.skipIf(TEST_NETWORK != 'liquid-regtest', 'Liquid only')
def test_asset_dual_fund_unilateral(node_factory, bitcoind):
    """A dual-funded asset channel force-closed by the accepter: the
    commitment spends the asset funding output and the opener's fee reserve,
    and each side sweeps its asset to its elementsd close address."""
    from test_assetchan import (CHANNEL_UNITS, FEE_RESERVE, PAY_UNITS, channel, issue_asset,
                                mine_spends_of, node_opts, paid_by_elementsd, pay, units,
                                wait_normal, wallet_units)
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = issue_asset(bitcoind)
    opts = dict(node_opts(bitcoind, asset), **DUAL_FUND)
    l1 = node_factory.get_node(options=opts)
    l2 = node_factory.get_node(options=dict(opts, **{'assetchan-dual-fund-match': 100}))

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    res = l1.rpc.call('assetchan-open', {'id': l2.info['id'], 'amount': CHANNEL_UNITS,
                                         'asset': asset, 'fee_reserve': FEE_RESERVE,
                                         'dual_fund': True})
    funding_txid, outnum, chan = res['txid'], res['outnum'], res['channel_id']
    bitcoind.generate_block(6, wait_for_mempool=funding_txid)
    wait_normal(l1, chan)
    wait_normal(l2, chan)
    close_to = {n: channel(n, chan)['close_to_addr'] for n in (l1, l2)}
    pay(l1, l2, asset, PAY_UNITS, 'balance')
    want_units = {l1: CHANNEL_UNITS - PAY_UNITS, l2: CHANNEL_UNITS + PAY_UNITS}
    elements_before = wallet_units(bitcoind, asset)

    l1.stop()
    res = l2.rpc.close(chan, unilateraltimeout=1)
    assert res['type'] == 'unilateral'
    commit_txid = only_one(res['txids'])
    blockhash = bitcoind.generate_block(1, wait_for_mempool=commit_txid)[0]
    commit = bitcoind.rpc.getrawtransaction(commit_txid, True, blockhash)
    assert sorted((vin['txid'], vin['vout']) for vin in commit['vin']) == \
        [(funding_txid, outnum), (funding_txid, outnum + 1)]
    assert sorted(units(v['value']) for v in commit['vout'] if v.get('asset') == asset) == \
        sorted(want_units.values())
    assert units(commit['fee'][lbtc]) == FEE_RESERVE - 660

    l1.start()
    sweeps = mine_spends_of(bitcoind, [l1, l2], commit_txid, 2)
    swept = {}
    for tx in sweeps.values():
        out = only_one([v for v in tx['vout'] if v.get('asset') == asset])
        swept[out['scriptPubKey']['address']] = units(out['value'])
        paid_by_elementsd(bitcoind, tx)
    assert swept == {close_to[n]: want_units[n] for n in (l1, l2)}
    assert wallet_units(bitcoind, asset) == elements_before + 2 * CHANNEL_UNITS
