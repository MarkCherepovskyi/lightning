"""PoC: Lightning channels denominated in a Liquid issued asset.

See common/liquid_asset.h.  `fundchannel_start ... asset=<id> fee_reserve=<sat>`
opens an asset channel: its funding tx pays the asset to the 2-of-2 at output n
and an L-BTC fee reserve to the same 2-of-2 at output n+1, and channel
transactions take on-chain fees from that reserve.  Ordinary L-BTC channels keep
working alongside.  The asset funding tx is built by elementsd's wallet (CLN's
wallet doesn't know about assets yet).  Covered: open, pay across a hop,
cooperative close, unilateral close with and without HTLCs in flight, and
penalties for a revoked commitment.

    TEST_NETWORK=liquid-regtest pytest tests/test_liquid_asset_poc.py
"""
from fixtures import *  # noqa: F401,F403
from decimal import Decimal
from pyln.client import RpcError
from utils import only_one, sync_blockheight, wait_for, TEST_NETWORK, TIMEOUT

import pytest
import struct
import threading


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


def chain_txs(bitcoind, since):
    """Every tx mined from block `since` on, decoded, by txid."""
    txs = {}
    for h in range(since, bitcoind.rpc.getblockcount() + 1):
        block = bitcoind.rpc.getblock(bitcoind.rpc.getblockhash(h), 2)
        for tx in block['tx']:
            txs[tx['txid']] = tx
    return txs


def spend_of(txs, txid, vout):
    """The (tx, input index) in txs spending txid:vout."""
    return only_one([(tx, i) for tx in txs.values()
                     for i, vin in enumerate(tx['vin'])
                     if vin.get('txid') == txid and vin['vout'] == vout])


def unspent_asset(txs, asset):
    """Units of asset in outputs of txs that no tx in txs spends, by script."""
    spent = {(vin['txid'], vin['vout'])
             for tx in txs.values() for vin in tx['vin'] if 'txid' in vin}
    res = {}
    for tx in txs.values():
        for v in tx['vout']:
            if v.get('asset') == asset and (tx['txid'], v['n']) not in spent:
                spk = v['scriptPubKey']['hex']
                res[spk] = res.get(spk, 0) + units(v)
    return res


def lbtc_change(commit, lbtc):
    """The opener's L-BTC change from the fee reserve, in a commitment."""
    return sum(units(v) for v in commit['vout']
               if v['asset'] == lbtc and v['scriptPubKey']['type'] != 'fee')


def lbtc_fees(txs, lbtc, but=()):
    """L-BTC fees paid by txs (but those in `but`)."""
    return sum(int(Decimal(str(tx['fee'][lbtc])) * 10**8)
               for txid, tx in txs.items()
               if txid not in but and 'coinbase' not in tx['vin'][0])


def wallet_sats(node):
    return sum(o['amount_msat'] for o in node.rpc.listfunds()['outputs']) // 1000


def mine_until_forgotten(bitcoind, nodes, maxblocks=200):
    """Mine block by block, letting the nodes react to each, until the
    nodes' onchainds are done."""
    for _ in range(maxblocks):
        if all(n.daemon.is_in_log('onchaind complete, forgetting peer')
               for n in nodes):
            return
        sync_blockheight(bitcoind, nodes)
        bitcoind.generate_block(1)
    raise ValueError("onchaind not done after {} blocks".format(maxblocks))


