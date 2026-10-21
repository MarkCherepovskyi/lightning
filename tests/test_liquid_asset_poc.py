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

import os
import pytest
import shutil
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
    # Routes' CLTVs count from l1's height: l3 mustn't be ahead of it.
    sync_blockheight(bitcoind, [l1, l2, l3])
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


def open_asset_channel(node_factory, bitcoind, opts, wallet_outputs=(2, 2)):
    """l1 opens an asset channel to l2 (when it closes, l1's asset goes to
    elementsd).  They get wallet_outputs L-BTC outputs in their wallets, for
    fees."""
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)

    l1, l2 = node_factory.get_nodes(2, opts=opts)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    for n, num in zip((l1, l2), wallet_outputs):
        for _ in range(num):
            n.fundwallet(10**6)

    opener_addr, _ = unconfidential(bitcoind)
    funding = fund_asset_channel(bitcoind, l1, l2, asset, lbtc,
                                 close_to=opener_addr)
    bitcoind.generate_block(6, wait_for_mempool=funding)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['state'] == 'CHANNELD_NORMAL')
    return l1, l2, lbtc, asset


def send_asset(l1, l2, amount_msat, payment_hash, payment_secret, delay=19,
               total_msat=None, **kwargs):
    """Send an HTLC from l1 to l2 (a part of total_msat, if given)."""
    ch = only_one(l1.rpc.listpeerchannels(l2.info['id'])['channels'])
    route = [{'id': l2.info['id'], 'channel': ch['short_channel_id'],
              'amount_msat': amount_msat, 'delay': delay}]
    l1.rpc.sendpay(route, payment_hash, payment_secret=payment_secret,
                   amount_msat=total_msat or amount_msat, **kwargs)


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
HOLD_PLUGIN = os.path.join(os.path.dirname(__file__), 'plugins',
                           'htlc_accepted-hold-all.py')


def log_count(node, text):
    """How many lines of the node's log contain text."""
    node.daemon.logs_catchup()
    return sum(text in line for line in node.daemon.logs)


def sendpay_statuses(node, payment_hash):
    return sorted(p['status'] for p in
                  node.rpc.listsendpays(payment_hash=payment_hash)['payments'])


def htlc_spends(txs, commit_txid, asset, units_wanted):
    """(witness, input index, tx) of the spends of the commitment's asset
    outputs worth units_wanted."""
    res = []
    for v in txs[commit_txid]['vout']:
        if v.get('asset') != asset or units(v) != units_wanted:
            continue
        for tx in txs.values():
            for innum, vin in enumerate(tx['vin']):
                if vin.get('txid') == commit_txid and vin['vout'] == v['n']:
                    res.append((vin['txinwitness'], innum, tx))
    return res


