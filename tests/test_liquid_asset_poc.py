"""PoC: Lightning channels denominated in a Liquid issued asset.

See common/liquid_asset.h.  `fundchannel_start ... asset=<id> fee_reserve=<sat>`
opens an asset channel: its funding tx pays the asset to the 2-of-2 at output n
and an L-BTC fee reserve to the same 2-of-2 at output n+1, and channel
transactions take on-chain fees from that reserve.  Ordinary L-BTC channels keep
working alongside.  The asset funding tx is built by elementsd's wallet (CLN's
wallet doesn't know about assets yet).  Covered: open, pay across a hop,
cooperative close, and unilateral close without HTLCs in flight.

    TEST_NETWORK=liquid-regtest pytest tests/test_liquid_asset_poc.py
"""
from fixtures import *  # noqa: F401,F403
from decimal import Decimal
from pyln.client import RpcError
from utils import only_one, sync_blockheight, wait_for, TEST_NETWORK

import pytest
import struct


FEE_RESERVE_SAT = 100_000
CHANNEL_UNITS = 5_000_000   # asset base units in each asset channel
LBTC_CHANNEL_SAT = 5_000_000
PAY_MSAT = 1_000_000_000    # 1,000,000 units (asset) or sats (L-BTC)


def varint(n):
    if n < 0xfd:
        return bytes([n])
    return b'\xfd' + struct.pack('<H', n)


def explicit_tx(ins, outs):
    """Unsigned Elements tx with explicit (unblinded) outputs.
    ins: [(txid, vout)], outs: [(asset_hex, amount, scriptpubkey_bytes)]"""
    b = struct.pack('<i', 2) + b'\x00' + varint(len(ins))
    for txid, n in ins:
        b += bytes.fromhex(txid)[::-1] + struct.pack('<I', n) + b'\x00' + struct.pack('<I', 0xfffffffd)
    b += varint(len(outs))
    for asset, amount, spk in outs:
        b += (b'\x01' + bytes.fromhex(asset)[::-1]
              + b'\x01' + struct.pack('>Q', amount)
              + b'\x00' + varint(len(spk)) + spk)
    return (b + struct.pack('<I', 0)).hex()


def unconfidential(bitcoind):
    info = bitcoind.rpc.getaddressinfo(bitcoind.rpc.getnewaddress())
    return info['unconfidential'], bytes.fromhex(info['scriptPubKey'])


def explicit_utxo(bitcoind, asset, amount):
    """Have elementsd's wallet pay itself an explicit output in `asset`."""
    addr, spk = unconfidential(bitcoind)
    txid = bitcoind.rpc.sendtoaddress(addr, str(amount), "", "", False, False,
                                      None, None, None, asset)
    # No -txindex: look it up while it's still in the mempool.
    vouts = bitcoind.rpc.getrawtransaction(txid, True)['vout']
    bitcoind.generate_block(1, wait_for_mempool=txid)
    for v in vouts:
        if v['scriptPubKey']['hex'] == spk.hex() and 'value' in v:
            return txid, v['n'], int(Decimal(str(v['value'])) * 10**8)
    raise ValueError("no explicit output found")


def fund_asset_channel(bitcoind, opener, peer, asset, lbtc, announce=False,
                       close_to=None):
    # (pyln-client's typed wrapper doesn't know the PoC parameters yet)
    params = {'id': peer.info['id'], 'amount': CHANNEL_UNITS,
              'announce': announce, 'asset': asset,
              'fee_reserve': FEE_RESERVE_SAT}
    if close_to:
        params['close_to'] = close_to
    start = opener.rpc.call('fundchannel_start', params)
    funding_spk = bytes.fromhex(start['scriptpubkey'])

    x_txid, x_n, x_amount = explicit_utxo(bitcoind, asset, 1)
    l_txid, l_n, l_amount = explicit_utxo(bitcoind, lbtc, Decimal('0.01'))
    fee = 1000
    raw = explicit_tx(
        [(x_txid, x_n), (l_txid, l_n)],
        [(asset, CHANNEL_UNITS, funding_spk),           # channel capacity
         (lbtc, FEE_RESERVE_SAT, funding_spk),          # fee reserve, same 2-of-2
         (asset, x_amount - CHANNEL_UNITS, unconfidential(bitcoind)[1]),
         (lbtc, l_amount - FEE_RESERVE_SAT - fee, unconfidential(bitcoind)[1]),
         (lbtc, fee, b'')])
    pset = bitcoind.rpc.utxoupdatepsbt(bitcoind.rpc.converttopsbt(raw))
    # Not withheld: elementsd broadcasts it, and a withheld channel is
    # simply forgotten on close (pyln-client's default is withhold=True).
    opener.rpc.fundchannel_complete(peer.info['id'], pset, withhold=False)

    signed = bitcoind.rpc.signrawtransactionwithwallet(raw)
    assert signed['complete']
    return bitcoind.rpc.sendrawtransaction(signed['hex'])