def open_asset_channel(node_factory, bitcoind, opts):
    """l1 opens an asset channel to l2 (when it closes, l1's asset goes to
    elementsd).  Both get two L-BTC outputs in their wallets, for fees."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts=opts)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    for n in (l1, l2):
        n.fundwallet(10**6)
        n.fundwallet(10**6)

    opener_addr, _ = unconfidential(bitcoind)
    funding = fund_asset_channel(bitcoind, l1, l2, asset, lbtc,
                                 close_to=opener_addr)
    bitcoind.generate_block(6, wait_for_mempool=funding)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    return l1, l2, lbtc, asset


def send_asset(l1, l2, amount_msat, payment_hash, payment_secret):
    ch = only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])
    route = [{'id': l2.info['id'], 'channel': ch['short_channel_id'],
              'amount_msat': amount_msat, 'delay': 19}]
    l1.rpc.sendpay(route, payment_hash, payment_secret=payment_secret,
                   amount_msat=amount_msat)


def invoice_units(node, label, asset, amount_msat):
    return node.rpc.call('invoice', {'amount_msat': amount_msat, 'label': label,
                                     'description': label, 'asset': asset})


def close_to(node):
    """Where the node's sweeps send its asset."""
    return only_one(node.rpc.listpeerchannels()['channels'])['close_to']


def is_single_acp_sig(item):
    """Is this witness item a SIGHASH_SINGLE|SIGHASH_ANYONECANPAY sig?"""
    return len(item) > 2 * 64 and item.endswith('83')


