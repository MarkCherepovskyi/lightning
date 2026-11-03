"""assetswap-plugin: pay an invoice that requires a different Liquid asset
than you hold, via a swap-provider node. See
contrib/liquid-poc/CROSS_ASSET_SWAP.md for the design.

This file currently covers steps 1-2 of that doc's implementation sequence
only: the `assetinvoice` wrapper, and `payassetinvoice`'s same-asset
fallback path. Later steps (the custommsg quote protocol, the
htlc_accepted hold/resolve hook, price tiers) get their own tests once
implemented.

    TEST_NETWORK=liquid-regtest pytest tests/test_assetswap.py
"""
from fixtures import *  # noqa: F401,F403
from test_liquid_asset_poc import (LBTC_CHANNEL_SAT, asset_invoice,
                                   fund_asset_channel, settled)
from utils import TEST_NETWORK

import base64
import json
import os
import pytest


pytestmark = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                reason="Liquid asset channels need elementsd")

ASSETSWAP_PLUGIN = os.path.join(os.getcwd(), 'target/debug/assetswap-plugin')


def decode_assetinvoice(encoded):
    padded = encoded + '=' * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def test_assetswap_invoice_wrapper_roundtrip(node_factory, bitcoind):
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, = node_factory.get_nodes(1, opts={'plugin': ASSETSWAP_PLUGIN})

    amount_msat = 1_000_000
    res = l1.rpc.call('assetinvoice', {'amount_msat': amount_msat,
                                       'label': 'inv1', 'description': 'inv1',
                                       'asset': asset})
    wrapper = decode_assetinvoice(res['assetinvoice'])

    assert wrapper['v'] == 1
    assert wrapper['asset'] == asset
    assert wrapper['amount_msat'] == amount_msat

    decoded = l1.rpc.decode(wrapper['bolt11'])
    assert decoded['payment_hash'] == wrapper['payment_hash']
    assert decoded['amount_msat'] == amount_msat


def test_assetswap_same_asset_fallback(node_factory, bitcoind):
    """payassetinvoice on an invoice the payer can already reach directly
    (no swap needed) should settle via the plain xpay path."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts={'plugin': ASSETSWAP_PLUGIN})
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.fundwallet(10**7)
    fund_asset_channel(bitcoind, l1, l2, asset, lbtc)

    inv = asset_invoice(l2, 'same-asset', asset)
    wrapped = l2.rpc.call('assetinvoice', {'amount_msat': inv['amount_msat'],
                                           'label': 'same-asset-2',
                                           'description': 'same-asset-2',
                                           'asset': asset})

    res = l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice']})
    assert res['amount_msat'] == inv['amount_msat']
    settled(l1, l2)


def test_assetswap_cross_asset_not_yet_implemented(node_factory, bitcoind):
    """Until the swap-provider quote protocol and htlc_accepted hook land
    (CROSS_ASSET_SWAP.md steps 3+), paying an invoice in an asset the payer
    can't directly reach must fail loudly, not silently do nothing."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts={'plugin': ASSETSWAP_PLUGIN})
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.fundwallet(10**7)
    # Only l2 has the asset channel — l1 can't reach it directly.
    fund_asset_channel(bitcoind, l2, l1, asset, lbtc)
    l1.rpc.fundchannel(l2.info['id'], LBTC_CHANNEL_SAT)  # l1-l2 L-BTC only

    wrapped = l2.rpc.call('assetinvoice', {'amount_msat': 1_000_000,
                                           'label': 'cross-asset',
                                           'description': 'cross-asset',
                                           'asset': asset})

    with pytest.raises(Exception, match="not implemented yet"):
        l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice']})
