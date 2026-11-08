"""The swap-provider's htlc_accepted hook (htlc.rs) is the only thing
standing between a crafted leg-1 HTLC and a real payout -- these tests
bypass payassetinvoice entirely and craft leg 1 by hand (manual sendpay,
same technique as test_liquid_asset_poc.py's pay_via) to exercise the
hook's own defenses directly: a buggy or malicious payer must never be
able to make the provider pay leg 2 it shouldn't.

    TEST_NETWORK=liquid-regtest pytest plugins/assetswap-plugin/tests/test_htlc_security.py
"""
from fixtures import *  # noqa: F401,F403
from helpers import encode_swap_metadata, setup_swap_topology
from test_liquid_asset_poc import channel_by_funding, LBTC_CHANNEL_SAT
from pyln.client import RpcError
from utils import TEST_NETWORK, TIMEOUT, only_one, wait_for

import pytest


pytestmark = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                reason="Liquid asset channels need elementsd")

TO_AMOUNT_MSAT = 100_000_000
GENEROUS_LEG1_MSAT = 3 * TO_AMOUNT_MSAT  # comfortably above any tier floor
GENEROUS_DELAY = 500  # comfortably above safety_blocks(18) + min_hop_budget(34) + any final_cltv


def test_htlc_rejects_payment_hash_mismatch(node_factory, bitcoind):
    """Leg 1's own payment_hash must match the payment_hash of the
    invoice named in its payment_metadata -- that link is what the whole
    swap's atomicity rests on (pay.rs's own doc comment). A leg 1 whose
    metadata points at a DIFFERENT invoice than the hash it carries must
    be rejected before the provider ever considers paying leg 2."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    real_invoice = l3.rpc.call('invoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'hash-mismatch-real',
                                           'description': 'real', 'asset': asset_b})
    metadata = encode_swap_metadata(real_invoice['bolt11'], asset_b)
    mismatched_hash = '02' * 32  # not real_invoice's own payment_hash

    scid = channel_by_funding(l1, funding12)['short_channel_id']
    route = [{'id': l2.info['id'], 'channel': scid,
             'amount_msat': GENEROUS_LEG1_MSAT, 'delay': GENEROUS_DELAY}]
    l1.rpc.sendpay(route, mismatched_hash, payment_metadata=metadata,
                   amount_msat=GENEROUS_LEG1_MSAT)
    with pytest.raises(RpcError, match="WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"):
        l1.rpc.waitsendpay(mismatched_hash)

    # No leg 2 was ever attempted for that invoice.
    assert l2.rpc.listsendpays(bolt11=real_invoice['bolt11'])['payments'] == []


def test_htlc_rejects_wrong_incoming_channel_asset(node_factory, bitcoind):
    """The provider must only accept leg 1 over a channel actually tagged
    the asset it's pricing from -- never pay out leg 2 just because a
    (mismatched) HTLC arrived with the right amount and a well-formed,
    hash-matching metadata blob, over the WRONG channel."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)
    l1.rpc.fundchannel(l2.info['id'], LBTC_CHANNEL_SAT)  # a second, L-BTC channel l1-l2
    bitcoind.generate_block(6)
    wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL' for c in l1.rpc.listpeerchannels()['channels']))

    real_invoice = l3.rpc.call('invoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'wrong-chan-asset',
                                           'description': 'real', 'asset': asset_b})
    metadata = encode_swap_metadata(real_invoice['bolt11'], asset_b)

    lbtc_chan = only_one([c for c in l1.rpc.listpeerchannels()['channels']
                         if c['peer_id'] == l2.info['id'] and c.get('liquid_asset') is None])
    route = [{'id': l2.info['id'], 'channel': lbtc_chan['short_channel_id'],
             'amount_msat': GENEROUS_LEG1_MSAT, 'delay': GENEROUS_DELAY}]
    l1.rpc.sendpay(route, real_invoice['payment_hash'], payment_metadata=metadata,
                   amount_msat=GENEROUS_LEG1_MSAT)
    with pytest.raises(RpcError, match="WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"):
        l1.rpc.waitsendpay(real_invoice['payment_hash'])

    assert l2.rpc.listsendpays(bolt11=real_invoice['bolt11'])['payments'] == []