HTLC_MSAT = 1_500_000_000


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
@pytest.mark.parametrize("closer", ["payer", "payee"])
@pytest.mark.parametrize("outcome", ["timeout", "success"])
def test_liquid_asset_htlc_onchain(node_factory, bitcoind, outcome, closer):
    """Force-close an asset channel with an HTLC in flight from l1 (the
    opener) to l2.  HTLC outputs are in the asset, so HTLC txs pay no fee
    and are signed SIGHASH_SINGLE|SIGHASH_ANYONECANPAY: whoever broadcasts
    one adds L-BTC from its wallet.  Spending an HTLC output of the peer's
    commitment directly is a sweep paid with wallet L-BTC.  Depending on
    who closes and whether l2 knows the preimage, the HTLC is resolved by:

    - timeout, payer closes: l1's HTLC-timeout tx, then its delayed output;
    - timeout, payee closes: l1 takes the timed-out HTLC directly;
    - success, payee closes: l2's HTLC-success tx, then its delayed output;
    - success, payer closes: l2 takes it directly, with the preimage.

    l1 learns the outcome on-chain, so the payment fails or succeeds."""
    # l2 hangs up instead of failing / fulfilling the HTLC (the second
    # one: the first gives l2 a balance), so it stays in flight.
    disconnect = (['-WIRE_UPDATE_FAIL_HTLC'] if outcome == 'timeout'
                  else ['-WIRE_UPDATE_FULFILL_HTLC*2'])
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind, [{}, {'disconnect': disconnect}])

    inv = invoice_units(l2, 'balance', asset, PAY_MSAT)
    send_asset(l1, l2, PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    spk = {l1: close_to(l1), l2: close_to(l2)}
    lbtc_before = {n: wallet_sats(n) for n in (l1, l2)}

    if outcome == 'timeout':
        # l2 doesn't know this payment.
        payment_hash = payment_secret = '01' * 32
    else:
        inv = invoice_units(l2, 'in-flight', asset, HTLC_MSAT)
        payment_hash, payment_secret = inv['payment_hash'], inv['payment_secret']
    send_asset(l1, l2, HTLC_MSAT, payment_hash, payment_secret)
    result = {}

    def wait_pay():
        try:
            result['ok'] = l1.rpc.waitsendpay(payment_hash, TIMEOUT * 2)
        except RpcError as e:
            result['err'] = e
    t = threading.Thread(target=wait_pay, daemon=True)
    t.start()

    l2.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_')
    for n in (l1, l2):
        wait_for(lambda: not only_one(n.rpc.listpeerchannels()['channels'])['peer_connected'])
    assert len(only_one(l1.rpc.listpeerchannels()['channels'])['htlcs']) == 1

    closing, other = (l1, l2) if closer == 'payer' else (l2, l1)
    assert closing.rpc.close(other.info['id'], unilateraltimeout=1)['type'] == 'unilateral'
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)

    mine_until_forgotten(bitcoind, [l1, l2])
    t.join(TIMEOUT)

    # The payment's outcome came from the chain.
    if outcome == 'timeout':
        assert 'timed out' in result['err'].error['message']
    else:
        assert result['ok']['status'] == 'complete'
        assert only_one(l2.rpc.listinvoices('in-flight')['invoices'])['status'] == 'paid'

    txs = chain_txs(bitcoind, commit_height)
    commit = txs[commit_txid]
    htlc_n = only_one([v['n'] for v in commit['vout'] if v.get('asset') == asset
                       and units(v) == HTLC_MSAT // 1000])
    spend, innum = spend_of(txs, commit_txid, htlc_n)
    witness = spend['vin'][innum]['txinwitness']
    winner = l2 if outcome == 'success' else l1
    if (outcome == 'timeout') == (closer == 'payer'):
        # The HTLC tx (pre-signed by the peer): `0 <remotehtlcsig>
        # <localhtlcsig> <preimage or empty> <wscript>`.
        assert len(witness) == 5
        assert is_single_acp_sig(witness[1]) and is_single_acp_sig(witness[2])
        assert len(witness[3]) == (64 if outcome == 'success' else 0)
        # Input 0 and output 0 are the HTLC.  The rest is L-BTC: from the
        # broadcaster's wallet, its change, and the fee.
        assert innum == 0
        assert spend['vout'][0]['asset'] == asset
        assert units(spend['vout'][0]) == HTLC_MSAT // 1000
        assert len(spend['vin']) >= 2
        assert all(v['asset'] == lbtc for v in spend['vout'][1:])
        # After the delay, its output goes to the winner's close address.
        sweep, _ = spend_of(txs, spend['txid'], 0)
        assert [v['scriptPubKey']['hex'] for v in sweep['vout']
                if v.get('asset') == asset] == [spk[winner]]
    else:
        # Taken directly from the peer's commitment: `<sig> <preimage or
        # empty> <wscript>`, with L-BTC from the wallet.
        assert len(witness) == 3
        assert len(witness[1]) == (64 if outcome == 'success' else 0)
        assert [v['scriptPubKey']['hex'] for v in spend['vout']
                if v.get('asset') == asset] == [spk[winner]]
        assert len(spend['vin']) >= 2

    # Each side got its asset: l2 its balance, plus the HTLC if it
    # succeeded, and l1 the rest.
    l2_units = (PAY_MSAT + (HTLC_MSAT if outcome == 'success' else 0)) // 1000
    assert unspent_asset(txs, asset) == {spk[l1]: CHANNEL_UNITS - l2_units,
                                         spk[l2]: l2_units}

    # L-BTC only paid fees: both wallets lost what the txs after the
    # commitment paid, and l1 got its fee reserve change back.
    change = lbtc_change(commit, lbtc)
    lbtc_after = {n: wallet_sats(n) for n in (l1, l2)}
    assert (sum(lbtc_after.values())
            == sum(lbtc_before.values()) + change - lbtc_fees(txs, lbtc, [commit_txid]))
    assert lbtc_before[l1] < lbtc_after[l1] < lbtc_before[l1] + change
    assert lbtc_after[l2] < lbtc_before[l2]


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
def test_liquid_asset_penalty(node_factory, bitcoind):
    """l1 (the opener) broadcasts a revoked commitment of an asset channel,
    with an HTLC to l2 in it.  l2 takes everything with the revocation key:
    l1's to_local together with l1's L-BTC fee reserve change (same script,
    and it pays that penalty's fee), and the HTLC (with L-BTC from l2's
    wallet for the fee).  All of the asset goes to l2's close address."""
    # Commitments stop after the first: the HTLC gets added, not fulfilled.
    # Feerates identical so we don't get gratuitous commits to update them.
    opts = {'dev-disable-commit-after': 1,
            'feerates': (7500, 7500, 7500, 7500)}
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind,
        [{**opts, 'may_fail': True,
          # l1 doesn't know its own revoked commitment.
          'broken_log': r"onchaind-chan#[0-9]*: Could not find resolution for output .*: did \*we\* cheat\?"},
         opts])

    inv = invoice_units(l2, 'cheat', asset, HTLC_MSAT)
    send_asset(l1, l2, HTLC_MSAT, inv['payment_hash'], inv['payment_secret'])
    l1.daemon.wait_for_log('dev-disable-commit-after: disabling')
    l2.daemon.wait_for_log('dev-disable-commit-after: disabling')
    # l1 got l2's commitment_signed with the HTLC: keep that commitment.
    l1.daemon.wait_for_log('got commitsig')
    cheat = l1.rpc.dev_sign_last_tx(l2.info['id'])['tx']

    l1.rpc.dev_reenable_commit(l2.info['id'])
    l2.rpc.dev_reenable_commit(l1.info['id'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    spk2 = close_to(l2)
    lbtc_before = wallet_sats(l2)

    commit_txid = bitcoind.rpc.sendrawtransaction(cheat)
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)
    l2.daemon.wait_for_logs(['Telling lightningd about OUR_PENALTY_TX to resolve'
                             ' THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM',
                             'Telling lightningd about OUR_PENALTY_TX to resolve'
                             ' THEIR_REVOKED_UNILATERAL/THEIR_HTLC'])
    mine_until_forgotten(bitcoind, [l2])

    txs = chain_txs(bitcoind, commit_height)
    commit = txs[commit_txid]
    # l1's to_local, its L-BTC change and the HTLC: all of them penalized.
    outs = [v for v in commit['vout'] if v['scriptPubKey']['type'] != 'fee']
    assert sorted(v['asset'] for v in outs) == sorted([asset, asset, lbtc])
    penalties = {spend_of(txs, commit_txid, v['n'])[0]['txid'] for v in outs}
    assert len(penalties) == 2
    for txid in penalties:
        p = txs[txid]
        from_commit = [vin for vin in p['vin'] if vin.get('txid') == commit_txid]
        # `<revocation_sig> <1 or revocationpubkey> <wscript>`
        assert all(len(vin['txinwitness']) == 3 for vin in from_commit)
        if len(from_commit) == 2:
            # to_local and the change, which pays the fee.
            assert {commit['vout'][vin['vout']]['asset'] for vin in from_commit} == {asset, lbtc}
            assert len(p['vin']) == 2
        else:
            # The HTLC, and L-BTC from l2's wallet.
            assert commit['vout'][from_commit[0]['vout']]['asset'] == asset
            assert len(p['vin']) >= 2

    assert unspent_asset(txs, asset) == {spk2: CHANNEL_UNITS}
    # l2 also keeps what the penalties' fees left of l1's L-BTC change.
    assert (wallet_sats(l2) == lbtc_before + lbtc_change(commit, lbtc)
            - lbtc_fees(txs, lbtc, [t for t in txs if t not in penalties]))


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
@pytest.mark.parametrize("outcome", ["timeout", "success"])
def test_liquid_asset_htlc_onchain_upstream(node_factory, bitcoind, outcome):
    """l1 pays l3 through l2 over asset channels, and l2-l3 goes on-chain
    with the HTLC in flight.  l2 resolves the incoming HTLC from l1 off-chain
    as the outgoing one resolves on-chain: it fails it once its HTLC-timeout
    tx is deep enough, or fulfills it with the preimage from l3's
    HTLC-success tx."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    # l3 hangs up instead of failing / fulfilling the HTLC.
    disconnect = (['-WIRE_UPDATE_FAIL_HTLC'] if outcome == 'timeout'
                  else ['-WIRE_UPDATE_FULFILL_HTLC'])
    l1, l2, l3 = node_factory.get_nodes(3, opts=[{}, {}, {'disconnect': disconnect}])
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)
    for n in (l2, l3):
        n.fundwallet(10**6)
        n.fundwallet(10**6)
    # (fund_asset_channel mines blocks, so mine each funding tx first)
    funding12 = fund_asset_channel(bitcoind, l1, l2, asset, lbtc)
    bitcoind.generate_block(1, wait_for_mempool=funding12)
    funding23 = fund_asset_channel(bitcoind, l2, l3, asset, lbtc)
    bitcoind.generate_block(6, wait_for_mempool=funding23)
    for n in (l1, l2, l3):
        wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL'
                             for c in n.rpc.listpeerchannels()['channels']))

    if outcome == 'timeout':
        # l3 doesn't know this payment.
        payment_hash = payment_secret = '02' * 32
    else:
        inv = invoice_units(l3, 'upstream', asset, PAY_MSAT)
        payment_hash, payment_secret = inv['payment_hash'], inv['payment_secret']
    ch12 = channel_by_funding(l1, funding12)
    ch23 = channel_by_funding(l2, funding23)
    fee = ch23['fee_base_msat'] + PAY_MSAT * ch23['fee_proportional_millionths'] // 10**6
    route = [{'id': l2.info['id'], 'channel': ch12['short_channel_id'],
              'amount_msat': PAY_MSAT + fee,
              'delay': 19 + ch23['updates']['local']['cltv_expiry_delta']},
             {'id': l3.info['id'], 'channel': ch23['alias']['local'],
              'amount_msat': PAY_MSAT, 'delay': 19}]
    l1.rpc.sendpay(route, payment_hash, payment_secret=payment_secret,
                   amount_msat=PAY_MSAT)
    result = {}

    def wait_pay():
        try:
            result['ok'] = l1.rpc.waitsendpay(payment_hash, TIMEOUT * 2)
        except RpcError as e:
            result['err'] = e
    t = threading.Thread(target=wait_pay, daemon=True)
    t.start()

    l3.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_')
    wait_for(lambda: not only_one(l3.rpc.listpeerchannels()['channels'])['peer_connected'])
    spk = {l2: channel_by_funding(l2, funding23)['close_to'],
           l3: channel_by_funding(l3, funding23)['close_to']}

    # Timeout: l2 goes on-chain, and times the HTLC out with its HTLC-timeout
    # tx.  Success: l3 does, and takes it with its HTLC-success tx.
    closing, other = (l2, l3) if outcome == 'timeout' else (l3, l2)
    assert closing.rpc.close(ch23['channel_id'], unilateraltimeout=1)['type'] == 'unilateral'
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)
    mine_until_forgotten(bitcoind, [l2, l3])
    t.join(TIMEOUT)

    # l2 failed or fulfilled l1's HTLC: l1-l2 is still open, and settled.
    if outcome == 'timeout':
        assert result['err'].error['data']['failcodename'] == 'WIRE_PERMANENT_CHANNEL_FAILURE'
        assert result['err'].error['data']['erring_node'] == l2.info['id']
    else:
        assert result['ok']['status'] == 'complete'
    settled(l1, l2)
    ch12 = channel_by_funding(l1, funding12)
    assert ch12['state'] == 'CHANNELD_NORMAL'
    paid = PAY_MSAT + fee if outcome == 'success' else 0
    assert ch12['to_us_msat'] == CHANNEL_UNITS * 1000 - paid

    # On-chain: the HTLC tx, and l3 got the HTLC if it succeeded.
    txs = chain_txs(bitcoind, commit_height)
    htlc_n = only_one([v['n'] for v in txs[commit_txid]['vout']
                       if v.get('asset') == asset and units(v) == PAY_MSAT // 1000])
    htlc_tx, innum = spend_of(txs, commit_txid, htlc_n)
    assert innum == 0 and len(htlc_tx['vin'][0]['txinwitness']) == 5
    l3_units = PAY_MSAT // 1000 if outcome == 'success' else 0
    want = {spk[l2]: CHANNEL_UNITS - l3_units}
    if l3_units:
        want[spk[l3]] = l3_units
    assert unspent_asset(txs, asset) == want
