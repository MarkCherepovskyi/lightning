"""PoC: gossip for Liquid asset channels.

An announced asset channel is gossiped on its own network: its
channel_announcement and channel_updates carry the asset id where
`chain_hash` goes.  Only nodes that follow that asset
(`liquid-gossip-asset=<id>`) accept it, after checking that the funding
output really is in that asset.  Everyone else ignores it as an unknown
chain, as BOLT 7 requires.  Pathfinding keeps networks apart: `getroutes` and
`xpay` stay in L-BTC unless asked for an asset.

Catching up works per network too: gossip queries (query_channel_range,
query_short_channel_ids) and the gossip_timestamp_filter stream only cover
the network named by their chain_hash, and followers ask for each asset
network they follow.

    TEST_NETWORK=liquid-regtest pytest tests/test_liquid_asset_gossip.py
"""
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from test_liquid_asset_poc import (CHANNEL_UNITS, LBTC_CHANNEL_SAT, PAY_MSAT,
                                   asset_invoice, channel_by_funding,
                                   fund_asset_channel, settled)
from utils import wait_for, TEST_NETWORK, TIMEOUT

import pytest
import struct
import subprocess


pytestmark = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                reason="Liquid asset channels need elementsd")


def issue_asset(bitcoind):
    lbtc = bitcoind.rpc.dumpassetlabels()['bitcoin']
    asset = bitcoind.rpc.issueasset(1000, 0, False)['asset']
    bitcoind.generate_block(1)
    return lbtc, asset


def open_announced(bitcoind, opener, peer, asset, lbtc):
    """An announced L-BTC channel and an announced asset channel; returns
    their short_channel_ids once both are normal."""
    opener.fundwallet(10**7)
    # (fund_asset_channel mines blocks for its inputs, so it goes first)
    asset_txid = fund_asset_channel(bitcoind, opener, peer, asset, lbtc,
                                    announce=True)
    lbtc_txid = opener.rpc.fundchannel(peer.info['id'], LBTC_CHANNEL_SAT,
                                       announce=True)['txid']
    # 6 blocks: deep enough to announce.
    bitcoind.generate_block(6, wait_for_mempool=[lbtc_txid, asset_txid])
    for n in (opener, peer):
        wait_for(lambda: all(c['state'] == 'CHANNELD_NORMAL'
                             for c in n.rpc.listpeerchannels()['channels']))
    return (channel_by_funding(opener, lbtc_txid)['short_channel_id'],
            channel_by_funding(opener, asset_txid)['short_channel_id'])


def gossiped(node, scid):
    """Both directions of scid, as node's gossip map has them."""
    return node.rpc.listchannels(scid)['channels']


def test_asset_announce_needs_allowlist(node_factory, bitcoind):
    """Announcing an asset channel needs the asset in liquid-gossip-asset;
    private asset channels don't."""
    _, asset = issue_asset(bitcoind)
    l1, l2 = node_factory.line_graph(2, fundchannel=False)

    with pytest.raises(RpcError, match='liquid-gossip-asset'):
        l1.rpc.call('fundchannel_start', {'id': l2.info['id'],
                                          'amount': CHANNEL_UNITS,
                                          'announce': True, 'asset': asset})

    l1.rpc.call('fundchannel_start', {'id': l2.info['id'],
                                      'amount': CHANNEL_UNITS,
                                      'announce': False, 'asset': asset})
    l1.rpc.fundchannel_cancel(l2.info['id'])


def test_asset_channel_gossip(node_factory, bitcoind):
    """Followers store an announced asset channel with its asset and its
    capacity in asset units; other nodes ignore it."""
    lbtc, asset = issue_asset(bitcoind)
    follow = {'liquid-gossip-asset': asset}
    # node_announcement has no network, so l4 can see l2's before any
    # channel of l2's that it accepts ("Bad gossip order").  Harmless.
    l1, l2, l3, l4 = node_factory.get_nodes(4, opts=[follow, follow, follow,
                                                     {'allow_bad_gossip': True}])
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    for n in (l3, l4):
        n.rpc.connect(l2.info['id'], 'localhost', l2.port)

    lbtc_scid, asset_scid = open_announced(bitcoind, l1, l2, asset, lbtc)

    # l3 follows the asset: it checked the funding output and stored the
    # channel, in asset units, marked with the asset.
    wait_for(lambda: len(gossiped(l3, asset_scid)) == 2)
    for c in gossiped(l3, asset_scid):
        assert c['liquid_asset'] == asset
        assert c['amount_msat'] == CHANNEL_UNITS * 1000
    for c in gossiped(l3, lbtc_scid):
        assert 'liquid_asset' not in c
        assert c['amount_msat'] == LBTC_CHANNEL_SAT * 1000
    # The channel's own nodes see it the same way.
    for n in (l1, l2):
        wait_for(lambda: len(gossiped(n, asset_scid)) == 2)
        assert all(c['liquid_asset'] == asset for c in gossiped(n, asset_scid))

    # l4 doesn't follow the asset.  Once it has a channel_update that l2
    # stored *after* the asset channel, it has seen (and ignored) the asset
    # gossip too.
    l2.rpc.setchannel(lbtc_scid, feebase=4321)
    wait_for(lambda: any(c['base_fee_millisatoshi'] == 4321
                         for c in gossiped(l4, lbtc_scid)))
    assert gossiped(l4, asset_scid) == []

    # A follower that joins later checks an older block (the filtered-block
    # path rather than its own UTXO set).
    l5 = node_factory.get_node(options=follow)
    l5.rpc.connect(l2.info['id'], 'localhost', l2.port)
    wait_for(lambda: len(gossiped(l5, asset_scid)) == 2)
    assert all(c['liquid_asset'] == asset and c['amount_msat'] == CHANNEL_UNITS * 1000
               for c in gossiped(l5, asset_scid))


