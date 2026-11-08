"""The actual cross-asset swap: payassetinvoice's leg-1/leg-2 split via a
swap-provider when the payer's chosen asset differs from the invoice's
own. See contrib/liquid-poc/CROSS_ASSET_SWAP.md for the design, and
pay.rs/htlc.rs for the implementation these exercise.

    TEST_NETWORK=liquid-regtest pytest plugins/assetswap-plugin/tests/test_cross_asset_swap.py
"""
from fixtures import *  # noqa: F401,F403
from helpers import (DEFAULT_FEE_BASE_MSAT, DEFAULT_RATE_PPM, PRICETIERS_PLUGIN,
                     ASSETSWAP_PLUGIN, setup_swap_topology, wait_for_channels_normal)
from test_liquid_asset_poc import channel_by_funding, fund_asset_channel, settled
from pyln.client import RpcError
from utils import TEST_NETWORK, only_one, sync_blockheight

import pytest


pytestmark = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                reason="Liquid asset channels need elementsd")

TO_AMOUNT_MSAT = 100_000_000  # 100,000 units of to_asset -- well within CHANNEL_UNITS either side
MARGINS = {'low': 2010, 'medium': 2030, 'high': 2060}  # pricetiers-plugin's own MARGINS


def tier_from_amount(to_amount_msat, tier_name, rate_ppm=DEFAULT_RATE_PPM,
                     fee_base_msat=DEFAULT_FEE_BASE_MSAT):
    tier_rate_ppm = rate_ppm * MARGINS[tier_name] // 1000
    return to_amount_msat * tier_rate_ppm // 1_000_000 + fee_base_msat


def test_cross_asset_swap_happy_path(node_factory, bitcoind):
    """Payer holds only asset_a, the invoice wants asset_b: leg 1 (l1->l2,
    asset_a) and leg 2 (l2->l3, asset_b) both settle, the receiver's
    invoice is paid, and l2's asset_a balance grew by the medium-tier
    (default) amount."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': 'swap-happy',
                                           'description': 'swap-happy',
                                           'asset': asset_b})
    res = l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                          'from_asset': asset_a})
    assert res['status'] == 'complete'
    settled(l1, l2, l3)

    assert only_one(l3.rpc.listinvoices('swap-happy')['invoices'])['status'] == 'paid'
    assert channel_by_funding(l2, funding12)['to_us_msat'] == tier_from_amount(TO_AMOUNT_MSAT, 'medium')


@pytest.mark.parametrize("tier_name", ['low', 'high'])
def test_cross_asset_swap_tier_selection(node_factory, bitcoind, tier_name):
    """A non-default tier name changes the leg-1 amount actually sent to
    exactly that tier's own rate -- not the default "medium"."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': f'swap-tier-{tier_name}',
                                           'description': f'swap-tier-{tier_name}',
                                           'asset': asset_b})
    res = l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                          'from_asset': asset_a, 'tier': tier_name})
    assert res['status'] == 'complete'
    settled(l1, l2, l3)

    assert channel_by_funding(l2, funding12)['to_us_msat'] == tier_from_amount(TO_AMOUNT_MSAT, tier_name)


def test_cross_asset_swap_unknown_tier_name(node_factory, bitcoind):
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': 'swap-bad-tier',
                                           'description': 'swap-bad-tier',
                                           'asset': asset_b})
    with pytest.raises(RpcError, match="no .*ultra.* tier for this pair"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                        'from_asset': asset_a, 'tier': 'ultra'})


