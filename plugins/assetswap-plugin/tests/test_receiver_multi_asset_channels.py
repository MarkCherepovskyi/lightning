"""The receiver holds channels in two different assets to the same
provider, requests payment in one of them, and the payer pays with the
*other*. Leg 2 must settle over the channel in the asset the invoice
actually requested -- never "helpfully" slip through the receiver's
other channel just because a route happens to exist there too.

    TEST_NETWORK=liquid-regtest pytest plugins/assetswap-plugin/tests/test_receiver_multi_asset_channels.py
"""
from fixtures import *  # noqa: F401,F403
from helpers import (DEFAULT_FEE_BASE_MSAT, DEFAULT_RATE_PPM, PRICETIERS_PLUGIN,
                     ASSETSWAP_PLUGIN, wait_for_channels_normal)
from test_liquid_asset_poc import channel_by_funding, fund_asset_channel, settled
from pyln.client import RpcError
from utils import TEST_NETWORK, only_one, sync_blockheight

import pytest


pytestmark = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                reason="Liquid asset channels need elementsd")

TO_AMOUNT_MSAT = 100_000_000


def setup_receiver_with_two_asset_channels(node_factory, bitcoind):
    """l1 (payer) --asset_a--> l2 (provider) --asset_a AND asset_b--> l3
    (receiver). l3 holds a channel in EACH asset directly with l2, so a
    route in asset_a reaches l3 just as directly as one in asset_b does
    -- the only thing that should decide which one is used is the
    invoice's own requested asset, not route availability.
    Returns (l1, l2, l3, asset_a, asset_b, lbtc, funding_a_l1l2, funding_a_l2l3, funding_b_l2l3).
    """
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset_a = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    asset_b = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    pair = f'{asset_a}:{asset_b}:{DEFAULT_RATE_PPM}:{DEFAULT_FEE_BASE_MSAT}'
    l2 = node_factory.get_node(options={'plugin': [ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN],
                                        'pricetiers-pair': pair})
    l1, l3 = node_factory.get_nodes(2, opts=[
        {'plugin': [ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN], 'pricetiers-pair': pair,
         'assetswap-provider': l2.info['id']},
        {'plugin': ASSETSWAP_PLUGIN},
    ])
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)
    l1.fundwallet(10**7)
    l2.fundwallet(2 * 10**7)

    funding_a_l1l2 = fund_asset_channel(bitcoind, l1, l2, asset_a, lbtc)
    funding_a_l2l3 = fund_asset_channel(bitcoind, l2, l3, asset_a, lbtc)
    funding_b_l2l3 = fund_asset_channel(bitcoind, l2, l3, asset_b, lbtc)
    bitcoind.generate_block(6)
    wait_for_channels_normal(l1, l2, l3)
    sync_blockheight(bitcoind, [l1, l2, l3])

    return (l1, l2, l3, asset_a, asset_b, lbtc,
           funding_a_l1l2, funding_a_l2l3, funding_b_l2l3)


def test_receiver_two_channels_payer_uses_non_requested_asset(node_factory, bitcoind):
    """l3 asks to be paid in asset_b; l1 pays with asset_a (which l3 ALSO
    holds a channel in, via the same provider). The swap must still
    settle over l3's asset_b channel -- its asset_a channel with l2 is
    untouched."""
    (l1, l2, l3, asset_a, asset_b, lbtc,
     funding_a_l1l2, funding_a_l2l3, funding_b_l2l3) = setup_receiver_with_two_asset_channels(node_factory, bitcoind)

    before_a = channel_by_funding(l3, funding_a_l2l3)['to_us_msat']

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': 'two-chan-cross',
                                           'description': 'two-chan-cross',
                                           'asset': asset_b})
    res = l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                          'from_asset': asset_a})
    assert res['status'] == 'complete'
    settled(l1, l2, l3)

    assert only_one(l3.rpc.listinvoices('two-chan-cross')['invoices'])['status'] == 'paid'
    # Settled on the asset_b channel...
    assert channel_by_funding(l3, funding_b_l2l3)['to_us_msat'] == TO_AMOUNT_MSAT
    # ...and the asset_a channel to l2 never moved.
    assert channel_by_funding(l3, funding_a_l2l3)['to_us_msat'] == before_a


def test_receiver_two_channels_direct_pin_does_not_crossfill(node_factory, bitcoind):
    """l1 pins from_asset to the invoice's OWN asset (asset_b) directly
    (no swap requested at all), but l1 has no route in asset_b itself --
    only in asset_a, which happens to also reach l3. This must fail as a
    direct-payment failure, never silently substitute asset_a."""
    (l1, l2, l3, asset_a, asset_b, lbtc,
     funding_a_l1l2, funding_a_l2l3, funding_b_l2l3) = setup_receiver_with_two_asset_channels(node_factory, bitcoind)

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': 'two-chan-pinned',
                                           'description': 'two-chan-pinned',
                                           'asset': asset_b})
    # l1 only ever funded an asset_a channel to l2 -- it has no route in
    # asset_b at all, direct or otherwise.
    with pytest.raises(RpcError, match=f"payment in {asset_b} failed"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                        'from_asset': asset_b})