def outputs_by_asset(bitcoind, txid, blockhash=None):
    tx = (bitcoind.rpc.getrawtransaction(txid, True, blockhash) if blockhash
          else bitcoind.rpc.getrawtransaction(txid, True))
    units = {}
    for v in tx['vout']:
        units.setdefault(v['asset'], []).append(int(Decimal(str(v['value'])) * 10**8))
    return tx, units


def channel_by_funding(node, txid):
    return only_one([c for c in node.rpc.listpeerchannels()['channels']
                     if c['funding_txid'] == txid])


def pay_via(l1, l2, l3, funding12, funding23, inv):
    """Pay inv from l1 to l3, forcing the l1->l2 and l2->l3 channels."""
    ch12 = channel_by_funding(l1, funding12)
    ch23 = channel_by_funding(l2, funding23)
    fee = ch23['fee_base_msat'] + PAY_MSAT * ch23['fee_proportional_millionths'] // 10**6 + 1000
    final = l3.rpc.decode(inv['bolt11'])['min_final_cltv_expiry'] + 1
    # Private channels: l2 only forwards via its own alias for l2->l3.
    route = [{'id': l2.info['id'], 'channel': ch12['short_channel_id'],
              'amount_msat': PAY_MSAT + fee,
              'delay': final + ch23['updates']['local']['cltv_expiry_delta']},
             {'id': l3.info['id'], 'channel': ch23['alias']['local'],
              'amount_msat': PAY_MSAT, 'delay': final}]
    l1.rpc.sendpay(route, inv['payment_hash'],
                   payment_secret=inv['payment_secret'],
                   amount_msat=PAY_MSAT)
    return l1.rpc.waitsendpay(inv['payment_hash'])


def asset_invoice(node, label, asset):
    return node.rpc.call('invoice', {'amount_msat': PAY_MSAT, 'label': label,
                                     'description': label, 'asset': asset})