def test_htlc_rejects_insufficient_cltv_budget(node_factory, bitcoind):
    """An otherwise-valid leg 1 with too little CLTV margin over leg 2's
    own final_cltv_expiry must be rejected up front, before even
    attempting leg 2 -- not accepted and then raced against the clock."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    real_invoice = l3.rpc.call('invoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'tight-cltv',
                                           'description': 'real', 'asset': asset_b})
    metadata = encode_swap_metadata(real_invoice['bolt11'], asset_b)
    leg2_final_cltv = l3.rpc.decode(real_invoice['bolt11'])['min_final_cltv_expiry']

    scid = channel_by_funding(l1, funding12)['short_channel_id']
    # Enough to satisfy lightningd's own base final-cltv sanity check
    # (it incorporates the invoice's own min_final_cltv_expiry), but well
    # under this plugin's own budget of leg2_final_cltv + 18 + 34 -- the
    # margin this specific check exists to enforce.
    tiny_delay = leg2_final_cltv + 5
    route = [{'id': l2.info['id'], 'channel': scid,
             'amount_msat': GENEROUS_LEG1_MSAT, 'delay': tiny_delay}]
    l1.rpc.sendpay(route, real_invoice['payment_hash'], payment_metadata=metadata,
                   amount_msat=GENEROUS_LEG1_MSAT)
    with pytest.raises(RpcError, match="WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"):
        l1.rpc.waitsendpay(real_invoice['payment_hash'])

    assert l2.rpc.listsendpays(bolt11=real_invoice['bolt11'])['payments'] == []


def test_htlc_ignores_plain_payment_to_provider(node_factory, bitcoind):
    """A plain payment terminating at the provider's own node, carrying
    no payment_metadata at all, must settle normally -- the hook must
    "continue" rather than second-guess ordinary traffic that was never
    meant to be a swap."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    l2_invoice = l2.rpc.call('invoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'plain-to-provider',
                                         'description': 'plain', 'asset': asset_a})
    res = l1.rpc.call('xpay', {'invstring': l2_invoice['bolt11'], 'asset': asset_a})
    assert res['failed_parts'] == 0
    assert only_one(l2.rpc.listinvoices('plain-to-provider')['invoices'])['status'] == 'paid'


def test_htlc_ignores_garbage_metadata(node_factory, bitcoind):
    """A final-hop HTLC to the provider carrying payment_metadata that
    isn't valid swap metadata (garbage bytes, not hex-JSON matching
    SwapMetadata) must fall through and settle as the ordinary payment it
    actually is, not get caught by the hook's own parsing."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    l2_invoice = l2.rpc.call('invoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'garbage-metadata',
                                         'description': 'garbage', 'asset': asset_a})
    scid = channel_by_funding(l1, funding12)['short_channel_id']
    route = [{'id': l2.info['id'], 'channel': scid,
             'amount_msat': TO_AMOUNT_MSAT, 'delay': GENEROUS_DELAY}]
    l1.rpc.sendpay(route, l2_invoice['payment_hash'], payment_metadata='deadbeef',
                   payment_secret=l2_invoice['payment_secret'], amount_msat=TO_AMOUNT_MSAT)
    res = l1.rpc.waitsendpay(l2_invoice['payment_hash'])
    assert res['status'] == 'complete'
    assert only_one(l2.rpc.listinvoices('garbage-metadata')['invoices'])['status'] == 'paid'


def test_htlc_rejects_duplicate_leg1_same_payment_hash(node_factory, bitcoind, executor):
    """Two leg-1 HTLCs racing in with the SAME payment_hash (a retried or
    replayed attempt) must never result in leg 2 being paid out twice --
    only one may ever win the provider's atomic claim."""
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    real_invoice = l3.rpc.call('invoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'race',
                                           'description': 'real', 'asset': asset_b})
    metadata = encode_swap_metadata(real_invoice['bolt11'], asset_b)
    scid = channel_by_funding(l1, funding12)['short_channel_id']

    def attempt(partid):
        route = [{'id': l2.info['id'], 'channel': scid,
                 'amount_msat': GENEROUS_LEG1_MSAT, 'delay': GENEROUS_DELAY}]
        l1.rpc.sendpay(route, real_invoice['payment_hash'], payment_metadata=metadata,
                       amount_msat=GENEROUS_LEG1_MSAT, partid=partid, groupid=1)
        try:
            return l1.rpc.waitsendpay(real_invoice['payment_hash'], partid=partid)
        except RpcError as e:
            return e

    fut1 = executor.submit(attempt, 1)
    fut2 = executor.submit(attempt, 2)
    res1, res2 = fut1.result(TIMEOUT), fut2.result(TIMEOUT)

    outcomes = [r for r in (res1, res2) if not isinstance(r, RpcError)]
    failures = [r for r in (res1, res2) if isinstance(r, RpcError)]
    # Whatever the exact race outcome, leg 2 was never paid out twice.
    assert len(l2.rpc.listsendpays(bolt11=real_invoice['bolt11'])['payments']) == 1
    assert len(outcomes) >= 1
    for r in outcomes:
        assert r['status'] == 'complete'
    for e in failures:
        assert 'WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS' in str(e)
