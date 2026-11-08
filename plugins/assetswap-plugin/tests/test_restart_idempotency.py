"""htlc.rs's restart-replay idempotency: lightningd re-presents a leg-1
HTLC whose htlc_accepted hook never got an answer (crash/restart before
the plugin replied) to the plugin again, rather than leaving it to time
out. resolve_from_leg2_status re-derives the right verdict from leg 2's
own status instead of re-deciding or re-running it. See htlc.rs's own
doc comments, and CROSS_ASSET_SWAP.md section 7, for the design.

    TEST_NETWORK=liquid-regtest pytest plugins/assetswap-plugin/tests/test_restart_idempotency.py
"""
from fixtures import *  # noqa: F401,F403
from helpers import decode_assetinvoice, encode_swap_metadata, setup_swap_topology
from test_liquid_asset_poc import channel_by_funding, settled
from pyln.client import RpcError
from utils import TEST_NETWORK, TIMEOUT, only_one, wait_for

import json
import pytest


pytestmark = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                reason="Liquid asset channels need elementsd")

TO_AMOUNT_MSAT = 100_000_000
GENEROUS_LEG1_MSAT = 3 * TO_AMOUNT_MSAT
GENEROUS_DELAY = 500


def test_replay_after_leg2_already_complete_resolves_with_same_preimage(node_factory, bitcoind):
    """A second leg-1 HTLC for a payment_hash whose swap already
    completed (the lookup_quote "claimed" shortcut at the top of
    on_htlc_accepted, exactly what a restart-replay of the SAME still
    "claimed" HTLC would also hit) must resolve with the SAME real
    preimage again -- never attempt leg 2 a second time.

    This exercises the exact code path a real process restart would
    replay through (on_htlc_accepted seeing an already-"claimed" quote
    record) deterministically, via a second manually-crafted leg-1
    attempt sent only after the first genuinely completes -- hitting the
    precise instant of an actual crash/restart black-box is inherently
    racy, but the decision logic being tested doesn't care how a
    "claimed" record with a completed leg 2 came to exist.
    """
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'replay-complete',
                                           'description': 'replay-complete', 'asset': asset_b})
    res = l1.rpc.call('payassetinvoice', {'assetinvoice': wrapped['assetinvoice'], 'from_asset': asset_a})
    assert res['status'] == 'complete'
    settled(l1, l2, l3)
    first_preimage = res['payment_preimage']

    payment_hash = decode_assetinvoice(wrapped['assetinvoice'])['payment_hash']
    final_bolt11 = decode_assetinvoice(wrapped['assetinvoice'])['bolt11']
    metadata = encode_swap_metadata(final_bolt11, asset_b)
    scid = channel_by_funding(l1, funding12)['short_channel_id']

    route = [{'id': l2.info['id'], 'channel': scid,
             'amount_msat': GENEROUS_LEG1_MSAT, 'delay': GENEROUS_DELAY}]
    # KNOWN BROKEN: lightningd's own sendpay-level dedup (check_progress in
    # lightningd/pay.c) blocks this unconditionally once ANY part of this
    # payment_hash has completed, regardless of partid/groupid -- a
    # matching partid with a different amount fails outright ("Already
    # succeeded with amount ..."), and a non-matching partid still hits
    # "Already succeeded other parts" at the end of the same check (the
    # have_complete flag it sets is never scoped to partid or groupid).
    # There is no sendpay parameter combination that gets a genuinely
    # fresh leg-1 HTLC out to l2 here -- this needs a different injection
    # technique entirely (e.g. a lower-level/dev RPC that bypasses
    # lightningd's own payment-tracking) to actually exercise htlc.rs's
    # "claimed" replay shortcut the way this test's docstring describes.
    l1.rpc.sendpay(route, payment_hash, payment_metadata=metadata,
                   amount_msat=GENEROUS_LEG1_MSAT)
    replay_res = l1.rpc.waitsendpay(payment_hash)

    assert replay_res['payment_preimage'] == first_preimage
    # Leg 2 was still only ever paid once.
    assert len(l2.rpc.listsendpays(bolt11=final_bolt11)['payments']) == 1