def settled(*nodes):
    for n in nodes:
        wait_for(lambda: all(c['htlcs'] == []
                             for c in n.rpc.listpeerchannels()['channels']))


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
def test_liquid_asset_channel_poc(node_factory, bitcoind):
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2, l3 = node_factory.get_nodes(3)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)

    # Each hop gets an ordinary L-BTC channel *and* an asset channel.
    lbtc_funding, asset_funding = {}, {}
    for opener, peer in ((l1, l2), (l2, l3)):
        opener.fundwallet(10**7)
        lbtc_funding[opener] = opener.rpc.fundchannel(peer.info['id'],
                                                      LBTC_CHANNEL_SAT,
                                                      announce=False)['txid']
        txid = fund_asset_channel(bitcoind, opener, peer, asset, lbtc)
        asset_funding[opener] = txid
        tx, _ = outputs_by_asset(bitcoind, txid)
        # Asset output and L-BTC fee reserve, both to the channel's 2-of-2.
        assert tx['vout'][0]['asset'] == asset
        assert tx['vout'][1]['asset'] == lbtc
        assert tx['vout'][1]['scriptPubKey']['hex'] == tx['vout'][0]['scriptPubKey']['hex']

    bitcoind.generate_block(6)
    for n in (l1, l2, l3):
        wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL'
                             for c in n.rpc.listpeerchannels()['channels']))
    funding = {'lbtc12': lbtc_funding[l1], 'lbtc23': lbtc_funding[l2],
               'asset12': asset_funding[l1], 'asset23': asset_funding[l2]}
    # listpeerchannels says which channels are asset channels.
    ch = channel_by_funding(l1, funding['asset12'])
    assert ch['liquid_asset'] == asset
    assert ch['liquid_fee_reserve_msat'] == FEE_RESERVE_SAT * 1000
    assert 'liquid_asset' not in channel_by_funding(l1, funding['lbtc12'])

    # 1. Asset invoice, paid over the asset channels.
    inv = asset_invoice(l3, 'asset-ok', asset)
    assert pay_via(l1, l2, l3, funding['asset12'], funding['asset23'], inv)['status'] == 'complete'

    # 2. Ordinary invoice, paid over the L-BTC channels.
    inv = l3.rpc.invoice(PAY_MSAT, 'lbtc-ok', 'in L-BTC')
    assert pay_via(l1, l2, l3, funding['lbtc12'], funding['lbtc23'], inv)['status'] == 'complete'

    # 3. Asset invoice, paid in L-BTC: the receiver refuses.
    inv = asset_invoice(l3, 'asset-wrong', asset)
    with pytest.raises(RpcError, match=r'WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS'):
        pay_via(l1, l2, l3, funding['lbtc12'], funding['lbtc23'], inv)
    l3.daemon.wait_for_log('arrived in the wrong Liquid asset')

    # 4. In on the asset channel, out on the L-BTC channel: l2 refuses to
    #    swap them 1:1.
    inv = l3.rpc.invoice(PAY_MSAT, 'cross', 'in L-BTC')
    with pytest.raises(RpcError, match=r'WIRE_UNKNOWN_NEXT_PEER'):
        pay_via(l1, l2, l3, funding['asset12'], funding['lbtc23'], inv)
    l2.daemon.wait_for_log('Refusing to forward between different Liquid assets')

    settled(l1, l2, l3)
    # Only the two good payments moved funds: one on each kind of channel.
    for name, peer in (('asset12', l2), ('asset23', l3), ('lbtc12', l2), ('lbtc23', l3)):
        ch = channel_by_funding(peer, funding[name])
        assert ch['to_us_msat'] >= PAY_MSAT
        assert ch['to_us_msat'] < 2 * PAY_MSAT

    # Cooperative closes.  Asset channels spend both outputs and pay the fee
    # from the L-BTC reserve.
    for opener, peer, name in ((l1, l2, 'asset12'), (l2, l3, 'asset23'),
                               (l1, l2, 'lbtc12'), (l2, l3, 'lbtc23')):
        peer_balance = channel_by_funding(peer, funding[name])['to_us_msat'] // 1000
        res = opener.rpc.close(channel_by_funding(opener, funding[name])['channel_id'])
        assert res['type'] == 'mutual'
        wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
        txid = only_one(bitcoind.rpc.getrawmempool())
        blockhash = bitcoind.generate_block(1, wait_for_mempool=txid)[0]
        tx, units = outputs_by_asset(bitcoind, txid, blockhash)

        if name.startswith('asset'):
            assert len(tx['vin']) == 2
            assert sum(units[asset]) == CHANNEL_UNITS
            assert peer_balance in units[asset]
            assert sum(units[lbtc]) == FEE_RESERVE_SAT
        else:
            assert len(tx['vin']) == 1
            assert asset not in units
            assert peer_balance in units[lbtc]

    # Nobody crashed processing the closes.
    bitcoind.generate_block(1)
    for n in (l1, l2, l3):
        n.rpc.getinfo()


def mempool_spend_of(bitcoind, txid):
    """The mempool tx spending an output of txid, if there is one."""
    for t in bitcoind.rpc.getrawmempool():
        tx = bitcoind.rpc.getrawtransaction(t, True)
        if any(vin.get('txid') == txid for vin in tx['vin']):
            return tx
    return None


def mine_spend_of(bitcoind, txid):
    """Wait for a tx spending an output of txid, mine it, return it."""
    wait_for(lambda: mempool_spend_of(bitcoind, txid) is not None)
    tx = mempool_spend_of(bitcoind, txid)
    bitcoind.generate_block(1, wait_for_mempool=tx['txid'])
    return tx