def test_cross_asset_swap_from_asset_not_quoted_by_provider(node_factory, bitcoind):
    """The provider's pricetiers-pair quotes paying asset_b from asset_a
    only -- asking to pay with a third (unconfigured) asset must be
    rejected naming what the provider actually prices it in, not silently
    substituted."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': 'swap-wrong-from',
                                           'description': 'swap-wrong-from',
                                           'asset': asset_b})
    with pytest.raises(RpcError, match="provider prices it in"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                        'from_asset': 'lbtc'})


def test_cross_asset_swap_no_leg1_route_to_provider(node_factory, bitcoind):
    """from_asset is correctly the one the provider quotes, but the payer
    has no channel in it at all (only L-BTC to the provider): leg 1 can
    never be routed."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset_a = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    asset_b = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    pair = f'{asset_a}:{asset_b}:{DEFAULT_RATE_PPM}:{DEFAULT_FEE_BASE_MSAT}'
    l2 = node_factory.get_node(options={'plugin': [ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN],
                                        'pricetiers-pair': pair})
    l1, l3 = node_factory.get_nodes(2, opts=[
        {'plugin': ASSETSWAP_PLUGIN, 'assetswap-provider': l2.info['id']},
        {'plugin': ASSETSWAP_PLUGIN},
    ])
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)
    l1.fundwallet(10**7)
    l2.fundwallet(10**7)
    l1.rpc.fundchannel(l2.info['id'], 200_000)  # l1-l2: L-BTC only, no asset_a
    fund_asset_channel(bitcoind, l2, l3, asset_b, lbtc)
    bitcoind.generate_block(6)
    wait_for_channels_normal(l1, l2, l3)
    sync_blockheight(bitcoind, [l1, l2, l3])

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': 'swap-no-leg1-route',
                                           'description': 'swap-no-leg1-route',
                                           'asset': asset_b})
    with pytest.raises(RpcError, match=f"no leg-1 route to swap provider .* in asset {asset_a}"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                        'from_asset': asset_a})


def test_cross_asset_swap_leg1_amount_below_providers_floor(node_factory, bitcoind):
    """An explicit from_amount_msat below the provider's cheapest tier at
    its current price must be rejected live, at the provider -- it would
    be paying leg 2 at a loss. Covers the same check that protects a
    provider whose price moved between a payer's (unverified) local
    estimate and the moment leg 1 actually lands."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': 'swap-underpriced',
                                           'description': 'swap-underpriced',
                                           'asset': asset_b})
    floor = tier_from_amount(TO_AMOUNT_MSAT, 'low')
    with pytest.raises(RpcError, match="WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                        'from_asset': asset_a,
                                        'from_amount_msat': floor // 2})


def test_cross_asset_swap_leg2_unreachable_then_retry_succeeds(node_factory, bitcoind):
    """If leg 2 can't be routed (no channel from the provider to the
    receiver yet), the swap fails cleanly and the quote is marked
    "failed" -- a later retry with the SAME invoice (same payment_hash)
    must be judged fresh, not permanently wedged by the earlier verdict."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset_a = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    asset_b = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    pair = f'{asset_a}:{asset_b}:{DEFAULT_RATE_PPM}:{DEFAULT_FEE_BASE_MSAT}'
    l2 = node_factory.get_node(options={'plugin': [ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN],
                                        'pricetiers-pair': pair})
    l1, l3 = node_factory.get_nodes(2, opts=[
        {'plugin': ASSETSWAP_PLUGIN, 'assetswap-provider': l2.info['id']},
        {'plugin': ASSETSWAP_PLUGIN},
    ])
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.fundwallet(10**7)
    l2.fundwallet(10**7)
    fund_asset_channel(bitcoind, l1, l2, asset_a, lbtc)  # l2-l3 deliberately not set up yet
    bitcoind.generate_block(6)
    wait_for_channels_normal(l1, l2)
    sync_blockheight(bitcoind, [l1, l2])

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT,
                                           'label': 'swap-retry',
                                           'description': 'swap-retry',
                                           'asset': asset_b})

    with pytest.raises(RpcError, match="WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                        'from_asset': asset_a})

    # Now give the provider a real route to the receiver, and retry.
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)
    fund_asset_channel(bitcoind, l2, l3, asset_b, lbtc)
    bitcoind.generate_block(6)
    wait_for_channels_normal(l2, l3)
    sync_blockheight(bitcoind, [l1, l2, l3])

    res = l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'],
                                          'from_asset': asset_a})
    assert res['status'] == 'complete'
    settled(l1, l2, l3)
    assert only_one(l3.rpc.listinvoices('swap-retry')['invoices'])['status'] == 'paid'