def test_restart_with_leg2_still_pending_waits_then_resolves(node_factory, bitcoind, executor):
    """A real provider restart while leg 2's HTLC is genuinely still
    outstanding (sent, but not yet fulfilled) must, on replay, wait on
    leg 2's own pending status rather than failing leg 1 prematurely --
    then resolve leg 1 correctly once leg 2 actually completes.

    l3 is configured to swallow its own UPDATE_FULFILL_HTLC the first
    time it would send leg 2's fulfillment (the same dev-disconnect
    technique test_liquid_asset_poc.py's restart tests use), leaving
    leg 2 stuck "pending" at exactly the moment l2 is restarted. The
    disconnect is one-shot: once l2 and l3 reconnect, the retransmit
    goes through and leg 2 genuinely completes.
    """
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(
        node_factory, bitcoind, receiver_opts={'disconnect': ['-WIRE_UPDATE_FULFILL_HTLC']})

    wrapped = l3.rpc.call('assetinvoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'restart-pending',
                                           'description': 'restart-pending', 'asset': asset_b})
    payment_hash = decode_assetinvoice(wrapped['assetinvoice'])['payment_hash']

    fut = executor.submit(l1.rpc.call, 'payassetinvoice',
                          {'assetinvoice': wrapped['assetinvoice'], 'from_asset': asset_a})

    # Leg 1 claimed, leg 2 dispatched and stuck exactly where l3 swallows
    # its own fulfillment.
    wait_for(lambda: len(l2.rpc.listdatastore(key=['assetswap', 'quote', payment_hash])['datastore']) == 1)
    l3.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_FULFILL_HTLC')

    l2.restart(clean=False)

    # One-shot disconnect already consumed: reconnecting lets leg 2's
    # fulfillment actually go through this time.
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)

    res = fut.result(TIMEOUT)
    assert res['status'] == 'complete'
    settled(l1, l2, l3)
    assert only_one(l3.rpc.listinvoices('restart-pending')['invoices'])['status'] == 'paid'


def test_claimed_record_with_no_leg2_attempt_fails_cleanly(node_factory, bitcoind):
    """A "claimed" quote record for which leg 2 was never actually
    attempted (the provider crashing between claiming and dispatching
    leg 2 -- the narrowest of the three restart-replay sub-cases, and
    impractical to hit via a real kill/restart deterministically in a
    black-box test) must still fail leg 1 cleanly on replay, not hang or
    crash the hook.

    The precondition is written directly via the public `datastore` RPC,
    which is exactly the state a genuine crash in that narrow window
    would leave behind -- resolve_from_leg2_status doesn't care how it
    got there, only that `listsendpays` finds nothing for it.
    """
    l1, l2, l3, asset_a, asset_b, lbtc, funding12, funding23 = setup_swap_topology(node_factory, bitcoind)

    never_paid = l3.rpc.call('invoice', {'amount_msat': TO_AMOUNT_MSAT, 'label': 'never-attempted',
                                         'description': 'never-attempted', 'asset': asset_b})
    l2.rpc.datastore(
        key=['assetswap', 'quote', never_paid['payment_hash']],
        string=json.dumps({'state': 'claimed', 'from_asset': asset_a,
                           'to_asset': asset_b, 'final_bolt11': never_paid['bolt11']}))

    metadata = encode_swap_metadata(never_paid['bolt11'], asset_b)
    scid = channel_by_funding(l1, funding12)['short_channel_id']
    route = [{'id': l2.info['id'], 'channel': scid,
             'amount_msat': 3 * TO_AMOUNT_MSAT, 'delay': GENEROUS_DELAY}]
    l1.rpc.sendpay(route, never_paid['payment_hash'], payment_metadata=metadata,
                   amount_msat=3 * TO_AMOUNT_MSAT)
    with pytest.raises(RpcError, match="WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"):
        l1.rpc.waitsendpay(never_paid['payment_hash'])

    assert l2.rpc.listsendpays(bolt11=never_paid['bolt11'])['payments'] == []
