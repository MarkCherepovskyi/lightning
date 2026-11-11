"""Swaps from Liquid to an EVM chain: a payer on Liquid pays an invoice of
a node on the EVM UtxoChain (contrib/evmchan), in ETH, through a provider
that runs one node on each chain.

    payer --L-BTC or asset--> provider_l   (liquid-regtest, elementsd)
                              provider_e --ETH--> payee   (regtest on anvil)

provider_l loads assetswap-plugin with assetswap-remote=eth:<provider_e's
RPC socket>: it holds leg 1, has provider_e pay the payee's invoice (leg 2)
and resolves leg 1 with the preimage that returns.

One pytest process runs both chains: TEST_NETWORK=liquid-regtest makes
node_factory's nodes Liquid ones on elementsd; evm_factory's are EvmNodes
on anvil, started with --network=regtest (see RegtestEvmNode).  Needs
elementsd, anvil and the built contract and plugins:

    contrib/liquid-poc/asset-swap/run-tests.sh
"""
from fixtures import *  # noqa: F401,F403
from helpers import ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN, decode_assetinvoice
from pyln.client import RpcError
from pyln.testing.utils import NodeFactory
from utils import TEST_NETWORK, only_one, sync_blockheight, wait_for

import json
import os
import pytest

pytestmark = pytest.mark.skipif(TEST_NETWORK != 'liquid-regtest',
                                reason="the payer side is on Liquid (elementsd)")

if TEST_NETWORK == 'liquid-regtest':
    from evmchan_utils import EvmChain, EvmNode
    from test_assetchan import issue_asset, node_opts as assetchan_opts, open_asset_channel
else:
    EvmNode = object

TO_MSAT = 100_000_000         # what the payee's invoice asks: 100,000 sat of ETH
EVM_CHANNEL_SAT = 200_000     # provider_e -> payee
LBTC_CHANNEL_SAT = 5_000_000  # payer -> provider_l
MARGINS = {'low': 2010, 'medium': 2030, 'high': 2060}  # pricetiers-plugin's