def test_asset_payments_over_gossip(node_factory, bitcoind):
    """With announced channels, routing finds paths in one asset by itself:
    no route hints, no hand-built routes."""
    lbtc, asset = issue_asset(bitcoind)
    follow = {'liquid-gossip-asset': asset}
    l1, l2, l3 = node_factory.get_nodes(3, opts=follow)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l2.rpc.connect(l3.info['id'], 'localhost', l3.port)

    lbtc12, asset12 = open_announced(bitcoind, l1, l2, asset, lbtc)
    lbtc23, asset23 = open_announced(bitcoind, l2, l3, asset, lbtc)
    for scid in (lbtc12, asset12, lbtc23, asset23):
        wait_for(lambda: len(gossiped(l1, scid)) == 2)

    def route_scids(**kwargs):
        routes = l1.rpc.call('getroutes', {'source': l1.info['id'],
                                           'destination': l3.info['id'],
                                           'amount_msat': PAY_MSAT,
                                           'layers': ['auto.localchans'],
                                           'maxfee_msat': PAY_MSAT // 100,
                                           'final_cltv': 18,
                                           **kwargs})['routes']
        return {hop['short_channel_id_dir'].split('/')[0]
                for r in routes for hop in r['path']}

    # Routes stay inside one network: L-BTC unless asked for the asset.
    assert route_scids() == {lbtc12, lbtc23}
    assert route_scids(asset=asset) == {asset12, asset23}

    # xpay in the asset, found purely from gossip.
    inv = asset_invoice(l3, 'gossip-asset', asset)
    l1.rpc.call('xpay', {'invstring': inv['bolt11'], 'asset': asset})

    # xpay in L-BTC still uses only L-BTC channels.
    inv = l3.rpc.invoice(PAY_MSAT, 'gossip-lbtc', 'in L-BTC')
    l1.rpc.xpay(inv['bolt11'])

    # An asset invoice paid without saying so goes over L-BTC, and the
    # receiver refuses it.
    inv = asset_invoice(l3, 'gossip-wrong', asset)
    with pytest.raises(RpcError):
        l1.rpc.xpay(inv['bolt11'])
    l3.daemon.wait_for_log('arrived in the wrong Liquid asset')

    settled(l1, l2, l3)
    # Exactly one payment arrived on each of l3's channels.
    for scid in (asset23, lbtc23):
        ch = [c for c in l3.rpc.listpeerchannels()['channels']
              if c['short_channel_id'] == scid][0]
        assert PAY_MSAT <= ch['to_us_msat'] < 2 * PAY_MSAT


def asset_network(asset):
    """An asset's gossip network, as it goes on the wire where chain_hash
    goes: the asset id (which elements-cli prints byte-reversed)."""
    return bytes.fromhex(asset)[::-1].hex()


def scid_str(v):
    return f"{v >> 40}x{(v >> 16) & 0xFFFFFF}x{v & 0xFFFF}"


def range_scids(node, network):
    """Ask node (as a raw peer) which channels it knows in this network."""
    msgs = node.query_gossip('query_channel_range', network, 0, 1000000,
                             filters=['0109', '0107', '0012', '0105'])
    scids = []
    for m in msgs:
        b = bytes.fromhex(m)
        # reply_channel_range: type, chain_hash, first_blocknum,
        # number_of_blocks, sync_complete, len, encoded_short_ids
        assert b[:2] == bytes.fromhex('0108')
        assert b[2:34].hex() == network
        enclen = struct.unpack('>H', b[43:45])[0]
        enc = b[45:45 + enclen]
        assert enc[0] == 0  # uncompressed
        scids += [scid_str(struct.unpack('>Q', enc[i:i + 8])[0])
                  for i in range(1, len(enc), 8)]
    return scids


