"""More Liquid asset tests: the wallet side (assets paid to our keys, spending
them with `withdrawasset`), the bookkeeper on asset channels, and limits."""
from fixtures import *  # noqa: F401,F403
from decimal import Decimal
from pyln.client import RpcError
from test_liquid_asset_poc import (
    CHANNEL_UNITS, PAY_MSAT, close_and_mine, htlc_in_flight, invoice_units,
    mine_until_forgotten, open_asset_channel, send_asset, settled,
    unconfidential, units,
)
from utils import only_one, sync_blockheight, wait_for, TEST_NETWORK

import pytest

needs_elements = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                    reason="Liquid asset channels need elementsd")


def new_asset(bitcoind):
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)
    return asset


def pay_asset_to_wallet(bitcoind, node, asset, amount_units):
    """elementsd pays `amount_units` of `asset` to a new (unblinded) address of
    the node's wallet.  Returns the txid."""
    addr = node.rpc.newaddr('bech32')['bech32']
    return bitcoind.rpc.sendtoaddress(addr, str(Decimal(amount_units) / 10**8),
                                      "", "", False, False, None, None, None, asset)


def asset_outputs(node, asset, spent=False, status=None):
    outs = [o for o in node.rpc.call('listassetfunds', {'spent': spent})['outputs']
            if o['asset'] == asset]
    if status:
        outs = [o for o in outs if o['status'] == status]
    return outs


def confirmed_units(node, asset):
    return sorted(o['amount'] for o in asset_outputs(node, asset, status='confirmed'))


def mine_and_sync(bitcoind, node, n=1):
    bitcoind.generate_block(n)
    sync_blockheight(bitcoind, [node])


def wait_confirmed(node, asset, want):
    wait_for(lambda: confirmed_units(node, asset) == sorted(want))


def vout_units(tx, asset):
    return sorted(units(v) for v in tx['vout'] if v.get('asset') == asset)


def withdraw(node, address, asset, amount, **kwargs):
    return node.rpc.call('withdrawasset', {'destination': address, 'asset': asset,
                                           'amount': amount, **kwargs})


# --------------------------------------------------------------- wallet
@needs_elements
def test_liquid_asset_wallet_receives_assets(node_factory, bitcoind):
    """Assets paid to a wallet address are listed by listassetfunds (and not
    by listfunds, which is L-BTC); the L-BTC balance isn't affected."""
    asset, other = new_asset(bitcoind), new_asset(bitcoind)
    l1 = node_factory.get_node()
    l1.fundwallet(10**6)
    lbtc_before = l1.rpc.listfunds()['outputs']

    for amount in (100_000, 250_000, 1):
        pay_asset_to_wallet(bitcoind, l1, asset, amount)
    pay_asset_to_wallet(bitcoind, l1, other, 7_000)
    mine_and_sync(bitcoind, l1)

    wait_confirmed(l1, asset, [1, 100_000, 250_000])
    wait_confirmed(l1, other, [7_000])
    assert asset_outputs(l1, asset, spent=True, status='spent') == []

    # listfunds only knows L-BTC: unchanged.
    assert l1.rpc.listfunds()['outputs'] == lbtc_before
    for o in asset_outputs(l1, asset):
        assert o['blockheight'] == bitcoind.rpc.getblockcount()
        assert len(o['txid']) == 64 and len(o['scriptpubkey']) > 0