def units(vout):
    return int(Decimal(str(vout['value'])) * 10**8)


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
@pytest.mark.parametrize("closer", ["opener", "peer"])
def test_liquid_asset_unilateral_close(node_factory, bitcoind, closer):
    """Force-close an asset channel (no HTLCs in flight).  The commitment
    spends both funding outputs, the asset and the L-BTC fee reserve; then
    each side sweeps its asset output.  Fees are L-BTC: the opener pays them
    from its fee reserve change (same script as its asset output, so it's
    swept along), the peer from its wallet.  Assets go where a mutual close
    would pay them (CLN's wallet can't hold them): `close_to`/`destination`
    if given, else the channel's default close address."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    # Only l2 has L-BTC in its wallet: l1's funding comes from elementsd.
    l2.fundwallet(10**6)
    assert l1.rpc.listfunds()['outputs'] == []

    # The opener's asset goes to elementsd (close_to, given at open).
    opener_addr, opener_spk = unconfidential(bitcoind)
    funding = fund_asset_channel(bitcoind, l1, l2, asset, lbtc,
                                 close_to=opener_addr)
    bitcoind.generate_block(6, wait_for_mempool=funding)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')

    # Give l2 a balance, then wait until no HTLC is in flight.
    inv = asset_invoice(l2, 'balance', asset)
    ch = channel_by_funding(l1, funding)
    route = [{'id': l2.info['id'], 'channel': ch['short_channel_id'],
              'amount_msat': PAY_MSAT,
              'delay': l2.rpc.decode(inv['bolt11'])['min_final_cltv_expiry'] + 1}]
    l1.rpc.sendpay(route, inv['payment_hash'],
                   payment_secret=inv['payment_secret'], amount_msat=PAY_MSAT)
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    peer_units = PAY_MSAT // 1000
    opener_units = CHANNEL_UNITS - peer_units
    peer_wallet = sum(o['amount_msat'] for o in l2.rpc.listfunds()['outputs']) // 1000

    # The closer's peer is offline, so `close` goes unilateral.  If the peer
    # (not the opener) closes, its asset goes to elementsd too.
    if closer == 'opener':
        closing, other = l1, l2
        peer_addr, peer_spk = None, None
        close_args = {}
    else:
        closing, other = l2, l1
        peer_addr, peer_spk = unconfidential(bitcoind)
        close_args = {'destination': peer_addr}
    other.stop()
    res = closing.rpc.close(ch['channel_id'], unilateraltimeout=1, **close_args)
    assert res['type'] == 'unilateral'

    # The commitment tx spends both funding outputs, and elementsd takes it.
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit = bitcoind.rpc.getrawtransaction(commit_txid, True)
    assert sorted((vin['txid'], vin['vout']) for vin in commit['vin']) == [(funding, 0), (funding, 1)]
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)
    _, commit_units = outputs_by_asset(bitcoind, commit_txid,
                                       bitcoind.rpc.getbestblockhash())
    assert sorted(commit_units[asset]) == sorted([opener_units, peer_units])
    # Fee and L-BTC change come out of the fee reserve.
    assert sum(commit_units[lbtc]) == FEE_RESERVE_SAT

    # The other side comes back and sweeps its to_remote right away.
    other.start()
    remote_sweep = mine_spend_of(bitcoind, commit_txid)

    # The closer sweeps its to_local once the CSV delay is over.
    bitcoind.generate_block(4)
    local_sweep = mine_spend_of(bitcoind, commit_txid)

    sweeps = {closing: local_sweep, other: remote_sweep}
    for node, sweep in sweeps.items():
        assert all(vin['txid'] != commit_txid or vin['sequence'] in (5, 0xfffffffd)
                   for vin in sweep['vin'])
        is_opener = node == l1
        want_units = opener_units if is_opener else peer_units
        want_spk = opener_spk if is_opener else peer_spk
        asset_outs = [v for v in sweep['vout'] if v['asset'] == asset]
        assert [units(v) for v in asset_outs] == [want_units]
        if want_spk is not None:
            assert asset_outs[0]['scriptPubKey']['hex'] == want_spk.hex()
        commit_inputs = [vin['vout'] for vin in sweep['vin'] if vin['txid'] == commit_txid]
        if is_opener:
            # Asset output and the L-BTC change from the fee reserve:
            # that pays the fee, nothing comes from the wallet.
            assert len(sweep['vin']) == 2 and len(commit_inputs) == 2
            assert {commit['vout'][i]['asset'] for i in commit_inputs} == {asset, lbtc}
        else:
            # Asset output, plus L-BTC from the wallet for the fee.
            assert commit_inputs == [i for i in commit_inputs
                                     if commit['vout'][i]['asset'] == asset]
            assert len(commit_inputs) == 1 and len(sweep['vin']) >= 2

    # The L-BTC ends up in each side's wallet: the opener's fee reserve
    # change (minus fees), and what the peer didn't spend on its sweep.
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    opener_lbtc = [o for o in l1.rpc.listfunds()['outputs']
                   if o['txid'] == sweeps[l1]['txid']]
    reserve_change = sum(commit_units[lbtc]) - int(Decimal(str(commit['fee'][lbtc])) * 10**8)
    assert only_one(opener_lbtc)['amount_msat'] // 1000 < reserve_change
    assert only_one(opener_lbtc)['amount_msat'] // 1000 > reserve_change - 5000
    peer_after = sum(o['amount_msat'] for o in l2.rpc.listfunds()['outputs']) // 1000
    assert peer_wallet - 5000 < peer_after < peer_wallet

    # Everything is resolved; nobody logged anything broken.
    bitcoind.generate_block(100)
    for n in (l1, l2):
        n.daemon.wait_for_log('onchaind complete, forgetting peer')