def scid_query(node, network, scid):
    """Ask node (as a raw peer) about one channel; returns the message types
    of its replies and the final reply_short_channel_ids_end."""
    encoded = subprocess.run(['devtools/mkencoded', '--scids', '00', scid],
                             check=True, timeout=TIMEOUT,
                             stdout=subprocess.PIPE).stdout.strip().decode()
    msgs = node.query_gossip('query_short_channel_ids', network, encoded,
                             filters=['0109', '0107', '0012'])
    return [m[:4] for m in msgs[:-1]], msgs[-1]


def message_network(hmsg):
    """The network of a channel_announcement / channel_update, else None."""
    b = bytes.fromhex(hmsg)
    if b[:2] == bytes.fromhex('0102'):
        return b[2 + 64:2 + 64 + 32].hex()
    if b[:2] == bytes.fromhex('0100'):
        flen = struct.unpack('>H', b[2 + 256:2 + 258])[0]
        off = 2 + 258 + flen
        return b[off:off + 32].hex()
    return None


def streamed_gossip(node):
    """Everything node streams to a peer that asks for all gossip on the
    chain's own network (as an ordinary, non-asset node does)."""
    out = subprocess.run(['devtools/gossipwith', '--all-gossip',
                          '--network={}'.format(TEST_NETWORK),
                          '--timeout-after=5',
                          '{}@localhost:{}'.format(node.info['id'], node.port)],
                         check=True, timeout=TIMEOUT,
                         stdout=subprocess.PIPE).stdout
    msgs = []
    while len(out):
        length = struct.unpack('>H', out[0:2])[0]
        msgs.append(out[2:2 + length].hex())
        out = out[2 + length:]
    return msgs


def test_asset_gossip_queries(node_factory, bitcoind, chainparams):
    """Gossip queries and the gossip stream only cover the network their
    chain_hash names."""
    lbtc, asset = issue_asset(bitcoind)
    follow = {'liquid-gossip-asset': asset}
    l1, l2, l3 = node_factory.get_nodes(3, opts=follow)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l3.rpc.connect(l2.info['id'], 'localhost', l2.port)
    lbtc_scid, asset_scid = open_announced(bitcoind, l1, l2, asset, lbtc)
    # l3 isn't in either channel, so it sends us nothing of its own.
    for scid in (lbtc_scid, asset_scid):
        wait_for(lambda: len(gossiped(l3, scid)) == 2)

    chain = chainparams['chain_hash']
    network = asset_network(asset)

    # Ranges: each network has just its own channel.
    assert range_scids(l3, chain) == [lbtc_scid]
    assert range_scids(l3, network) == [asset_scid]

    # Asking about the asset channel on the chain's network: nothing.
    types, end = scid_query(l3, chain, asset_scid)
    assert types == []
    assert end == '0106' + chain + '01'
    # On the asset's network: its announcement and both updates.
    types, end = scid_query(l3, network, asset_scid)
    assert types.count('0100') == 1 and types.count('0102') == 2
    assert end == '0106' + network + '01'

    # Streaming everything on the chain's own network leaks no asset gossip.
    streamed = [message_network(m) for m in streamed_gossip(l3)]
    assert chain in streamed
    assert network not in streamed


def test_asset_gossip_catch_up(node_factory, bitcoind):
    """A follower catches up on an asset network with gossip queries, even
    when its only peer doesn't stream gossip to it."""
    lbtc, asset = issue_asset(bitcoind)
    follow = {'liquid-gossip-asset': asset}
    l1, l2, l3, l4 = node_factory.get_nodes(4, opts=follow)
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    l4.rpc.connect(l2.info['id'], 'localhost', l2.port)
    lbtc_scid, asset_scid = open_announced(bitcoind, l1, l2, asset, lbtc)
    wait_for(lambda: len(gossiped(l4, asset_scid)) == 2)

    # l4 stops streaming gossip to peers: l3 can only ask for it.
    l4.rpc.call('dev-suppress-gossip')
    l3.rpc.connect(l4.info['id'], 'localhost', l4.port)

    wait_for(lambda: len(gossiped(l3, asset_scid)) == 2)
    assert all(c['liquid_asset'] == asset and c['amount_msat'] == CHANNEL_UNITS * 1000
               for c in gossiped(l3, asset_scid))
    l3.daemon.wait_for_log('sending query_channel_range on network ' + asset)