@needs_elements
def test_liquid_asset_withdraw_basic(node_factory, bitcoind):
    """withdrawasset sends part of an asset output: the asset to the
    destination, the rest back to the wallet, the fee paid in L-BTC."""
    asset = new_asset(bitcoind)
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    l1 = node_factory.get_node()
    l1.fundwallet(10**6)
    pay_asset_to_wallet(bitcoind, l1, asset, 1_000_000)
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [1_000_000])

    dest, dest_spk = unconfidential(bitcoind)
    res = withdraw(l1, dest, asset, 400_000)
    tx = bitcoind.rpc.getrawtransaction(res['txid'], True)

    assert vout_units(tx, asset) == [400_000, 600_000]
    assert only_one([v for v in tx['vout'] if v.get('asset') == asset
                     and units(v) == 400_000])['scriptPubKey']['hex'] == dest_spk.hex()
    # The fee is an explicit L-BTC output with no script, and L-BTC comes back.
    fees = [v for v in tx['vout'] if v['scriptPubKey']['hex'] == '']
    assert len(fees) == 1 and fees[0]['asset'] == lbtc
    assert len(vout_units(tx, lbtc)) == 2
    assert len(tx['vin']) >= 2

    mine_and_sync(bitcoind, l1)
    # The change is ours and confirmed; the spent output is marked spent.
    wait_confirmed(l1, asset, [600_000])
    wait_for(lambda: [o['amount'] for o in asset_outputs(l1, asset, spent=True, status='spent')] == [1_000_000])
    assert only_one(asset_outputs(l1, asset, spent=True, status='spent'))['spendheight'] == bitcoind.rpc.getblockcount()


@needs_elements
def test_liquid_asset_withdraw_errors(node_factory, bitcoind):
    asset = new_asset(bitcoind)
    other = new_asset(bitcoind)
    l1 = node_factory.get_node()
    l1.fundwallet(10**6)
    pay_asset_to_wallet(bitcoind, l1, asset, 100_000)
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [100_000])
    dest, _ = unconfidential(bitcoind)
    before = bitcoind.rpc.getrawmempool()

    with pytest.raises(RpcError, match='non-zero'):
        withdraw(l1, dest, asset, 0)
    with pytest.raises(RpcError, match='asset id'):
        withdraw(l1, dest, 'nothex', 1000)
    with pytest.raises(RpcError, match='asset id'):
        withdraw(l1, dest, asset[:-2], 1000)
    with pytest.raises(RpcError):
        withdraw(l1, 'notanaddress', asset, 1000)
    # More than we have, or an asset we don't have.
    with pytest.raises(RpcError, match='Cannot afford'):
        withdraw(l1, dest, asset, 100_001)
    with pytest.raises(RpcError, match='Cannot afford'):
        withdraw(l1, dest, other, 1)

    # Nothing was broadcast, nothing was reserved.
    assert bitcoind.rpc.getrawmempool() == before
    assert confirmed_units(l1, asset) == [100_000]
    withdraw(l1, dest, asset, 100_000)


@needs_elements
def test_liquid_asset_withdraw_many_inputs(node_factory, bitcoind):
    """A withdrawal that needs several asset outputs, then one that spends
    exactly what is left (no asset change).  (The list of inputs grows while
    the tx is built: this once crashed lightningd.)"""
    asset = new_asset(bitcoind)
    l1 = node_factory.get_node()
    for _ in range(2):
        l1.fundwallet(10**6)
    for _ in range(5):
        pay_asset_to_wallet(bitcoind, l1, asset, 100_000)
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [100_000] * 5)
    dest, dest_spk = unconfidential(bitcoind)

    res = withdraw(l1, dest, asset, 450_000)
    tx = bitcoind.rpc.getrawtransaction(res['txid'], True)
    assert vout_units(tx, asset) == [50_000, 450_000]
    # Five asset inputs, and the L-BTC for the fee.
    assert len(tx['vin']) >= 6
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [50_000])

    # Exactly the balance: a single asset output, no change.
    res = withdraw(l1, dest, asset, 50_000)
    tx = bitcoind.rpc.getrawtransaction(res['txid'], True)
    assert vout_units(tx, asset) == [50_000]
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [])
    with pytest.raises(RpcError, match='Cannot afford'):
        withdraw(l1, dest, asset, 1)


@needs_elements
def test_liquid_asset_withdraw_reserves_outputs(node_factory, bitcoind):
    """Back to back withdrawals (before a block) don't spend the same
    output twice: the first reserves what it uses."""
    asset = new_asset(bitcoind)
    l1 = node_factory.get_node()
    for _ in range(3):
        l1.fundwallet(10**6)
    pay_asset_to_wallet(bitcoind, l1, asset, 100_000)
    pay_asset_to_wallet(bitcoind, l1, asset, 100_000)
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [100_000, 100_000])
    dest, _ = unconfidential(bitcoind)

    t1 = withdraw(l1, dest, asset, 100_000)['txid']
    t2 = withdraw(l1, dest, asset, 100_000)['txid']
    assert t1 != t2
    vins = [{(i['txid'], i['vout']) for i in bitcoind.rpc.getrawtransaction(t, True)['vin']}
            for t in (t1, t2)]
    assert not (vins[0] & vins[1])
    # Everything is reserved now.
    with pytest.raises(RpcError, match='Cannot afford'):
        withdraw(l1, dest, asset, 1)

    bitcoind.generate_block(1, wait_for_mempool=2)
    sync_blockheight(bitcoind, [l1])
    wait_confirmed(l1, asset, [])