def check_htlc_spend(witness, innum, tx, by_closer, fulfilled):
    """An HTLC output of the commitment was spent: by the closer's HTLC tx
    (pre-signed by the peer), or directly by the other side."""
    if by_closer:
        # `0 <remotehtlcsig> <localhtlcsig> <preimage or empty> <wscript>`,
        # at input 0 (with output 0), and the rest is wallet L-BTC.
        assert len(witness) == 5 and innum == 0
        assert is_single_acp_sig(witness[1]) and is_single_acp_sig(witness[2])
        assert len(witness[3]) == (64 if fulfilled else 0)
    else:
        # `<sig> <preimage or empty> <wscript>`
        assert len(witness) == 3
        assert len(witness[1]) == (64 if fulfilled else 0)
    assert len(tx['vin']) >= 2


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
@pytest.mark.parametrize("cheater", ["opener", "peer"])
def test_liquid_asset_penalty(node_factory, bitcoind, cheater):
    """The cheater broadcasts a revoked commitment of an asset channel, with
    an HTLC from it to the victim in it.  The victim takes everything: the
    cheater's to_local and the HTLC with the revocation key, and its own
    to_remote.  The opener's L-BTC fee reserve change has the same script
    as the opener's output, and is spent with it: if the opener cheated,
    in its to_local penalty (and pays that fee), else in the victim's
    to_remote sweep.  The other spends pay their fees from the victim's
    wallet.  All of the asset goes to the victim's close address."""
    # Feerates identical so we don't get gratuitous commits to update them.
    opts = {'feerates': (7500, 7500, 7500, 7500), 'may_reconnect': True}
    cheat_opts = {**opts, 'may_fail': True,
                  # The cheater doesn't know its own revoked commitment.
                  'broken_log': r"onchaind-chan#[0-9]*: Could not find resolution for output .*: did \*we\* cheat\?"}
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind,
        [cheat_opts, opts] if cheater == 'opener' else [opts, cheat_opts])
    bad, victim = (l1, l2) if cheater == 'opener' else (l2, l1)

    # Both get a balance.
    inv = invoice_units(l2, 'balance', asset, 2 * PAY_MSAT)
    send_asset(l1, l2, 2 * PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)

    # From the next commitment on, commitments stop: the cheater's HTLC
    # gets added, not fulfilled.
    for n in (l1, l2):
        n.stop()
        n.daemon.opts['dev-disable-commit-after'] = 1
        n.start()
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    for n in (l1, l2):
        wait_for(lambda: only_one(n.rpc.listpeerchannels()['channels'])['peer_connected'])

    inv = invoice_units(victim, 'cheat', asset, HTLC_MSAT)
    send_asset(bad, victim, HTLC_MSAT, inv['payment_hash'], inv['payment_secret'])
    bad.daemon.wait_for_log('dev-disable-commit-after: disabling')
    victim.daemon.wait_for_log('dev-disable-commit-after: disabling')
    # The cheater got the victim's commitment_signed with the HTLC: keep that
    # commitment.
    bad.daemon.wait_for_log('got commitsig')
    cheat = bad.rpc.dev_sign_last_tx(victim.info['id'])['tx']

    bad.rpc.dev_reenable_commit(victim.info['id'])
    victim.rpc.dev_reenable_commit(bad.info['id'])
    assert bad.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    spk = close_to(victim)
    lbtc_before = wallet_sats(victim)

    commit_txid = bitcoind.rpc.sendrawtransaction(cheat)
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)
    victim.daemon.wait_for_logs(['Telling lightningd about OUR_PENALTY_TX to resolve'
                                 ' THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM',
                                 'Telling lightningd about OUR_PENALTY_TX to resolve'
                                 ' THEIR_REVOKED_UNILATERAL/THEIR_HTLC'])
    mine_until_forgotten(bitcoind, [victim])

    txs = chain_txs(bitcoind, commit_height)
    commit = txs[commit_txid]
    # The cheater's to_local, the HTLC, the victim's to_remote, and the
    # opener's L-BTC change: every one of them went to the victim.
    outs = [v for v in commit['vout'] if v['scriptPubKey']['type'] != 'fee']
    assert sorted(v['asset'] for v in outs) == sorted([asset, asset, asset, lbtc])
    spends = {spend_of(txs, commit_txid, v['n'])[0]['txid'] for v in outs}
    penalties = 0
    for txid in spends:
        tx = txs[txid]
        from_commit = [vin for vin in tx['vin'] if vin.get('txid') == commit_txid]
        assets = sorted(commit['vout'][vin['vout']]['asset'] for vin in from_commit)
        if len(from_commit[0]['txinwitness']) == 3:
            # A penalty: `<revocation_sig> <1 or revocationpubkey> <wscript>`
            penalties += 1
        else:
            # The victim's to_remote (P2WPKH)
            assert len(from_commit[0]['txinwitness']) == 2
            assert asset in assets
        if lbtc in assets:
            # The opener's output and its change: that pays the fee.
            assert assets == sorted([asset, lbtc])
            assert len(tx['vin']) == 2
        else:
            # L-BTC from the victim's wallet.
            assert len(tx['vin']) >= 2
    assert penalties == 2 and len(spends) == 3

    assert unspent_asset(txs, asset) == {spk: CHANNEL_UNITS}
    # The victim also got the opener's L-BTC change, less its txs' fees.
    assert (wallet_sats(victim)
            == lbtc_before + lbtc_change(commit, lbtc) - lbtc_fees(txs, lbtc, [commit_txid]))


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
    sync_blockheight(bitcoind, [l1, l2, l3])

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


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
@pytest.mark.parametrize("closer", ["opener", "peer"])
def test_liquid_asset_multihtlc(node_factory, bitcoind, closer):
    """Force-close an asset channel with HTLCs in flight both ways, which
    both nodes hold (htlc_accepted) until the commitment is mined:

    - l1->l2: two parts of one payment l2 fulfills (same payment hash, amount
      and CLTV, so their outputs are identical), one l2 fails, and one so
      small it's trimmed (no output);
    - l2->l1: one l1 fulfills, and one it fails.

    Each is resolved the way it should be for whose commitment it is (HTLC
    txs of the closer, direct spends of the other side), all of them at the
    same time, with L-BTC from both wallets."""
    l1, l2, lbtc, asset = open_asset_channel(node_factory, bitcoind, [{}, {}],
                                             wallet_outputs=(4, 4))
    # l2 gets a balance, so it can pay l1 too.
    inv = invoice_units(l2, 'balance', asset, 2 * PAY_MSAT)
    send_asset(l1, l2, 2 * PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    spk = {l1: close_to(l1), l2: close_to(l2)}
    lbtc_before = {n: wallet_sats(n) for n in (l1, l2)}

    for n in (l1, l2):
        n.rpc.plugin_start(HOLD_PLUGIN)

    # In asset units; l2 doesn't know B's and D's payment hash, l1 not E's.
    A, B, C, D, E = 600_000, 700_000, 800_000, 100, 900_000
    hash_b, hash_d, hash_e = '03' * 32, '04' * 32, '05' * 32
    inv_a = invoice_units(l2, 'a', asset, 2 * A * 1000)
    for partid in (1, 2):
        send_asset(l1, l2, A * 1000, inv_a['payment_hash'], inv_a['payment_secret'],
                   total_msat=2 * A * 1000, partid=partid, groupid=1)
    send_asset(l1, l2, B * 1000, hash_b, hash_b, delay=25)
    send_asset(l1, l2, D * 1000, hash_d, hash_d, delay=21)
    inv_c = invoice_units(l1, 'c', asset, C * 1000)
    send_asset(l2, l1, C * 1000, inv_c['payment_hash'], inv_c['payment_secret'],
               delay=23)
    send_asset(l2, l1, E * 1000, hash_e, hash_e, delay=27)
    for n, num in ((l1, 2), (l2, 4)):
        wait_for(lambda: log_count(n, 'holding htlc') == num)

    closing, other = (l1, l2) if closer == 'opener' else (l2, l1)
    assert closing.rpc.close(other.info['id'], unilateraltimeout=1)['type'] == 'unilateral'
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)

    # Now they decide: fulfill what they have invoices for, fail the rest.
    for n in (l1, l2):
        n.rpc.releasehtlcs()
    mine_until_forgotten(bitcoind, [l1, l2])

    assert sendpay_statuses(l1, inv_a['payment_hash']) == ['complete', 'complete']
    assert sendpay_statuses(l1, hash_b) == ['failed']
    assert sendpay_statuses(l1, hash_d) == ['failed']
    assert sendpay_statuses(l2, inv_c['payment_hash']) == ['complete']
    assert sendpay_statuses(l2, hash_e) == ['failed']

    txs = chain_txs(bitcoind, commit_height)
    # Who gets each HTLC, and whether it's with the preimage.
    for amount, winner, fulfilled, num in ((A, l2, True, 2), (B, l1, False, 1),
                                           (C, l1, True, 1), (E, l2, False, 1)):
        spends = htlc_spends(txs, commit_txid, asset, amount)
        assert len(spends) == num
        for witness, innum, tx in spends:
            check_htlc_spend(witness, innum, tx, winner == closing, fulfilled)
    # The trimmed HTLC has no output (its asset is in the opener's).
    assert htlc_spends(txs, commit_txid, asset, D) == []

    l1_units = CHANNEL_UNITS - 2 * PAY_MSAT // 1000 - 2 * A + C
    assert unspent_asset(txs, asset) == {spk[l1]: l1_units,
                                         spk[l2]: CHANNEL_UNITS - l1_units}
    commit = txs[commit_txid]
    lbtc_after = {n: wallet_sats(n) for n in (l1, l2)}
    assert (sum(lbtc_after.values()) == sum(lbtc_before.values())
            + lbtc_change(commit, lbtc) - lbtc_fees(txs, lbtc, [commit_txid]))


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
def test_liquid_asset_htlc_onchain_restart(node_factory, bitcoind):
    """The timeout path (l1 closes: its HTLC-timeout tx, then that tx's
    delayed output), with restarts on the way: l1 once its commitment is
    mined, while its HTLC-timeout tx is in the mempool, and once that's
    mined; l2 while its to_remote sweep is in the mempool.  onchaind replays
    the close and lightningd rebuilds the same txs: l1 has a single L-BTC
    output in its wallet, and uses it again."""
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind, [{}, {'disconnect': ['-WIRE_UPDATE_FAIL_HTLC']}],
        wallet_outputs=(1, 2))
    inv = invoice_units(l2, 'balance', asset, PAY_MSAT)
    send_asset(l1, l2, PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    spk = {l1: close_to(l1), l2: close_to(l2)}
    lbtc_before = {n: wallet_sats(n) for n in (l1, l2)}

    # l2 doesn't know this payment, and hangs up instead of failing it.
    payment_hash = '01' * 32
    send_asset(l1, l2, HTLC_MSAT, payment_hash, payment_hash)
    l2.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_FAIL_HTLC')
    wait_for(lambda: not only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    expiry = only_one(only_one(l1.rpc.listpeerchannels()['channels'])['htlcs'])['expiry']
    assert l1.rpc.close(l2.info['id'], unilateraltimeout=1)['type'] == 'unilateral'
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)
    commit = bitcoind.rpc.getrawtransaction(commit_txid, True,
                                            bitcoind.rpc.getbestblockhash())
    htlc_n = only_one([v['n'] for v in commit['vout'] if v.get('asset') == asset
                       and units(v) == HTLC_MSAT // 1000])

    def mempool_spend(n):
        for txid in bitcoind.rpc.getrawmempool():
            tx = bitcoind.rpc.getrawtransaction(txid, True)
            if any(vin.get('txid') == commit_txid and vin['vout'] == n
                   for vin in tx['vin']):
                return tx
        return None

    l1.daemon.wait_for_log('Telling lightningd about OUR_HTLC_TIMEOUT_TX')
    l1.restart()
    l1.daemon.wait_for_log('Telling lightningd about OUR_HTLC_TIMEOUT_TX')

    # l2 sweeps its to_remote right away: restart it while that's waiting.
    wait_for(lambda: mempool_spend_of(bitcoind, commit_txid) is not None)
    l2.restart()

    # Once the HTLC expires, l1 broadcasts its HTLC-timeout tx: restart it
    # while that's in the mempool.
    sync_blockheight(bitcoind, [l1, l2])
    bitcoind.generate_block(expiry - bitcoind.rpc.getblockcount())
    wait_for(lambda: mempool_spend(htlc_n) is not None)
    htlc_timeout = mempool_spend(htlc_n)
    l1.restart()
    # It builds the very same tx again (same wallet L-BTC, same change).
    l1.daemon.wait_for_log('Liquid asset spend {}'.format(htlc_timeout['hex']))
    bitcoind.generate_block(1, wait_for_mempool=htlc_timeout['txid'])

    # Its output is swept after the CSV delay, after this restart too.
    sync_blockheight(bitcoind, [l1, l2])
    l1.restart()
    mine_until_forgotten(bitcoind, [l1, l2])

    assert sendpay_statuses(l1, payment_hash) == ['failed']
    txs = chain_txs(bitcoind, commit_height)
    witness, innum, tx = only_one(htlc_spends(txs, commit_txid, asset, HTLC_MSAT // 1000))
    assert tx['txid'] == htlc_timeout['txid']
    check_htlc_spend(witness, innum, tx, True, False)
    assert unspent_asset(txs, asset) == {spk[l1]: CHANNEL_UNITS - PAY_MSAT // 1000,
                                         spk[l2]: PAY_MSAT // 1000}
    lbtc_after = {n: wallet_sats(n) for n in (l1, l2)}
    assert (sum(lbtc_after.values()) == sum(lbtc_before.values())
            + lbtc_change(txs[commit_txid], lbtc) - lbtc_fees(txs, lbtc, [commit_txid]))


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
def test_liquid_asset_htlc_onchain_no_lbtc(node_factory, bitcoind):
    """l2 (not the opener) has no L-BTC when it closes with l1's HTLC in
    flight, so it can't pay for its HTLC-success tx: it retries every block,
    and gets it in once its wallet has L-BTC.  l1 learns the preimage from
    it."""
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind,
        [{}, {'disconnect': ['-WIRE_UPDATE_FULFILL_HTLC*2']}],
        wallet_outputs=(2, 0))
    inv = invoice_units(l2, 'balance', asset, PAY_MSAT)
    send_asset(l1, l2, PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    spk = {l1: close_to(l1), l2: close_to(l2)}
    assert wallet_sats(l2) == 0
    lbtc_before = wallet_sats(l1)

    inv = invoice_units(l2, 'in-flight', asset, HTLC_MSAT)
    send_asset(l1, l2, HTLC_MSAT, inv['payment_hash'], inv['payment_secret'])
    l2.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_FULFILL_HTLC')
    wait_for(lambda: not only_one(l2.rpc.listpeerchannels()['channels'])['peer_connected'])
    assert l2.rpc.close(l1.info['id'], unilateraltimeout=1)['type'] == 'unilateral'
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)

    # Nothing to pay the fee with: it can't spend the HTLC (l1 sweeps its
    # to_remote meanwhile).
    l2.daemon.wait_for_log(r'Liquid asset spend of .*: only 0sat L-BTC .* Will retry')
    for _ in range(3):
        sync_blockheight(bitcoind, [l1, l2])
        bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    l2.daemon.wait_for_log(r'Liquid asset spend of .*: only 0sat L-BTC .* Will retry')
    txs = chain_txs(bitcoind, commit_height)
    assert htlc_spends(txs, commit_txid, asset, HTLC_MSAT // 1000) == []
    assert sendpay_statuses(l1, inv['payment_hash']) == ['pending']

    # Now it has some.
    _, fund_txid = l2.fundwallet(10**6)
    mine_until_forgotten(bitcoind, [l1, l2])

    assert sendpay_statuses(l1, inv['payment_hash']) == ['complete']
    txs = chain_txs(bitcoind, commit_height)
    witness, innum, tx = only_one(htlc_spends(txs, commit_txid, asset, HTLC_MSAT // 1000))
    check_htlc_spend(witness, innum, tx, True, True)
    l2_units = (PAY_MSAT + HTLC_MSAT) // 1000
    assert unspent_asset(txs, asset) == {spk[l1]: CHANNEL_UNITS - l2_units,
                                         spk[l2]: l2_units}
    assert (wallet_sats(l1) + wallet_sats(l2)
            == lbtc_before + 10**6 + lbtc_change(txs[commit_txid], lbtc)
            - lbtc_fees(txs, lbtc, [commit_txid, fund_txid]))


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
@pytest.mark.skipif(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3',
                    reason="Makes use of the sqlite3 db")
def test_liquid_asset_penalty_htlc_tx(node_factory, bitcoind):
    """l1 (the opener) cheats from an old database: its revoked commitment
    has an HTLC from l2 in it, which l1 claims with its HTLC-success tx
    (it has the preimage) while l2 is offline.  When l2 is back, it takes
    that tx's output with the revocation key, and l1's to_local with its
    fee reserve change; it sweeps its own to_remote, and its payment
    completes (it saw the preimage)."""
    opts = {'may_reconnect': True, 'dev-no-reconnect': None}
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind,
        [{**opts, 'plugin': HOLD_PLUGIN, 'may_fail': True,
          # Its outputs get taken
          'broken_log': 'onchaind.*: Unknown spend'},
         opts])
    # l2 gets a balance (l1 holds incoming HTLCs, l2 doesn't).
    inv = invoice_units(l2, 'balance', asset, 2 * PAY_MSAT)
    send_asset(l1, l2, 2 * PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)

    # l2 pays l1, which holds the HTLC.
    sticky = invoice_units(l1, 'sticky', asset, HTLC_MSAT)
    send_asset(l2, l1, HTLC_MSAT, sticky['payment_hash'], sticky['payment_secret'])
    l1.daemon.wait_for_log('holding htlc')

    # Keep l1's database as it is now.
    db = os.path.join(l1.daemon.lightning_dir, TEST_NETWORK, 'lightningd.sqlite3')
    l1.stop()
    shutil.copyfile(db, db + '.bak')
    l1.start()
    l1.daemon.wait_for_log('holding htlc')

    # More payments: that commitment gets revoked.
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    push = invoice_units(l2, 'push', asset, PAY_MSAT // 10)
    send_asset(l1, l2, PAY_MSAT // 10, push['payment_hash'], push['payment_secret'])
    assert l1.rpc.waitsendpay(push['payment_hash'])['status'] == 'complete'
    spk = close_to(l2)
    lbtc_before = wallet_sats(l2)

    # l1 goes back to the old database, and closes while l2 is offline.
    l1.stop()
    l2.stop()
    shutil.copyfile(db + '.bak', db)
    l1.start()
    l1.daemon.wait_for_log('holding htlc')
    assert l1.rpc.close(l2.info['id'], unilateraltimeout=1)['type'] == 'unilateral'
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)

    # It fulfills the HTLC on-chain, with its HTLC-success tx.
    l1.rpc.releasehtlcs()
    l1.daemon.wait_for_log('Telling lightningd about OUR_HTLC_SUCCESS_TX')
    wait_for(lambda: mempool_spend_of(bitcoind, commit_txid) is not None)
    htlc_success = mempool_spend_of(bitcoind, commit_txid)
    bitcoind.generate_block(1, wait_for_mempool=htlc_success['txid'])

    l2.start()
    l2.daemon.wait_for_logs(['Telling lightningd about OUR_PENALTY_TX to resolve'
                             ' THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM',
                             'Telling lightningd about OUR_PENALTY_TX to resolve'
                             ' OUR_HTLC_FULFILL_TO_THEM/DELAYED_CHEAT_OUTPUT_TO_THEM'])
    mine_until_forgotten(bitcoind, [l2])

    assert sendpay_statuses(l2, sticky['payment_hash']) == ['complete']
    txs = chain_txs(bitcoind, commit_height)
    witness, innum, tx = only_one(htlc_spends(txs, commit_txid, asset, HTLC_MSAT // 1000))
    assert tx['txid'] == htlc_success['txid']
    check_htlc_spend(witness, innum, tx, True, True)
    # Its output 0 went to l2, with the revocation key.
    steal, _ = spend_of(txs, tx['txid'], 0)
    assert len(steal['vin'][0]['txinwitness']) == 3
    assert unspent_asset(txs, asset) == {spk: CHANNEL_UNITS}
    assert (wallet_sats(l2) == lbtc_before + lbtc_change(txs[commit_txid], lbtc)
            - lbtc_fees(txs, lbtc, [commit_txid, htlc_success['txid']]))


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
def test_liquid_asset_htlc_onchain_reorg(node_factory, bitcoind):
    """l1 closes with its HTLC in flight and times it out with its HTLC-timeout
    tx; then the block with that tx is reorged out.  onchaind restarts, and
    the asset spends that stopped once that tx spent their input (the
    HTLC-timeout tx itself) are built and broadcast again, so it all still
    resolves."""
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind, [{}, {'disconnect': ['-WIRE_UPDATE_FAIL_HTLC']}])
    inv = invoice_units(l2, 'balance', asset, PAY_MSAT)
    send_asset(l1, l2, PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)
    spk = {l1: close_to(l1), l2: close_to(l2)}
    lbtc_before = {n: wallet_sats(n) for n in (l1, l2)}

    payment_hash = '06' * 32
    send_asset(l1, l2, HTLC_MSAT, payment_hash, payment_hash)
    l2.daemon.wait_for_log('dev_disconnect: -WIRE_UPDATE_FAIL_HTLC')
    wait_for(lambda: not only_one(l1.rpc.listpeerchannels()['channels'])['peer_connected'])
    expiry = only_one(only_one(l1.rpc.listpeerchannels()['channels'])['htlcs'])['expiry']
    assert l1.rpc.close(l2.info['id'], unilateraltimeout=1)['type'] == 'unilateral'
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)

    # Mine until the HTLC-timeout tx is in a block.
    sync_blockheight(bitcoind, [l1, l2])
    bitcoind.generate_block(expiry - bitcoind.rpc.getblockcount())
    sync_blockheight(bitcoind, [l1, l2])
    txs = chain_txs(bitcoind, commit_height)
    htlc_n = only_one([v['n'] for v in txs[commit_txid]['vout'] if v.get('asset') == asset
                       and units(v) == HTLC_MSAT // 1000])
    wait_for(lambda: any(vin.get('txid') == commit_txid and vin['vout'] == htlc_n
                         for t in bitcoind.rpc.getrawmempool()
                         for vin in bitcoind.rpc.getrawtransaction(t, True)['vin']))
    htlc_block = bitcoind.generate_block(1)[0]
    htlc_height = bitcoind.rpc.getblock(htlc_block)['height']
    sync_blockheight(bitcoind, [l1, l2])
    l1.daemon.wait_for_log('Telling lightningd about OUR_DELAYED_RETURN_TO_WALLET'
                           ' to resolve OUR_HTLC_TIMEOUT_TX/DELAYED_OUTPUT_TO_US')

    # Take that block (only) out: the commitment stays confirmed.
    bitcoind.rpc.invalidateblock(htlc_block)
    bitcoind.wait_for_log(r'InvalidChainFound: invalid block=.*  height={}'
                          .format(htlc_height))
    addr = bitcoind.rpc.getnewaddress()
    for _ in range(2):
        bitcoind.rpc.generateblock(addr, [])
    sync_blockheight(bitcoind, [l1, l2])
    l1.daemon.wait_for_log('Chain reorganization!')
    l1.daemon.wait_for_log('Restarting onchaind')
    l1.daemon.wait_for_log('Telling lightningd about OUR_HTLC_TIMEOUT_TX'
                           ' to resolve OUR_UNILATERAL/OUR_HTLC')
    mine_until_forgotten(bitcoind, [l1, l2])

    assert sendpay_statuses(l1, payment_hash) == ['failed']
    txs = chain_txs(bitcoind, commit_height)
    witness, innum, tx = only_one(htlc_spends(txs, commit_txid, asset, HTLC_MSAT // 1000))
    check_htlc_spend(witness, innum, tx, True, False)
    assert unspent_asset(txs, asset) == {spk[l1]: CHANNEL_UNITS - PAY_MSAT // 1000,
                                         spk[l2]: PAY_MSAT // 1000}
    lbtc_after = {n: wallet_sats(n) for n in (l1, l2)}
    assert (sum(lbtc_after.values()) == sum(lbtc_before.values())
            + lbtc_change(txs[commit_txid], lbtc) - lbtc_fees(txs, lbtc, [commit_txid]))


@pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                    reason="Liquid asset channels need elementsd")
@pytest.mark.skipif(os.getenv('TEST_DB_PROVIDER', 'sqlite3') != 'sqlite3',
                    reason="Makes use of the sqlite3 db")
def test_liquid_asset_penalty_htlc_timeout_tx(node_factory, bitcoind):
    """l1 (the opener) cheats from an old database: its revoked commitment
    has an HTLC from it to l2 (which l2 holds), and l1 times it out with
    its HTLC-timeout tx while l2 is offline.  When l2 is back, it takes
    that tx's output with the revocation key, and l1's to_local with its
    fee reserve change, and sweeps its own to_remote.  (l2 makes l1 wait
    long enough before it could sweep any of them.)"""
    opts = {'may_reconnect': True, 'dev-no-reconnect': None}
    l1, l2, lbtc, asset = open_asset_channel(
        node_factory, bitcoind,
        [{**opts, 'may_fail': True,
          # Its outputs get taken
          'broken_log': 'onchaind.*: Unknown spend'},
         {**opts, 'watchtime-blocks': 30}])
    inv = invoice_units(l2, 'balance', asset, 2 * PAY_MSAT)
    send_asset(l1, l2, 2 * PAY_MSAT, inv['payment_hash'], inv['payment_secret'])
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'
    settled(l1, l2)

    # l1 pays l2, which holds the HTLC.  (Not HTLC_MSAT: that's what l1 has
    # left, and we tell outputs apart by their amount.)
    htlc_msat = HTLC_MSAT - 100_000_000
    l2.rpc.plugin_start(HOLD_PLUGIN)
    send_asset(l1, l2, htlc_msat, '07' * 32, '07' * 32, delay=10)
    l2.daemon.wait_for_log('holding htlc')
    expiry = only_one(only_one(l1.rpc.listpeerchannels()['channels'])['htlcs'])['expiry']

    # Keep l1's database as it is now.
    db = os.path.join(l1.daemon.lightning_dir, TEST_NETWORK, 'lightningd.sqlite3')
    l1.stop()
    shutil.copyfile(db, db + '.bak')
    l1.start()

    # Another HTLC: that commitment gets revoked.
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    send_asset(l1, l2, PAY_MSAT // 10, '08' * 32, '08' * 32, delay=40)
    wait_for(lambda: log_count(l2, 'holding htlc') == 2)
    spk = close_to(l2)
    lbtc_before = wallet_sats(l2)

    # l1 goes back to the old database, and closes while l2 is offline.
    l1.stop()
    l2.stop()
    shutil.copyfile(db + '.bak', db)
    l1.start()
    assert l1.rpc.close(l2.info['id'], unilateraltimeout=1)['type'] == 'unilateral'
    wait_for(lambda: len(bitcoind.rpc.getrawmempool()) == 1)
    commit_txid = only_one(bitcoind.rpc.getrawmempool())
    commit_height = bitcoind.rpc.getblockcount() + 1
    bitcoind.generate_block(1, wait_for_mempool=commit_txid)

    # Once the HTLC expires, it times it out with its HTLC-timeout tx.
    sync_blockheight(bitcoind, [l1])
    bitcoind.generate_block(expiry - bitcoind.rpc.getblockcount())
    l1.daemon.wait_for_log('Telling lightningd about OUR_HTLC_TIMEOUT_TX')
    wait_for(lambda: mempool_spend_of(bitcoind, commit_txid) is not None)
    htlc_timeout = mempool_spend_of(bitcoind, commit_txid)
    bitcoind.generate_block(1, wait_for_mempool=htlc_timeout['txid'])

    l2.start()
    l2.daemon.wait_for_logs(['Telling lightningd about OUR_PENALTY_TX to resolve'
                             ' THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM',
                             'Telling lightningd about OUR_PENALTY_TX to resolve'
                             ' THEIR_HTLC_TIMEOUT_TO_THEM/DELAYED_CHEAT_OUTPUT_TO_THEM'])
    mine_until_forgotten(bitcoind, [l2])

    txs = chain_txs(bitcoind, commit_height)
    witness, innum, tx = only_one(htlc_spends(txs, commit_txid, asset, htlc_msat // 1000))
    assert tx['txid'] == htlc_timeout['txid']
    check_htlc_spend(witness, innum, tx, True, False)
    # Its output 0 went to l2, with the revocation key.
    steal, _ = spend_of(txs, tx['txid'], 0)
    assert len(steal['vin'][0]['txinwitness']) == 3
    assert unspent_asset(txs, asset) == {spk: CHANNEL_UNITS}
    assert (wallet_sats(l2) == lbtc_before + lbtc_change(txs[commit_txid], lbtc)
            - lbtc_fees(txs, lbtc, [commit_txid, htlc_timeout['txid']]))