def tier_msat(to_msat, tier='medium', rate_ppm=1_000_000, fee_base_msat=0):
    return to_msat * (rate_ppm * MARGINS[tier] // 1000) // 1_000_000 + fee_base_msat


class RegtestEvmNode(EvmNode):
    """An EvmNode in a liquid-regtest test run: lightningd runs with
    --network=regtest, so its files go to <dir>/regtest, which links to the
    <dir>/liquid-regtest that pyln-testing (TEST_NETWORK) uses for them."""

    def __init__(self, node_id, lightning_dir, bitcoind, executor, **kwargs):
        os.makedirs(os.path.join(lightning_dir, TEST_NETWORK), exist_ok=True)
        link = os.path.join(lightning_dir, 'regtest')
        if not os.path.lexists(link):
            os.symlink(TEST_NETWORK, link)
        EvmNode.__init__(self, node_id, lightning_dir, bitcoind, executor, **kwargs)
        self.daemon.opts['network'] = 'regtest'


@pytest.fixture
def chain(directory):
    """anvil and a fresh UtxoChain, a block a second."""
    c = EvmChain(os.path.join(directory, 'anvil'))
    c.keep_mining()
    try:
        yield c
    finally:
        c.stop()


@pytest.fixture
def evm_factory(request, directory, test_name, chain, executor, db_provider, teardown_checks, jsonschemas):
    """node_factory's twin for the EVM nodes (their own directories)."""
    nf = NodeFactory(request, test_name, chain, executor, directory=os.path.join(directory, 'evm'),
                     db_provider=db_provider, node_cls=RegtestEvmNode, jsonschemas=jsonschemas)
    # pyln-testing derives a node's key from its directory's name
    # (lightning-<n>): numbered apart from node_factory's, the EVM nodes get
    # node ids of their own, not the Liquid nodes' twins.
    nf.next_id = 101
    yield nf
    ok, errs = nf.killall([not n.may_fail for n in nf.nodes])
    for e in errs:
        teardown_checks.add_error(e)


def normal(a, b):
    return all(c['state'] == 'CHANNELD_NORMAL' for c in a.rpc.listpeerchannels(b.info['id'])['channels'])


def open_evm_channel(chain, opener, peer, sats):
    opener.deposit(sats + 100_000)
    opener.rpc.connect(peer.info['id'], 'localhost', peer.port)
    res = opener.rpc.fundchannel(peer.info['id'], sats)
    chain.wait_event(res['txid'])
    wait_for(lambda: normal(opener, peer) and normal(peer, opener))
    return res


def to_us(node, peer):
    return only_one(node.rpc.listpeerchannels(peer.info['id'])['channels'])['to_us_msat']


def swap_nodes(node_factory, evm_factory, chain, bitcoind, pair_from='lbtc',
               payer_opts=None, provider_opts=None):
    """provider_e and payee on the EVM chain with a channel between them;
    provider_l (pricing pair_from:eth 1:1, leg 2 through provider_e) and the
    payer on Liquid, not yet connected.  The payer carries the same pair,
    for payassetinvoice's local estimate."""
    provider_e, payee = evm_factory.get_nodes(2, opts=[{}, {'plugin': ASSETSWAP_PLUGIN}])
    open_evm_channel(chain, provider_e, payee, EVM_CHANNEL_SAT)

    pair = f'{pair_from}:eth:1000000:0'
    provider_l = node_factory.get_node(options={
        **(provider_opts or {}),
        'plugin': (provider_opts or {}).get('plugin', []) + [ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN],
        'pricetiers-pair': pair,
        'assetswap-remote': 'eth:' + provider_e.rpc.socket_path,
    })
    payer = node_factory.get_node(options={
        **(payer_opts or {}),
        'plugin': (payer_opts or {}).get('plugin', []) + [ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN],
        'pricetiers-pair': pair,
        'assetswap-provider': provider_l.info['id'],
    })
    payer.rpc.connect(provider_l.info['id'], 'localhost', provider_l.port)
    ids = [n.info['id'] for n in (payer, provider_l, provider_e, payee)]
    assert len(set(ids)) == 4, ids
    return payer, provider_l, provider_e, payee


def open_lbtc_channel(bitcoind, payer, provider_l):
    payer.fundwallet(10**7)
    payer.rpc.fundchannel(provider_l.info['id'], LBTC_CHANNEL_SAT, announce=False)
    bitcoind.generate_block(6, wait_for_mempool=1)
    wait_for(lambda: normal(payer, provider_l) and normal(provider_l, payer))
    sync_blockheight(bitcoind, [payer, provider_l])


def eth_invoice(payee, label, msat=TO_MSAT):
    wrapped = payee.rpc.call('assetinvoice', {'amount_msat': msat, 'label': label,
                                              'description': label, 'asset': 'eth'})['assetinvoice']
    w = decode_assetinvoice(wrapped)
    assert w['asset'] == 'eth' and w['amount_msat'] == msat
    assert w['bolt11'].startswith('lnbcrt')   # an invoice of the EVM node's network
    return wrapped, w


def quote_state(provider_l, payment_hash):
    d = provider_l.rpc.listdatastore(['assetswap', 'quote', payment_hash])['datastore']
    return json.loads(only_one(d)['string'])['state'] if d else None


def test_lbtc_to_eth(node_factory, evm_factory, chain, bitcoind):
    """The payer only has L-BTC; payassetinvoice (automatic: the direct
    attempt fails, the invoice being for another network) swaps through the
    provider: the payee is paid in ETH on the EVM chain, the provider gets
    the medium tier in L-BTC on Liquid."""
    payer, provider_l, provider_e, payee = swap_nodes(node_factory, evm_factory, chain, bitcoind)
    open_lbtc_channel(bitcoind, payer, provider_l)

    wrapped, w = eth_invoice(payee, 'lbtc-to-eth')
    res = payer.rpc.call('payassetinvoice', {'assetinvoice': wrapped})
    assert res['status'] == 'complete'
    assert res['payment_hash'] == w['payment_hash']

    assert only_one(payee.rpc.listinvoices('lbtc-to-eth')['invoices'])['status'] == 'paid'
    wait_for(lambda: to_us(payee, provider_e) == TO_MSAT)
    wait_for(lambda: to_us(provider_l, payer) == tier_msat(TO_MSAT))
    assert quote_state(provider_l, w['payment_hash']) == 'claimed'
    # Leg 2 went out from the EVM node, with the payee's own hash.
    leg2 = only_one(provider_e.rpc.listsendpays(payment_hash=w['payment_hash'])['payments'])
    assert leg2['status'] == 'complete'


def test_lbtc_to_eth_leg2_fails(node_factory, evm_factory, chain, bitcoind):
    """The provider's EVM channel can't carry the invoice: leg 2 fails, so
    leg 1 is failed back and the payer keeps its L-BTC."""
    payer, provider_l, provider_e, payee = swap_nodes(node_factory, evm_factory, chain, bitcoind)
    open_lbtc_channel(bitcoind, payer, provider_l)
    before = to_us(payer, provider_l)

    too_much = (EVM_CHANNEL_SAT + 50_000) * 1000
    wrapped, w = eth_invoice(payee, 'too-much', too_much)
    with pytest.raises(RpcError):
        payer.rpc.call('payassetinvoice', {'assetinvoice': wrapped, 'from_asset': 'lbtc'})

    assert only_one(payee.rpc.listinvoices('too-much')['invoices'])['status'] == 'unpaid'
    wait_for(lambda: payer.rpc.listpeerchannels()['channels'][0]['htlcs'] == [])
    assert to_us(payer, provider_l) == before
    assert quote_state(provider_l, w['payment_hash']) == 'failed'
    assert provider_l.daemon.is_in_log(r'assetswap: leg 2 \(xpay\) failed')


def test_lbtc_to_eth_underpaid(node_factory, evm_factory, chain, bitcoind):
    """A leg 1 below the provider's own cheapest tier is refused before leg
    2 is tried: nothing leaves the EVM node."""
    payer, provider_l, provider_e, payee = swap_nodes(node_factory, evm_factory, chain, bitcoind)
    open_lbtc_channel(bitcoind, payer, provider_l)
    before = to_us(payer, provider_l)

    wrapped, w = eth_invoice(payee, 'underpaid')
    with pytest.raises(RpcError):
        payer.rpc.call('payassetinvoice', {'assetinvoice': wrapped, 'from_asset': 'lbtc',
                                           'from_amount_msat': tier_msat(TO_MSAT, 'low') - 1})

    assert provider_e.rpc.listsendpays(payment_hash=w['payment_hash'])['payments'] == []
    assert quote_state(provider_l, w['payment_hash']) is None
    wait_for(lambda: payer.rpc.listpeerchannels()['channels'][0]['htlcs'] == [])
    assert to_us(payer, provider_l) == before
    assert only_one(payee.rpc.listinvoices('underpaid')['invoices'])['status'] == 'unpaid'


def test_asset_to_eth(node_factory, evm_factory, chain, bitcoind):
    """A Liquid issued asset to ETH: leg 1 in an asset channel opened with
    contrib/assetchan, whose htlc_accepted hook would fail an HTLC that
    arrives in an asset no invoice of the provider asks for -- assetswap's
    hook runs first and takes it."""
    asset = issue_asset(bitcoind)
    opts = assetchan_opts(bitcoind, asset)
    opts['plugin'] = [opts['plugin']]
    payer, provider_l, provider_e, payee = swap_nodes(node_factory, evm_factory, chain, bitcoind,
                                                      pair_from=asset, payer_opts=opts,
                                                      provider_opts=opts)
    open_asset_channel(bitcoind, payer, provider_l, asset)
    sync_blockheight(bitcoind, [payer, provider_l])
    before = to_us(provider_l, payer)

    to_msat = 10_000_000   # 10,000 sat of ETH, for 20,300 units of the asset
    wrapped, w = eth_invoice(payee, 'asset-to-eth', to_msat)
    res = payer.rpc.call('payassetinvoice', {'assetinvoice': wrapped, 'from_asset': asset})
    assert res['status'] == 'complete'

    assert only_one(payee.rpc.listinvoices('asset-to-eth')['invoices'])['status'] == 'paid'
    wait_for(lambda: to_us(payee, provider_e) == to_msat)
    wait_for(lambda: to_us(provider_l, payer) == before + tier_msat(to_msat))
    assert only_one(provider_l.rpc.listpeerchannels(payer.info['id'])['channels'])['asset'] == asset
