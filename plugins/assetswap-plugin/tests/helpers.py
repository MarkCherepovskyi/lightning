"""Shared setup for assetswap-plugin's own tests (see CROSS_ASSET_SWAP.md
for the design these exercise). conftest.py in this directory puts
tests/ on sys.path so the plain `from fixtures import *` convention used
by every test_*.py here, and the imports below, both resolve.
"""
from test_liquid_asset_poc import fund_asset_channel
from utils import wait_for, sync_blockheight

import base64
import json
import os


ASSETSWAP_PLUGIN = os.path.join(os.getcwd(), 'target/debug/assetswap-plugin')
PRICETIERS_PLUGIN = os.path.join(os.getcwd(), 'target/debug/pricetiers-plugin')

# 1:1 reference rate between the two test assets by default, so tier
# amounts are easy to reason about in assertions (medium tier = 2030/1000
# of that, per pricetiers-plugin's MARGINS).
DEFAULT_RATE_PPM = 1_000_000
DEFAULT_FEE_BASE_MSAT = 0


def decode_assetinvoice(encoded):
    padded = encoded + '=' * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def encode_swap_metadata(final_bolt11, to_asset):
    """Mirrors assetswap-plugin's own protocol::encode_swap_metadata:
    hex(json({final_bolt11, to_asset})). Tests that craft a leg-1 HTLC by
    hand (manual sendpay, not payassetinvoice) use this to exercise
    htlc_accepted's own defenses directly."""
    return json.dumps({'final_bolt11': final_bolt11, 'to_asset': to_asset}).encode().hex()


def wait_for_channels_normal(*nodes):
    for n in nodes:
        wait_for(lambda n=n: all(c['state'] == 'CHANNELD_NORMAL'
                                 for c in n.rpc.listpeerchannels()['channels']))


def setup_swap_topology(node_factory, bitcoind, rate_ppm=DEFAULT_RATE_PPM,
                        fee_base_msat=DEFAULT_FEE_BASE_MSAT,
                        payer_opts=None, provider_opts=None, receiver_opts=None):
    """The standard topology most tests here use:

        l1 (payer) --asset_a--> l2 (provider) --asset_b--> l3 (receiver)

    l2 is started first (and configured with pricetiers-plugin's
    pricetiers-pair=asset_a:asset_b:rate_ppm:fee_base_msat) so its node_id
    is known before l1 is configured with assetswap-provider=<l2's id> --
    there's no way to set that option after l1 has already started
    without a restart, so it has to be known up front.

    Returns (l1, l2, l3, asset_a, asset_b, lbtc).
    """
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset_a = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    asset_b = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    pair = f'{asset_a}:{asset_b}:{rate_ppm}:{fee_base_msat}'
    l2 = node_factory.get_node(options={
        **(provider_opts or {}),
        'plugin': [ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN],
        'pricetiers-pair': pair,
    })

    # l1 also needs pricetiers-plugin with the SAME pair l2 (the provider)
    # uses: payassetinvoice's local pricing lookup (pay.rs) queries this
    # node's own pricetiers-plugin for to_asset=asset_b, and checks that
    # the from_asset it returns matches what the caller asked to pay with
    # -- exactly the "set deliberately, e.g. by the harness that also set
    # up the provider" case documented on quoteassetinvoice.
    l1_opts = {**(payer_opts or {}), 'plugin': [ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN],
              'pricetiers-pair': pair,
              'assetswap-provider': l2.info['id']}
    l3_opts = {**(receiver_opts or {}), 'plugin': ASSETSWAP_PLUGIN}
    l1, l3 = node_factory.get_nodes(2, opts=[l1_opts, l3_opts])

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)
    l1.fundwallet(10**7)
    l2.fundwallet(10**7)
    funding12 = fund_asset_channel(bitcoind, l1, l2, asset_a, lbtc)
    funding23 = fund_asset_channel(bitcoind, l2, l3, asset_b, lbtc)

    bitcoind.generate_block(6)
    wait_for_channels_normal(l1, l2, l3)
    sync_blockheight(bitcoind, [l1, l2, l3])

    return l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23
