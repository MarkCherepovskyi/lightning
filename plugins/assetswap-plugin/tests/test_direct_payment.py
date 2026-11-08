"""payassetinvoice's same-asset path: when the payer already holds (or is
given) a channel in the invoice's own asset, no swap-provider is needed
at all -- this is just an ordinary payment, same as `xpay` directly. See
pay.rs's own doc comment for the from_asset decision table this exercises.

    TEST_NETWORK=liquid-regtest pytest plugins/assetswap-plugin/tests/test_direct_payment.py
"""
from fixtures import *  # noqa: F401,F403
from helpers import ASSETSWAP_PLUGIN
from test_liquid_asset_poc import LBTC_CHANNEL_SAT, asset_invoice, fund_asset_channel, settled
from pyln.client import RpcError
from utils import TEST_NETWORK, wait_for

import pytest


pytestmark = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                reason="Liquid asset channels need elementsd")


def test_direct_payment_automatic(node_factory, bitcoind):
    """from_asset omitted ("automatic"): a direct route in the invoice's
    own asset exists, so payassetinvoice settles over it without ever
    consulting a swap-provider (there isn't one configured here at all)."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts={'plugin': ASSETSWAP_PLUGIN})
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.fundwallet(10**7)
    fund_asset_channel(bitcoind, l1, l2, asset, lbtc)
    bitcoind.generate_block(6)
    wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL' for c in l1.rpc.listpeerchannels()['channels']))

    inv = asset_invoice(l2, 'direct-auto', asset)
    wrapped = l2.rpc.call('assetinvoice', {'amount_msat': inv['amount_msat'],
                                           'label': 'direct-auto-2',
                                           'description': 'direct-auto-2',
                                           'asset': asset})

    res = l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice']})
    assert res['amount_msat'] == inv['amount_msat']
    settled(l1, l2)


def test_direct_payment_explicit_same_asset(node_factory, bitcoind):
    """from_asset explicitly equal to the invoice's own asset: a direct,
    no-swap-fallback payment -- same route as the automatic case, just
    pinned deliberately rather than inferred."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts={'plugin': ASSETSWAP_PLUGIN})
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.fundwallet(10**7)
    fund_asset_channel(bitcoind, l1, l2, asset, lbtc)
    bitcoind.generate_block(6)
    wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL' for c in l1.rpc.listpeerchannels()['channels']))

    inv = asset_invoice(l2, 'direct-pinned', asset)
    wrapped = l2.rpc.call('assetinvoice', {'amount_msat': inv['amount_msat'],
                                           'label': 'direct-pinned-2',
                                           'description': 'direct-pinned-2',
                                           'asset': asset})

    res = l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                          'from_asset': asset})
    assert res['amount_msat'] == inv['amount_msat']
    settled(l1, l2)


def test_direct_payment_explicit_same_asset_no_fallback_on_failure(node_factory, bitcoind):
    """from_asset pinned to the invoice's own asset, but there's no route
    in it (l1 only has an L-BTC channel to l2). Must fail as a direct
    payment failure -- the caller asked for this asset specifically, so
    it must NOT be silently retried as a swap in some other asset."""
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts={'plugin': ASSETSWAP_PLUGIN})
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.fundwallet(10**7)
    l1.rpc.fundchannel(l2.info['id'], LBTC_CHANNEL_SAT)  # l1-l2: L-BTC only, no `asset`
    bitcoind.generate_block(6)
    wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL' for c in l1.rpc.listpeerchannels()['channels']))

    wrapped = l2.rpc.call('assetinvoice', {'amount_msat': 1_000_000,
                                           'label': 'direct-pinned-fail',
                                           'description': 'direct-pinned-fail',
                                           'asset': asset})

    with pytest.raises(RpcError, match=f"payment in {asset} failed"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                        'from_asset': asset})


def test_no_provider_configured_and_no_direct_route(node_factory, bitcoind):
    """Paying an invoice in an asset the payer can't directly reach, with
    no assetswap-provider configured at all, must fail loudly naming that
    as the reason -- not hang, and not silently do nothing."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts={'plugin': ASSETSWAP_PLUGIN})
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.fundwallet(10**7)
    # l2 opens the asset channel, so the whole initial balance sits on
    # l2's side: l1 has the channel but nothing of `asset` to pay with.
    fund_asset_channel(bitcoind, l2, l1, asset, lbtc)
    l1.rpc.fundchannel(l2.info['id'], LBTC_CHANNEL_SAT)
    bitcoind.generate_block(6)
    wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL' for c in l1.rpc.listpeerchannels()['channels']))

    wrapped = l2.rpc.call('assetinvoice', {'amount_msat': 1_000_000,
                                           'label': 'no-provider',
                                           'description': 'no-provider',
                                           'asset': asset})

    with pytest.raises(RpcError, match="no assetswap-provider is configured"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice']})