@needs_elements
def test_liquid_asset_withdraw_needs_lbtc(node_factory, bitcoind):
    """The fee can only be paid in L-BTC: with none in the wallet the
    withdrawal fails cleanly and keeps the asset."""
    asset = new_asset(bitcoind)
    l1 = node_factory.get_node()
    pay_asset_to_wallet(bitcoind, l1, asset, 100_000)
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [100_000])
    dest, _ = unconfidential(bitcoind)

    with pytest.raises(RpcError, match='Not enough L-BTC'):
        withdraw(l1, dest, asset, 50_000)
    assert confirmed_units(l1, asset) == [100_000]

    # Give it some L-BTC and it works.
    l1.fundwallet(10**6)
    withdraw(l1, dest, asset, 50_000)


@needs_elements
def test_liquid_asset_withdraw_ignores_unconfirmed(node_factory, bitcoind):
    """Only confirmed outputs are spent."""
    asset = new_asset(bitcoind)
    l1 = node_factory.get_node()
    l1.fundwallet(10**6)
    pay_asset_to_wallet(bitcoind, l1, asset, 100_000)
    dest, _ = unconfidential(bitcoind)

    with pytest.raises(RpcError, match='Cannot afford'):
        withdraw(l1, dest, asset, 1000)

    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [100_000])
    withdraw(l1, dest, asset, 1000)


@needs_elements
def test_liquid_asset_withdraw_survives_restart(node_factory, bitcoind):
    """Asset outputs are in the database: still there after a restart, and
    still spendable."""
    asset = new_asset(bitcoind)
    l1 = node_factory.get_node()
    l1.fundwallet(10**6)
    pay_asset_to_wallet(bitcoind, l1, asset, 300_000)
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [300_000])

    l1.restart()
    wait_confirmed(l1, asset, [300_000])
    dest, _ = unconfidential(bitcoind)
    withdraw(l1, dest, asset, 100_000)
    mine_and_sync(bitcoind, l1)
    wait_confirmed(l1, asset, [200_000])

    # The spent output is remembered as spent.
    l1.restart()
    wait_confirmed(l1, asset, [200_000])
    wait_for(lambda: [o['amount'] for o in asset_outputs(l1, asset, spent=True, status='spent')] == [300_000])


# ----------------------------------------------------------- bookkeeper
def balances(node):
    return {a['account']: a['balances'][0]['balance_msat'] // 1000
            for a in node.rpc.call('bkpr-listbalances', {})['accounts']}


def channel_balance_zero(node, channel_id):
    wait_for(lambda: balances(node).get(channel_id) == 0)


@needs_elements
def test_liquid_asset_bkpr_mutual_close(node_factory, bitcoind):
    """A cooperative close balances the channel account on both sides, in
    asset units."""
    l1, l2, lbtc, asset = open_asset_channel(node_factory, bitcoind, [{}, {}])
    cid = only_one(l1.rpc.listpeerchannels()['channels'])['channel_id']
    inv = invoice_units(l2, 'p', asset, PAY_MSAT)
    send_asset(l1, l2, PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)

    # Open: the opener's account starts with the whole channel.
    wait_for(lambda: balances(l1).get(cid) == CHANNEL_UNITS - PAY_MSAT // 1000)
    assert balances(l2).get(cid) == PAY_MSAT // 1000

    l1.rpc.close(l2.info['id'])
    bitcoind.generate_block(1, wait_for_mempool=1)
    sync_blockheight(bitcoind, [l1, l2])
    for n in (l1, l2):
        channel_balance_zero(n, cid)


@needs_elements
@pytest.mark.parametrize("outcome", ["timeout", "success"])
def test_liquid_asset_bkpr_htlc_force_close(node_factory, bitcoind, outcome):
    """The payer force-closes with an HTLC in flight; it times out, or the
    payee takes it with the preimage.  Either way both channel accounts
    balance to zero, and bookkeeper stays up (an unbalanced account makes it
    shut the node down)."""
    disconnect = (['-WIRE_UPDATE_FAIL_HTLC'] if outcome == 'timeout'
                  else ['-WIRE_UPDATE_FULFILL_HTLC*2'])
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind, [{}, {'disconnect': disconnect}])
    cid = only_one(l1.rpc.listpeerchannels()['channels'])['channel_id']
    htlc_in_flight(l1, l2, asset, outcome)

    close_and_mine(bitcoind, l1, l2)
    mine_until_forgotten(bitcoind, [l1, l2])
    for n in (l1, l2):
        channel_balance_zero(n, cid)


