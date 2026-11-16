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
from helpers import ASSETSWAP_PLUGIN, PRICETIERS_PLUGIN, decode_assetinvoice, encode_swap_metadata
from pyln.client import RpcError
from pyln.testing.utils import NodeFactory
from utils import TEST_NETWORK, only_one, sync_blockheight, wait_for, TIMEOUT

import base64
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
               payer_opts=None, provider_opts=None, payee_opts=None):
    """provider_e and payee on the EVM chain with a channel between them;
    provider_l (pricing pair_from:eth 1:1, leg 2 through provider_e) and the
    payer on Liquid, not yet connected.  The payer carries the same pair,
    for payassetinvoice's local estimate."""
    # The payee's invoices ask for a final CLTV of 144, as the Base Sepolia
    # nodes' do: leg 2's maxdelay has to leave room for all of it.
    payee_opts = payee_opts or {}
    provider_e, payee = evm_factory.get_nodes(2, opts=[{}, {**payee_opts, 'cltv-final': 144,
                                                            'plugin': [ASSETSWAP_PLUGIN] + payee_opts.get('plugin', [])}])
    open_evm_channel(chain, provider_e, payee, EVM_CHANNEL_SAT)

    pair = f'{pair_from}:eth:1000000:0'
    # (node_factory's own keywords, not lightningd options.)
    payer_opts, provider_opts = dict(payer_opts or {}), dict(provider_opts or {})
    payer_kw = {k: payer_opts.pop(k) for k in ('may_reconnect', 'broken_log') if k in payer_opts}
    provider_kw = {k: provider_opts.pop(k) for k in ('may_reconnect', 'broken_log') if k in provider_opts}
    # The provider runs assetswap as an important plugin: it takes no new
    # swaps otherwise (lightningd mustn't outlive it while it holds leg 1).
    important = (provider_opts or {}).get('important-plugin', [])
    if isinstance(important, str):
        important = [important]
    provider_l = node_factory.get_node(**provider_kw, options={
        **(provider_opts or {}),
        'important-plugin': important + [ASSETSWAP_PLUGIN],
        'plugin': (provider_opts or {}).get('plugin', []) + [PRICETIERS_PLUGIN],
        'pricetiers-pair': pair,
        'assetswap-remote': 'eth:' + provider_e.rpc.socket_path,
    })
    payer = node_factory.get_node(**payer_kw, options={
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
    assert quote_state(provider_l, w['payment_hash']) == 'complete'
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
    contrib/assetchan (an important plugin), whose htlc_accepted hook leaves
    an HTLC no invoice of the provider asks for to other plugins: assetswap
    takes it, whichever hook runs first."""
    asset = issue_asset(bitcoind)
    opts = assetchan_opts(bitcoind, asset)
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


def test_leg2_not_own_invoice(node_factory, evm_factory, chain, bitcoind):
    """A leg 1 naming an invoice of the provider's own (here its EVM node's)
    is refused: paying it would only mark that invoice paid with the
    provider's own funds (xpay to itself never leaves the node), in exchange
    for a leg 1 priced in another asset."""
    payer, provider_l, provider_e, payee = swap_nodes(node_factory, evm_factory, chain, bitcoind)
    open_lbtc_channel(bitcoind, payer, provider_l)

    inv = provider_e.rpc.invoice(TO_MSAT, 'ours', 'ours')
    scid = only_one(payer.rpc.listpeerchannels(provider_l.info['id'])['channels'])['short_channel_id']
    amount = 3 * tier_msat(TO_MSAT)
    route = [{'id': provider_l.info['id'], 'channel': scid, 'amount_msat': amount, 'delay': 500}]
    payer.rpc.sendpay(route, inv['payment_hash'], amount_msat=amount,
                      payment_metadata=encode_swap_metadata(inv['bolt11'], 'eth'))
    with pytest.raises(RpcError, match='WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS'):
        payer.rpc.waitsendpay(inv['payment_hash'])

    assert provider_l.daemon.is_in_log(r"leg 2's invoice is ours")
    assert only_one(provider_e.rpc.listinvoices('ours')['invoices'])['status'] == 'unpaid'
    assert provider_e.rpc.listsendpays(payment_hash=inv['payment_hash'])['payments'] == []
    assert quote_state(provider_l, inv['payment_hash']) is None


def test_wrapper_tampered(node_factory, evm_factory, chain, bitcoind):
    """The assetinvoice wrapper's own fields aren't signed: payassetinvoice
    refuses one whose payment_hash or amount differ from its bolt11's (leg 1
    would be sent on a hash the invoice doesn't settle, or priced above it),
    or whose block time would lock leg 1 for an absurd time. Nothing is sent."""
    payer, provider_l, provider_e, payee = swap_nodes(node_factory, evm_factory, chain, bitcoind)
    open_lbtc_channel(bitcoind, payer, provider_l)
    wrapped, w = eth_invoice(payee, 'tampered')

    def rewrap(**changes):
        raw = json.dumps({**w, **changes}).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')

    for changes, why in (({'payment_hash': '02' * 32}, 'payment_hash is not'),
                         ({'amount_msat': 10 * TO_MSAT}, 'amount_msat is not'),
                         ({'block_seconds': 10**6}, 'block_seconds is out of range')):
        with pytest.raises(RpcError, match=why):
            payer.rpc.call('payassetinvoice', {'assetinvoice': rewrap(**changes), 'from_asset': 'lbtc'})
    assert payer.rpc.listsendpays()['payments'] == []
    assert only_one(payee.rpc.listinvoices('tampered')['invoices'])['status'] == 'unpaid'

    # The untouched wrapper still pays.
    assert payer.rpc.call('payassetinvoice', {'assetinvoice': wrapped})['status'] == 'complete'


HOLD = os.path.join(os.getcwd(), 'contrib/evmchan/hold.py')


def test_leg1_held_past_giveup(node_factory, evm_factory, chain, bitcoind, executor):
    """Invariant S1: while leg 2 is pending the provider never fails leg 1,
    even close to leg 1's expiry: the payee could still settle leg 2 (it
    holds it here), and failing leg 1 then would pay it for nothing.  The
    provider restarts meanwhile, so it waits in `settle` (what a restart
    replays) rather than in its xpay call.  Once the payee settles, leg 1 is
    resolved with the preimage."""
    payer, provider_l, provider_e, payee = swap_nodes(node_factory, evm_factory, chain, bitcoind,
                                                      payer_opts={'may_reconnect': True},
                                                      provider_opts={'may_reconnect': True,
                                                                     'broken_log': 'still holding it'},
                                                      payee_opts={'plugin': [HOLD], 'hold-all': False})
    open_lbtc_channel(bitcoind, payer, provider_l)
    wrapped, w = eth_invoice(payee, 'held')
    payee.rpc.call('hold-add', {'payment_hash': w['payment_hash']})

    fut = executor.submit(payer.rpc.call, 'payassetinvoice', {'assetinvoice': wrapped, 'from_asset': 'lbtc'})
    wait_for(lambda: payee.rpc.call('hold-list')['held'] == [w['payment_hash']])
    htlc = only_one(only_one(provider_l.rpc.listpeerchannels(payer.info['id'])['channels'])['htlcs'])
    assert quote_state(provider_l, w['payment_hash']) == 'paying'

    provider_l.restart()
    provider_l.daemon.wait_for_log(r'holding leg 1 of {}'.format(w['payment_hash']))
    # Past the old give-up height (half the 18-block safety margin before
    # expiry), short of the expiry itself.
    height = provider_l.rpc.getinfo()['blockheight']
    bitcoind.generate_block(htlc['expiry'] - 9 - height + 1)
    sync_blockheight(bitcoind, [payer, provider_l])
    provider_l.daemon.wait_for_log(r'still holding it, as leg 2 may still be settled', timeout=TIMEOUT)
    assert not fut.done()
    assert quote_state(provider_l, w['payment_hash']) == 'paying'
    assert len(only_one(payer.rpc.listpeerchannels(provider_l.info['id'])['channels'])['htlcs']) == 1

    payee.rpc.call('hold-release', {'payment_hash': w['payment_hash']})
    assert fut.result(TIMEOUT)['status'] == 'complete'
    assert only_one(payee.rpc.listinvoices('held')['invoices'])['status'] == 'paid'
    wait_for(lambda: quote_state(provider_l, w['payment_hash']) == 'complete')