@needs_elements
def test_liquid_asset_bkpr_wallet_unaffected(node_factory, bitcoind):
    """Asset movements stay out of the L-BTC wallet account."""
    l1, l2, lbtc, asset = open_asset_channel(node_factory, bitcoind, [{}, {}])
    before = {n: balances(n).get('wallet') for n in (l1, l2)}
    inv = invoice_units(l2, 'p', asset, PAY_MSAT)
    send_asset(l1, l2, PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    # (an off-chain payment of the asset moves no L-BTC)
    assert {n: balances(n).get('wallet') for n in (l1, l2)} == before


# --------------------------------------------------------------- limits
@needs_elements
def test_liquid_asset_open_limits(node_factory, bitcoind):
    asset = new_asset(bitcoind)
    l1, l2 = node_factory.get_nodes(2)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l1.fundwallet(10**6)
    peer = l2.info['id']

    # Bigger than the maximum channel size (2^24 - 1).
    with pytest.raises(RpcError):
        l1.rpc.call('fundchannel_start', {'id': peer, 'amount': 2**24, 'asset': asset})
    with pytest.raises(RpcError):
        l1.rpc.call('fundchannel_start', {'id': peer, 'amount': 0, 'asset': asset})
    # Not an asset id (16 bytes, not 32).
    with pytest.raises(RpcError):
        l1.rpc.call('fundchannel_start', {'id': peer, 'amount': 100_000, 'asset': 'ab' * 16})
    # None of that left a channel behind.
    assert l1.rpc.listpeerchannels()['channels'] == []


@needs_elements
def test_liquid_asset_payment_limits(node_factory, bitcoind):
    """Can't pay more than the channel allows, and a payment that fails
    leaves the balances alone.  (One payment is capped at 2^32 msat, so use
    two.)  The opener's spendable amount is below its balance: the channel
    reserve, and the HTLC fee, which asset channels still count against the
    balance (in asset units) although the commitment pays it from the L-BTC
    fee reserve."""
    l1, l2, lbtc, asset = open_asset_channel(node_factory, bitcoind, [{}, {}])
    big = 4_000_000_000       # msat: 4,000,000 of the 5,000,000 units
    inv = invoice_units(l2, 'big', asset, big)
    send_asset(l1, l2, big, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    ch = only_one(l1.rpc.listpeerchannels()['channels'])
    assert ch['to_us_msat'] == (CHANNEL_UNITS * 1000) - big
    spendable = ch['spendable_msat']
    assert 0 < spendable < ch['to_us_msat']

    # A bit more than we can spend doesn't go through...
    over = spendable + 1_000_000
    inv = invoice_units(l2, 'too-much', asset, over)
    with pytest.raises(RpcError):
        send_asset(l1, l2, over, inv['payment_hash'], inv['payment_secret'])
        l1.rpc.waitsendpay(inv['payment_hash'])
    settled(l1, l2)
    ch2 = only_one(l1.rpc.listpeerchannels()['channels'])
    assert ch2['to_us_msat'] == ch['to_us_msat']
    assert ch2['htlcs'] == []

    # ... and what we can spend does.
    inv = invoice_units(l2, 'ok', asset, spendable)
    send_asset(l1, l2, spendable, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
