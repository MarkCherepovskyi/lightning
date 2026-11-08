# SPDX-License-Identifier: MIT
"""Core Lightning on the EVM UtxoChain contract (contrib/evmchan, SPEC 7
"Integration"): unmodified lightningd with --disable-plugin=bcli and
contrib/evmchan/evmbackend.py, against a fresh anvil and contract per test.
No bitcoind is started: the `bitcoind` fixture is the anvil chain.

    TEST_DIR=/tmp/evt pytest -p no:trackflaky -n 4 tests/test_evmchan.py

(contrib/evmchan/run-tests.sh runs them on Linux in Docker.) Facts on chain
are read from the contract (TxApplied events, isUnspent, withdrawable,
balances), and CLN's view is checked against them."""
from fixtures import *  # noqa: F401,F403
from pyln.client import RpcError
from pyln.testing.utils import only_one, wait_for

from evmchan_utils import ALARM_LAG, ALARM_SYNC, ALARM_TAG, HOLD, SAT, EvmChain, EvmNode, btc, evm

import base64
import hashlib
import os
import pytest
import re
import time

RESOLVED = re.compile(r'Resolved (\w+)/(\w+) by our proposal (\w+) \(([0-9a-f]{64})\)')
FORGOTTEN = 'onchaind complete, forgetting peer'
ANCHOR_SAT = 330
HELD = ('SENT_ADD_ACK_REVOCATION', 'RCVD_ADD_ACK_REVOCATION')
REPLAY_BUG = ("upstream lightningd/onchain_control.c: when a tx spends two outputs onchaind watches and "
              "onchaind answers 'not interested' for one, the replay after a restart (replay_unwatch_txid) "
              "drops the watch on that whole tx, so its own output's later spend is never seen and onchaind "
              "never finishes; see test_restart_replay_two_spends")


@pytest.fixture
def chain(request, directory):
    """anvil + UtxoChain, mining a block a second. Parametrized indirectly
    with EvmChain's options ({'keep_state': True}: anvil can be restarted;
    {'wallclock': True}: heights keep real time)."""
    c = EvmChain(os.path.join(directory, 'anvil'), **getattr(request, 'param', {}))
    c.keep_mining(wallclock=c.wallclock)
    try:
        yield c
    finally:
        c.stop()


@pytest.fixture
def bitcoind(chain):
    """pyln-testing's NodeFactory takes a `bitcoind`: the chain is ours."""
    yield chain


@pytest.fixture
def node_cls():
    return EvmNode


# ------------------------------------------------------------ helpers

def connect(a, b):
    a.rpc.connect(b.info['id'], 'localhost', b.port)


def both_normal(a, b):
    return a.state(b) == 'CHANNELD_NORMAL' and b.state(a) == 'CHANNELD_NORMAL'


def open_channel(chain, opener, peer, amount, **kwargs):
    """fundchannel from opener's wallet; waits for the contract to apply the
    funding tx and both sides to reach CHANNELD_NORMAL. Returns
    (fundchannel's result, the funding tx's event)."""
    connect(opener, peer)
    res = opener.rpc.fundchannel(peer.info['id'], amount, **kwargs)
    ev = chain.wait_event(res['txid'])
    assert (res['txid'], res['outnum']) not in [s for e in chain.events() for s in e.spends()]
    wait_for(lambda: both_normal(opener, peer))
    return res, ev


def disconnect(a, b):
    a.rpc.disconnect(b.info['id'], force=True)
    wait_for(lambda: not a.channel(b)['peer_connected'] and not b.channel(a)['peer_connected'])


def resolutions(node, txtype=None, outtype=None, proposal=None):
    """txids of onchaind's 'Resolved <tx>/<output> by our proposal <p>'."""
    return [m.group(4) for m in RESOLVED.finditer(node.log_text())
            if (txtype is None or m.group(1) == txtype) and (outtype is None or m.group(2) == outtype)
            and (proposal is None or m.group(3) == proposal)]


def wait_resolved(chain, node, txtype, outtype, proposal, burst=4):
    """Mines until onchaind resolves an output by our own proposal; the
    proposal's tx must be one the contract applied. Returns its event."""
    chain.wait_mining(lambda: resolutions(node, txtype, outtype, proposal), burst=burst)
    txid = resolutions(node, txtype, outtype, proposal)[-1]
    return chain.wait_event(txid)


def wait_forgotten(chain, *nodes):
    """Mines until every node's onchaind has resolved everything to depth
    100 and forgotten the channel."""
    chain.wait_mining(lambda: all(n.daemon.is_in_log(FORGOTTEN) for n in nodes), burst=10, blocks=60)
    for n in nodes:
        wait_for(lambda: n.rpc.listpeerchannels()['channels'] == [])


def wallet_has(node, txid, vout):
    return any(o['txid'] == txid and o['output'] == vout and o['status'] == 'confirmed'
               for o in node.rpc.listfunds()['outputs'])


def ends_in_wallet(chain, node, txid):
    """Every output of txid, or whatever the contract shows its value went
    on to (the wallet may spend it, e.g. as fee input for an HTLC tx), is
    a confirmed output of node's wallet."""
    e = chain.event(txid)
    outs = [x for i in range(len(e.tx.outputs)) for x in leaves(chain, txid, i)]
    return bool(outs) and all(wallet_has(node, t, v) for t, v in outs)


def htlcs_held(a, b, n):
    """n HTLCs irrevocably committed on both sides."""
    def ok():
        for x, y in ((a, b), (b, a)):
            hs = x.channel(y)['htlcs']
            if len(hs) != n or any(h['state'] not in HELD for h in hs):
                return False
        return True
    wait_for(ok)


def no_htlcs(a, b):
    wait_for(lambda: not a.channel(b)['htlcs'] and not b.channel(a)['htlcs'])


def send_direct(payer, payee, msat, payment_hash, payment_secret, delay):
    """sendpay of one HTLC straight to the peer, with this CLTV delay."""
    scid = payer.channel(payee)['short_channel_id']
    route = [{'id': payee.info['id'], 'channel': scid, 'amount_msat': msat, 'delay': delay, 'style': 'tlv'}]
    payer.rpc.sendpay(route, payment_hash, payment_secret=payment_secret)


def leaves(chain, txid, vout):
    """The unspent outputs a contract output ended up in, following the
    spends recorded in the contract's events."""
    sp = chain.spender(txid, vout)
    if sp is None:
        assert chain.is_unspent(txid, vout)
        return [(txid, vout)]
    assert not chain.is_unspent(txid, vout)
    out = []
    for i, (_, spk) in enumerate(sp.tx.outputs):
        if not btc.is_exit_script(spk) and not btc.is_op_return(spk):
            out += leaves(chain, sp.txid, i)
    return out


def psbt_spending(spk, amount=1_000_000, prevout=(hashlib.sha256(b'coin').digest(), 0)):
    """A PSBT v0 with one input that spends a coin (made up, by default)
    with this scriptPubKey, described by its witness utxo, and no outputs.
    prevout: (txid in internal byte order, vout)."""
    def kv(k, v):
        return btc.varint(len(k)) + k + btc.varint(len(v)) + v
    unsigned = (b'\x02\x00\x00\x00' + btc.varint(1) + prevout[0] + prevout[1].to_bytes(4, 'little')
                + btc.varint(0) + b'\xfd\xff\xff\xff' + btc.varint(0) + b'\x00' * 4)
    utxo = amount.to_bytes(8, 'little') + btc.varint(len(spk)) + spk
    raw = b'psbt\xff' + kv(b'\x00', unsigned) + b'\x00' + kv(b'\x01', utxo) + b'\x00'
    return base64.b64encode(raw).decode()


def psbt_with_prevtx(spk, amount=1_000_000, redeem=None):
    """A PSBT v0 with one input spending a made-up coin with this
    scriptPubKey, with its full previous tx (non-witness utxo, which
    dual funding sends to the peer), its witness utxo, and a redeem script
    if given (CLN wants one for P2SH)."""
    def kv(k, v):
        return btc.varint(len(k)) + k + btc.varint(len(v)) + v
    prev = btc.Tx(2, [btc.TxIn(os.urandom(32), 0)], [(amount, spk)], 0)
    unsigned = btc.Tx(2, [btc.TxIn(prev.txid(), 0, b'', 0xfffffffd)], [], 0)
    utxo = amount.to_bytes(8, 'little') + btc.varint(len(spk)) + spk
    raw = (b'psbt\xff' + kv(b'\x00', unsigned.serialize(witness=False)) + b'\x00'
           + kv(b'\x00', prev.serialize(witness=False)) + kv(b'\x01', utxo)
           + (kv(b'\x04', redeem) if redeem else b'') + b'\x00')
    return base64.b64encode(raw).decode()


def log_mark(node):
    node.daemon.logs_catchup()
    return len(node.daemon.logs)


def spends_to_local_sweep(chain, commit, htlc_tx):
    """REPLAY_BUG's trigger: the HTLC-timeout tx takes, as its fee input,
    the output of a tx that spent only commitment outputs (the to_local
    sweep), so it spends two outputs onchaind watches."""
    for t, _ in htlc_tx.spends():
        e = chain.event(t) if t != commit.txid else None
        if e is not None and e.spends() and all(s[0] == commit.txid for s in e.spends()):
            return True
    return False


def check_replay_bug(chain, node, htlc_tx, since):
    """Returns only if onchaind stopped finishing as REPLAY_BUG says, and for
    no reason of ours: since the restart (log position `since`) its replay
    still waits for the sweep of the HTLC-timeout output, which the contract
    applied, and keeps re-sending it; meanwhile lightningd runs, is at the
    chain's tip, and is served at that sweep's height the block built here
    from the contract's events. Otherwise pytest.fail."""
    if not node.running():
        pytest.fail('lightningd is not running')
    try:
        tip = chain.wait_synced([node])
    except ValueError:
        pytest.fail('lightningd is at height {}, behind the tip {}'.format(
            node.rpc.getinfo()['blockheight'], chain.expected_tip()))
    sweep = chain.spender(htlc_tx.txid, 0)
    if sweep is None or sweep.height > tip:
        pytest.fail('the HTLC-timeout output\'s sweep is not in the chain CLN has: {}'.format(sweep))
    served = node.rpc.call('getrawblockbyheight', {'height': sweep.height})['blockhash']
    if served != chain.reference_blocks(sweep.height)[sweep.height]:
        pytest.fail('the block served at height {} is not the contract\'s'.format(sweep.height))
    stuck = 'waiting confirmation that we spent DELAYED_OUTPUT_TO_US ({}'.format(htlc_tx.txid[:16])
    if not (node.daemon.is_in_log(re.escape(stuck), start=since)
            and node.daemon.is_in_log('Expected error broadcasting tx', start=since)):
        pytest.fail('onchaind did not finish, but not as REPLAY_BUG says')


# ------------------------------------------------------------ funding, paying, gossip

def test_open_pay_gossip(node_factory, chain):
    """Deposits to P2TR and P2WPKH; a channel funded from both; a payment;
    the channel announced and verified by gossip (getutxout) at a third node."""
    l1, l2, l3 = node_factory.get_nodes(3)

    # Deposits (SPEC 3.3): one output each, at the deposit tx's height.
    deps = {}
    for kind, prefix in (('p2tr', '5120'), ('bech32', '0014')):
        txid, addr = l1.deposit(400_000, kind)
        e = chain.event(txid)
        assert e.is_deposit
        assert e.tx.outputs == [(400_000, btc.address_script(addr))]
        assert e.tx.outputs[0][1].hex().startswith(prefix)
        assert chain.is_unspent(txid, 0)
        out = only_one([o for o in l1.rpc.listfunds()['outputs'] if o['txid'] == txid])
        assert out['amount_msat'] == 400_000_000
        assert out['scriptpubkey'] == e.tx.outputs[0][1].hex()
        assert out['blockheight'] == e.height
        deps[kind] = txid

    # A channel spending both (wallet P2TR and P2WPKH inputs).
    res, ev = open_channel(chain, l1, l2, 500_000, utxos=['{}:0'.format(t) for t in deps.values()])
    assert sorted(ev.spends()) == sorted((t, 0) for t in deps.values())
    assert not any(chain.is_unspent(t, 0) for t in deps.values())
    amount, spk, _ = chain.output(res['txid'], res['outnum'])
    assert amount == 500_000 and spk[:2] == b'\x00\x20' and len(spk) == 34
    assert chain.is_unspent(res['txid'], res['outnum'])
    # The short channel id is the contract's height and the tx's position
    # in that virtual block (the coinbase comes first).
    scid = l1.channel(l2)['short_channel_id']
    height, index, outnum = map(int, scid.split('x'))
    at_height = [e.txid for e in chain.events() if e.height == ev.height]
    assert (height, outnum) == (ev.height, res['outnum'])
    assert at_height[index - 1] == res['txid']

    inv = l2.rpc.invoice(50_000_000, 'p1', 'p1')['bolt11']
    l1.rpc.pay(inv)
    wait_for(lambda: l2.channel(l1)['to_us_msat'] == 50_000_000)
    assert l2.rpc.listinvoices('p1')['invoices'][0]['status'] == 'paid'

    # Gossip: l3 only talks to l2, and takes the announcement only after
    # checking the funding output with getutxout.
    connect(l3, l2)
    wait_for(lambda: len(l3.rpc.listchannels(scid)['channels']) == 2)
    wait_for(lambda: [n.get('alias') for n in l3.rpc.listnodes(l1.info['id'])['nodes']]
             == [l1.rpc.getinfo()['alias']])
    assert l3.rpc.call('getutxout', {'txid': res['txid'], 'vout': res['outnum']}) == \
        {'amount': 500_000, 'script': spk.hex()}
    for n in (l1, l2):
        assert len(n.rpc.listchannels(scid)['channels']) == 2
    # l3 routes a payment over it.
    l3.rpc.connect(l1.info['id'], 'localhost', l1.port)
    l3.deposit(300_000)
    open_channel(chain, l3, l2, 200_000)
    wait_for(lambda: len(l3.rpc.listchannels()['channels']) == 4)
    l3.rpc.pay(l1.rpc.invoice(10_000_000, 'via-l2', 'via-l2')['bolt11'])
    assert l1.rpc.listinvoices('via-l2')['invoices'][0]['status'] == 'paid'


# ------------------------------------------------------------ closes

def test_mutual_close(node_factory, chain):
    l1, l2 = node_factory.get_nodes(2)
    l1.deposit(1_000_000)
    res, _ = open_channel(chain, l1, l2, 600_000)
    l1.rpc.pay(l2.rpc.invoice(150_000_000, 'm', 'm')['bolt11'])
    wait_for(lambda: l2.channel(l1)['to_us_msat'] == 150_000_000)
    ours = l1.channel(l2)['to_us_msat'] // 1000

    close = l1.rpc.close(l2.info['id'])
    assert close['type'] == 'mutual'
    ev = chain.wait_event(close['txids'][0])
    assert ev.spends() == [(res['txid'], res['outnum'])]
    assert not chain.is_unspent(res['txid'], res['outnum'])
    # l2's output is its balance exactly; l1 (the opener) pays the fee.
    outs = {spk.hex(): (i, amount) for i, (amount, spk) in enumerate(ev.tx.outputs)}
    assert len(outs) == 2
    wait_for(lambda: all(n.state(p) in ('CLOSINGD_COMPLETE', 'FUNDING_SPEND_SEEN', 'ONCHAIN')
                         for n, p in ((l1, l2), (l2, l1))))
    fee = chain.fee_of(ev.txid)
    assert 0 < fee < 5_000
    for node, expect in ((l2, 150_000), (l1, ours - fee)):
        wait_for(lambda: any(o['txid'] == ev.txid and o['status'] == 'confirmed'
                             for o in node.rpc.listfunds()['outputs']))
        o = only_one([o for o in node.rpc.listfunds()['outputs'] if o['txid'] == ev.txid])
        i, amount = outs[o['scriptpubkey']]
        assert (o['output'], o['amount_msat']) == (i, amount * 1000)
        assert amount == expect
        assert chain.is_unspent(ev.txid, i)
    wait_forgotten(chain, l1, l2)


@pytest.mark.parametrize('closer', ['opener', 'peer'])
def test_unilateral_close(node_factory, chain, closer):
    """A force close by either side: the closer sweeps its to_local after
    the CSV delay, the other side's to_remote is in its wallet (and spends),
    and both onchainds finish."""
    l1, l2 = node_factory.get_nodes(2)
    l1.deposit(1_000_000)
    res, _ = open_channel(chain, l1, l2, 600_000)
    l1.rpc.pay(l2.rpc.invoice(200_000_000, 'u', 'u')['bolt11'])
    wait_for(lambda: l2.channel(l1)['to_us_msat'] == 200_000_000)
    a, b = (l1, l2) if closer == 'opener' else (l2, l1)
    balance = {l1: l1.channel(l2)['to_us_msat'] // 1000, l2: 200_000}

    disconnect(a, b)
    fee = a.channel(b)['last_tx_fee_msat']          # CLN's fee for the commitment it holds
    close = a.rpc.close(b.info['id'], unilateraltimeout=1)
    assert close['type'] == 'unilateral'
    commit = chain.wait_event(close['txids'][0])
    assert commit.spends() == [(res['txid'], res['outnum'])]
    assert not chain.is_unspent(res['txid'], res['outnum'])
    # Its one input is the funding output as the contract recorded it, and
    # what the outputs leave of it is CLN's fee, a sane one.
    amount, spk, _ = chain.output(res['txid'], res['outnum'])
    assert amount == 600_000 and spk[:2] == b'\x00\x20'
    assert 0 < chain.fee_of(commit.txid) < 5_000
    assert chain.fee_of(commit.txid) * 1000 == fee
    assert sorted(x for x, _ in commit.tx.outputs)[:2] == [ANCHOR_SAT, ANCHOR_SAT]
    wait_for(lambda: a.state(b) == 'ONCHAIN' and b.state(a) == 'ONCHAIN')

    # The other side's to_remote (P2WSH, CSV 1) is in its wallet, and is
    # exactly its balance (the opener's output pays the fee and anchors).
    wait_for(lambda: any(o['txid'] == commit.txid for o in b.rpc.listfunds()['outputs']))
    remote = only_one([o for o in b.rpc.listfunds()['outputs'] if o['txid'] == commit.txid])
    expect = {l2: balance[l2], l1: balance[l1] - 2 * ANCHOR_SAT - chain.fee_of(commit.txid)}
    assert remote['amount_msat'] == expect[b] * 1000
    assert chain.output(commit.txid, remote['output'])[0] == expect[b]

    # The closer's to_local, after its 40-block delay.
    sweep = wait_resolved(chain, a, 'OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US', 'OUR_DELAYED_RETURN_TO_WALLET')
    (_, local), = sweep.spends()
    assert sweep.spends() == [(commit.txid, local)]
    assert sweep.height - commit.height >= 40
    assert chain.output(commit.txid, local)[0] == expect[a]
    assert not chain.is_unspent(commit.txid, local)
    (swept, _), = sweep.tx.outputs           # one output, to a's wallet
    assert 0 < expect[a] - swept < 5_000      # the sweep's fee
    wait_for(lambda: wallet_has(a, sweep.txid, 0))
    assert only_one([o for o in a.rpc.listfunds()['outputs'] if o['txid'] == sweep.txid])['amount_msat'] \
        == swept * 1000
    assert chain.is_unspent(sweep.txid, 0)

    # b spends its to_remote with an ordinary withdraw (a P2WSH input).
    w = b.rpc.withdraw(b.rpc.newaddr('p2tr')['p2tr'], 'all', utxos=['{}:{}'.format(commit.txid, remote['output'])])
    assert chain.wait_event(w['txid']).spends() == [(commit.txid, remote['output'])]
    assert not chain.is_unspent(commit.txid, remote['output'])
    wait_forgotten(chain, a, b)


# ------------------------------------------------------------ HTLCs on chain

@pytest.mark.parametrize('outcome,closer', [('timeout', 'payer'), ('timeout', 'payee'),
                                            ('success', 'payee'), ('success', 'payer')])
def test_htlc_onchain(node_factory, chain, outcome, closer):
    """An HTLC l1 -> l2 resolved on chain, from both commitments:
    timeout/payer: l1's HTLC-timeout tx from its own commitment, then its sweep;
    timeout/payee: l1 times it out from l2's commitment directly;
    success/payee: l2's HTLC-success tx from its own commitment, then its sweep;
    success/payer: l2 claims it with the preimage from l1's commitment."""
    l1, l2 = node_factory.get_nodes(2, opts=[{}, {'plugin': HOLD, 'hold-all': False}])
    l1.deposit(1_000_000)
    l2.deposit(300_000)      # zero-fee HTLC txs and anchors take wallet inputs
    open_channel(chain, l1, l2, 800_000)
    msat = 50_000_000
    if outcome == 'timeout':
        # A hash nobody has the preimage of, held by l2 until it times out.
        phash, secret = hashlib.sha256(os.urandom(32)).hexdigest(), '11' * 32
        delay = 45
    else:
        inv = l2.rpc.invoice(msat, 'h', 'h')
        phash, secret = inv['payment_hash'], inv['payment_secret']
        delay = 90           # room for l2's cltv-final check when it is released
    l2.rpc.call('hold-add', {'payment_hash': phash})
    send_direct(l1, l2, msat, phash, secret, delay)
    htlcs_held(l1, l2, 1)
    expiry = only_one(l1.channel(l2)['htlcs'])['expiry']

    a, b = (l1, l2) if closer == 'payer' else (l2, l1)
    disconnect(a, b)
    close = a.rpc.close(b.info['id'], unilateraltimeout=1)
    commit = chain.wait_event(close['txids'][0])
    htlc_vout = only_one([i for i, (x, _) in enumerate(commit.tx.outputs) if x == msat // 1000])
    wait_for(lambda: a.state(b) == 'ONCHAIN' and b.state(a) == 'ONCHAIN')
    if outcome == 'success':
        assert l2.rpc.call('hold-release', {'payment_hash': phash})['released'] == [phash]

    if (outcome, closer) == ('timeout', 'payer'):
        tx = wait_resolved(chain, l1, 'OUR_UNILATERAL', 'OUR_HTLC', 'OUR_HTLC_TIMEOUT_TX')
        second = wait_resolved(chain, l1, 'OUR_HTLC_TIMEOUT_TX', 'DELAYED_OUTPUT_TO_US',
                               'OUR_DELAYED_RETURN_TO_WALLET')
    elif (outcome, closer) == ('timeout', 'payee'):
        tx = wait_resolved(chain, l1, 'THEIR_UNILATERAL', 'OUR_HTLC', 'OUR_HTLC_TIMEOUT_TO_US')
        second = None
    elif (outcome, closer) == ('success', 'payee'):
        tx = wait_resolved(chain, l2, 'OUR_UNILATERAL', 'THEIR_HTLC', 'OUR_HTLC_SUCCESS_TX')
        second = wait_resolved(chain, l2, 'OUR_HTLC_SUCCESS_TX', 'DELAYED_OUTPUT_TO_US',
                               'OUR_DELAYED_RETURN_TO_WALLET')
    else:
        tx = wait_resolved(chain, l2, 'THEIR_UNILATERAL', 'THEIR_HTLC', 'THEIR_HTLC_FULFILL_TO_US')
        second = None
    assert (commit.txid, htlc_vout) in tx.spends()
    assert not chain.is_unspent(commit.txid, htlc_vout)
    winner = l1 if outcome == 'timeout' else l2
    if outcome == 'timeout':
        # A timeout is final only above the HTLC's expiry (its nLockTime, or
        # the script's CLTV), which the contract enforces.
        assert tx.tx.locktime == expiry and tx.height > expiry
    else:
        assert tx.height <= expiry
    if second is not None:
        # The HTLC tx's output is to_self_delay (40) blocks late.
        (_, v), = [s for s in second.spends() if s[0] == tx.txid]
        assert second.height - tx.height >= 40
        assert not chain.is_unspent(tx.txid, v)
        last = second
    else:
        last = tx
    wait_for(lambda: ends_in_wallet(chain, winner, last.txid))

    # The payer learns the outcome from the chain.
    status = 'failed' if outcome == 'timeout' else 'complete'
    wait_for(lambda: only_one(l1.rpc.listsendpays(payment_hash=phash)['payments'])['status'] == status)
    if outcome == 'success':
        pay = only_one(l1.rpc.listsendpays(payment_hash=phash)['payments'])
        assert hashlib.sha256(bytes.fromhex(pay['payment_preimage'])).hexdigest() == phash
        assert l2.rpc.listinvoices('h')['invoices'][0]['status'] == 'paid'
    wait_forgotten(chain, l1, l2)


# ------------------------------------------------------------ penalty

def test_penalty(node_factory, chain):
    """l2 broadcasts a revoked commitment with HTLCs both ways (signed with
    dev-sign-last-tx); l1 penalizes to_local and both HTLCs and ends up with
    every output (but the anchors) of that commitment."""
    opts = {'plugin': HOLD, 'hold-all': False}
    # The cheater's onchaind says what it sees (as in CLN's own penalty tests).
    l2_opts = dict(opts, broken_log=r'Could not find resolution for output .*: did \*we\* cheat\?')
    l1, l2 = node_factory.get_nodes(2, opts=[opts, l2_opts])
    l1.deposit(2_000_000)
    l2.deposit(300_000)
    res, _ = open_channel(chain, l1, l2, 1_000_000, push_msat=400_000_000)
    to_l2 = l2.rpc.invoice(60_000_000, 'to-l2', 'to-l2')
    to_l1 = l1.rpc.invoice(70_000_000, 'to-l1', 'to-l1')
    l2.rpc.call('hold-add', {'payment_hash': to_l2['payment_hash']})
    l1.rpc.call('hold-add', {'payment_hash': to_l1['payment_hash']})
    send_direct(l1, l2, 60_000_000, to_l2['payment_hash'], to_l2['payment_secret'], 60)
    send_direct(l2, l1, 70_000_000, to_l1['payment_hash'], to_l1['payment_secret'], 60)
    htlcs_held(l1, l2, 2)
    revoked = l2.rpc.call('dev-sign-last-tx', {'id': l1.info['id']})['tx']
    rtx = btc.Tx.parse(bytes.fromhex(revoked))
    assert len(rtx.outputs) == 6     # 2 anchors, to_local, to_remote, 2 HTLCs
    l2.rpc.call('hold-release', {})
    l1.rpc.call('hold-release', {})
    no_htlcs(l1, l2)
    l1.rpc.pay(l2.rpc.invoice(10_000_000, 'more', 'more')['bolt11'])
    no_htlcs(l1, l2)

    # l2 cheats: its revoked commitment goes to the contract.
    assert l2.rpc.call('sendrawtransaction', {'tx': revoked, 'allowhighfees': False})['success']
    commit = chain.wait_event(rtx.txid_hex())
    assert commit.spends() == [(res['txid'], res['outnum'])]
    for outtype in ('DELAYED_CHEAT_OUTPUT_TO_THEM', 'THEIR_HTLC', 'OUR_HTLC'):
        p = wait_resolved(chain, l1, 'THEIR_REVOKED_UNILATERAL', outtype, 'OUR_PENALTY_TX')
        assert all(s[0] == commit.txid for s in p.spends())

    # Everything but the anchors ended up in l1's wallet, none in l2's.
    final = []
    for i, (amount, _) in enumerate(commit.tx.outputs):
        if amount != ANCHOR_SAT:
            final += leaves(chain, commit.txid, i)
    # l1's to_remote (unspent, in its wallet), and one penalty output each
    # for l2's to_local and the two HTLCs.
    assert len(final) == 4 and len({t for t, _ in final} - {commit.txid}) == 3
    wait_for(lambda: all(wallet_has(l1, t, v) for t, v in final))
    l2_outs = {(o['txid'], o['output']) for o in l2.rpc.listfunds()['outputs']}
    assert not l2_outs & set(final)
    total = sum(chain.output(t, v)[0] for t, v in final)
    spent_by_l1 = {t for t, _ in final} - {commit.txid}
    fees = sum(chain.fee_of(t) for t in spent_by_l1)
    assert total + fees == sum(a for a, _ in commit.tx.outputs) - 2 * ANCHOR_SAT
    wait_forgotten(chain, l1)


# ------------------------------------------------------------ refusals (SPEC 5.4, 5.6)

def test_refuse_non_anchor_and_no_anchor_peer(node_factory, chain):
    """A non-anchor channel_type is refused, an anchor one is not; a peer
    without anchors (feature 22/23) is disconnected either way."""
    l1, l2 = node_factory.get_nodes(2)
    # l3 is told why (a WARNING message) each time it is sent away.
    l3 = node_factory.get_node(options={'dev-force-features': ['-63', '-23']}, allow_warning=True)
    l1.deposit(1_000_000)
    connect(l1, l2)
    with pytest.raises(RpcError, match=r'evmbackend: .*channel_type'):
        l1.rpc.fundchannel(l2.info['id'], 100_000, channel_type=[12])
    with pytest.raises(RpcError, match=r'evmbackend: .*channel_type'):
        l1.rpc.fundchannel_start(l2.info['id'], 100_000, channel_type=[12])
    # multifundchannel takes no channel_type: its filter is seen refusing a lease.
    with pytest.raises(RpcError, match='evmbackend: liquidity-ad leases are not supported'):
        l1.rpc.multifundchannel([{'id': l2.info['id'], 'amount': 100_000, 'request_amt': 100_000,
                                  'compact_lease': '029a002d000000004b2003e8'}])
    assert l1.rpc.listpeerchannels()['channels'] == []
    assert [e.txid for e in chain.events() if not e.is_deposit] == []
    res, ev = open_channel(chain, l1, l2, 100_000, channel_type=[12, 22])
    assert l1.channel(l2)['channel_type']['names'] == ['static_remotekey/even', 'anchors/even']

    # l3 has no anchors: disconnected whichever side connects.
    said = 'disconnecting peer {}: no anchors'.format(l3.info['id'])
    for n, (a, b) in enumerate(((l3, l1), (l1, l3)), start=1):
        try:
            a.rpc.connect(b.info['id'], 'localhost', b.port)
        except RpcError:
            pass
        wait_for(lambda: len(re.findall(said, l1.log_text())) == n)
        wait_for(lambda: not any(p['connected'] for p in l1.rpc.listpeers(l3.info['id'])['peers']))
        wait_for(lambda: len(re.findall('Received WIRE_WARNING: WARNING: this node only opens '
                                        'anchors_zero_fee_htlc_tx channels', l3.log_text())) >= n)
    assert l1.rpc.listpeerchannels(l3.info['id'])['channels'] == []
    assert l3.rpc.listpeerchannels()['channels'] == []


def test_dual_fund_and_lease(node_factory, chain):
    """Dual funding: an open with segwit inputs from both sides works; a
    PSBT with a non-segwit input is refused; a liquidity-ad lease is refused
    asking for one and offering one."""
    df = {'experimental-dual-fund': None}
    l1, l2 = node_factory.get_nodes(2, opts=[df, dict(df, **{'funder-policy': 'match', 'funder-policy-mod': 100,
                                                             'lease-fee-base-sat': '100sat',
                                                             'lease-fee-basis': 100,
                                                             'funder-lease-requests-only': False})])
    l1.deposit(2_000_000)
    l2.deposit(1_000_000, 'bech32')
    res, ev = open_channel(chain, l1, l2, 400_000)
    # l2 matched it with its own (P2WPKH) input.
    assert l1.channel(l2)['funding']['remote_funds_msat'] == 400_000_000
    spent = {chain.output(t, v)[1][:2] for t, v in ev.spends()}
    assert spent == {b'\x51\x20', b'\x00\x14'}
    assert chain.output(res['txid'], res['outnum'])[0] == 800_000

    # A PSBT input the contract could never spend: P2PKH, and P2SH.
    for spk in (bytes.fromhex('76a914') + bytes(20) + bytes.fromhex('88ac'),
                bytes.fromhex('a914') + bytes(20) + bytes.fromhex('87')):
        with pytest.raises(RpcError, match='evmbackend: funding input 0 is not P2WPKH or P2TR'):
            l1.rpc.openchannel_init(l2.info['id'], 100_000, psbt_spending(spk))
    # The wallet's own (segwit) PSBT gets through the hook.
    psbt = l1.rpc.fundpsbt(150_000, '253perkw', 250, reserve=0)['psbt']
    init = l1.rpc.openchannel_init(l2.info['id'], 150_000, psbt)
    l1.rpc.openchannel_abort(init['channel_id'])

    # Leases: asking for one is refused by l1's own rpc_command hook ...
    with pytest.raises(RpcError, match='evmbackend: liquidity-ad leases are not supported'):
        l1.rpc.fundchannel(l2.info['id'], 100_000, request_amt=100_000, compact_lease='029a002d000000004b2003e8')
    # ... and l2's openchannel2 hook refuses a lease request (dev-queryrates
    # sends one without going through l1's rpc_command filter).
    with pytest.raises(RpcError, match='liquidity-ad leases are not supported'):
        l1.rpc.dev_queryrates(l2.info['id'], 100_000, 100_000)
    assert l2.daemon.is_in_log('refused a dual-funded open from {}: liquidity-ad leases'.format(l1.info['id']))
    assert len(l1.rpc.listpeerchannels()['channels']) == 1


NO_FILTER = {'evm-dev-no-outbound-filter': None}


@pytest.mark.parametrize('dual', [False, True])
def test_accepter_refuses(node_factory, chain, dual):
    """The accepter's own hooks (SPEC 5.4), with the opener's rpc_command
    checks turned off (evm-dev-no-outbound-filter): a non-anchor channel_type
    (openchannel, or openchannel2 when dual-funded), and, dual-funded, an
    opener's input the contract could never spend (openchannel2_changed).
    Nothing reaches the chain."""
    df = {'experimental-dual-fund': None} if dual else {}
    l1, l2 = node_factory.get_nodes(2, opts=[dict(df, **NO_FILTER), dict(df)])
    l1.deposit(1_000_000)
    connect(l1, l2)
    hook = 'refused a dual-funded open' if dual else 'refused an open'
    said = '{} from {}: channel_type [12] is not allowed'.format(hook, l1.info['id'])
    with pytest.raises(RpcError, match=r'channel_type \[12\] is not allowed on this chain'):
        l1.rpc.fundchannel(l2.info['id'], 100_000, channel_type=[12])
    assert l2.daemon.is_in_log(re.escape(said))
    assert not l1.daemon.is_in_log('evmbackend.py: refused')      # not the opener's own check
    if dual:
        # P2PKH and P2SH never get this far: CLN's interactive tx refuses a
        # non-segwit prevtx itself (BOLT 2). A segwit one the contract can't
        # spend (P2WSH) reaches the accepter's openchannel2_changed hook.
        p2wpkh = b'\x00\x14' + bytes(20)
        for n, (spk, redeem) in enumerate(((bytes.fromhex('76a914') + bytes(20) + bytes.fromhex('88ac'), None),
                                           (b'\xa9\x14' + bytes(20) + b'\x87', p2wpkh)), start=1):
            with pytest.raises(RpcError):
                init = l1.rpc.openchannel_init(l2.info['id'], 100_000, psbt_with_prevtx(spk, redeem=redeem))
                l1.rpc.openchannel_update(init['channel_id'], init['psbt'])
            wait_for(lambda: len(re.findall('tx-abort: Invalid tx sent. Not SegWit', l2.log_text())) == n)
            if not l1.rpc.listpeers(l2.info['id'])['peers'][0]['connected']:
                connect(l1, l2)
        # Refused by evmbackend, which aborts the open before answering
        # (lightningd would crash on a plain 'reject' here, upstream).
        with pytest.raises(RpcError):
            init = l1.rpc.openchannel_init(l2.info['id'], 100_000, psbt_with_prevtx(bytes.fromhex('0020') + bytes(32)))
            l1.rpc.openchannel_update(init['channel_id'], init['psbt'])
        l2.daemon.wait_for_log(r'openchannel2_changed for \w+: refused: funding input 0 is not P2WPKH or P2TR')
        assert l2.running()
    wait_for(lambda: l1.rpc.listpeerchannels()['channels'] == [] and l2.rpc.listpeerchannels()['channels'] == [])
    assert [e.txid for e in chain.events() if not e.is_deposit] == []


def test_regtest_defaults_refuse(node_factory, chain):
    """A node with regtest's default options refuses opens, both ways."""
    l1 = node_factory.get_node()
    # Its own BROKEN lines say so; its margins (3 blocks) are below what the
    # lag can be, so the lag alarm goes off too.
    l2 = node_factory.get_node(evm_options=False,
                               broken_log=[r'evmbackend.py: CLN option below SPEC 5.6: ', ALARM_LAG])
    l2.daemon.wait_for_log('CLN option below SPEC 5.6: --watchtime-blocks is 6')
    for o in ('--cltv-delta is 6', '--cltv-final is 10'):
        assert l2.daemon.is_in_log('CLN option below SPEC 5.6: ' + o)
    for o in ('--max-concurrent-htlcs is 483', 'splicing is on'):
        assert l2.daemon.is_in_log('CLN option below SPEC 5.6: ' + o)
    assert l2.evm_status()['opens_refused'].startswith('lightningd runs with options below SPEC 5.6')
    assert not l1.daemon.is_in_log('CLN option below SPEC 5.6')
    l1.deposit(500_000)
    l2.deposit(500_000)
    connect(l2, l1)
    with pytest.raises(RpcError, match='evmbackend: channel opens are refused: lightningd runs with options '
                                       'below SPEC 5.6'):
        l2.rpc.fundchannel(l1.info['id'], 100_000)
    with pytest.raises(RpcError, match='options below SPEC 5.6'):
        l1.rpc.fundchannel(l2.info['id'], 100_000)
    assert l1.rpc.listpeerchannels()['channels'] == [] and l2.rpc.listpeerchannels()['channels'] == []
    assert [e.txid for e in chain.events() if not e.is_deposit] == []


# ------------------------------------------------------------ exits (SPEC 4.4, 5.5)

def test_exit_withdraw(node_factory, chain):
    """withdraw to an exit address credits withdrawable (and the in-tx fee
    to the submitter); evm-withdraw and the contract's withdraw() pay out
    exactly that."""
    l1 = node_factory.get_node()
    l1.deposit(1_000_000)
    me = l1.eth_address
    other = evm.Account(os.urandom(32))
    chain.fund_eth(other.address, 10**18)
    assert chain.withdrawable(me) == 0 and l1.evm_status()['withdrawable_wei'] == 0

    for who, sats in ((me, 100_000), (other.address, 250_000)):
        addr = l1.rpc.call('evm-exitaddr', {'ethaddress': who})['address']
        assert addr == btc.exit_address(evm.address_bytes(who), 'bcrt') and addr.startswith('bcrt1s')
        before = {x: chain.withdrawable(x) for x in (me, other.address)}
        w = l1.rpc.withdraw(addr, sats)
        ev = chain.wait_event(w['txid'])
        exit_spk = btc.exit_script(evm.address_bytes(who))
        vout = only_one([i for i, (_, spk) in enumerate(ev.tx.outputs) if spk == exit_spk])
        assert ev.tx.outputs[vout][0] == sats
        # No UTXO for an exit: the contract pays it out instead.
        assert not chain.is_unspent(w['txid'], vout)
        assert l1.rpc.call('getutxout', {'txid': w['txid'], 'vout': vout}) == {'amount': None, 'script': None}
        fee = chain.fee_of(w['txid'])
        assert fee > 0
        credit = {me: fee * SAT, other.address: 0}
        credit[who] += sats * SAT
        for x in (me, other.address):
            assert chain.withdrawable(x) - before[x] == credit[x]
        wait_for(lambda: not any(o['status'] != 'confirmed' for o in l1.rpc.listfunds()['outputs']))
    assert l1.evm_status()['withdrawable_wei'] == chain.withdrawable(me)

    def paid(rc, address):
        n = int(rc['blockNumber'], 16)
        delta = int(chain.rpc.request('eth_getBalance', address, evm.qty(n)), 16) - \
            int(chain.rpc.request('eth_getBalance', address, evm.qty(n - 1)), 16)
        return delta + int(rc['gasUsed'], 16) * int(rc['effectiveGasPrice'], 16)

    # evm-withdraw: the node key gets its exit and its fees.
    owed = chain.withdrawable(me)
    wd = l1.rpc.call('evm-withdraw')
    assert wd['status'] == 'mined' and wd['amount_wei'] == owed
    assert paid(chain.rpc.request('eth_getTransactionReceipt', wd['evm_tx']), me) == owed
    assert chain.withdrawable(me) == 0
    with pytest.raises(RpcError, match='nothing to withdraw'):
        l1.rpc.call('evm-withdraw')
    # The other address calls withdraw() itself.
    rc = chain.rpc.send(other, chain.contract, data=evm.selector('withdraw()'))
    assert paid(rc, other.address) == 250_000 * SAT
    assert chain.withdrawable(other.address) == 0
    # The wallet kept the change: deposit - exits - fees.
    fees = sum(chain.fee_of(e.txid) for e in chain.events() if not e.is_deposit)
    total = sum(o['amount_msat'] for o in l1.rpc.listfunds()['outputs']) // 1000
    assert total == 1_000_000 - 350_000 - fees


# ------------------------------------------------------------ token mode (SPEC 4.6)

@pytest.mark.parametrize('chain', [{'token_decimals': 6}], indirect=True)
def test_token_channel(node_factory, chain):
    """A UtxoChainToken on a 6-decimal ERC-20 (1 sat = 1 unit), CLN unchanged:
    evm-deposit approves and deposits; open, pay, mutual close; both nodes
    withdraw everything to their exit addresses, then evm-withdraw. Every
    amount on chain is exact in token units, the contract ends holding
    nothing, and gas is paid in ETH only."""
    assert chain.token is not None and chain.units == 1
    l1, l2 = node_factory.get_nodes(2)
    nodes = (l1, l2)
    minted = {n.eth_address: chain.token_balance(n.eth_address) for n in nodes}
    s = l1.evm_status()
    assert s['token'] == chain.token and s['units_per_sat'] == 1
    assert s['token_balance_units'] == minted[l1.eth_address] and s['withdrawable_units'] == 0

    txid, _ = l1.deposit(1_000_000)
    assert chain.event(txid).tx.outputs[0][0] == 1_000_000
    assert chain.token_balance(chain.contract) == 1_000_000
    assert chain.token_balance(l1.eth_address) == minted[l1.eth_address] - 1_000_000
    res, _ = open_channel(chain, l1, l2, 600_000)
    l1.rpc.pay(l2.rpc.invoice(150_000_000, 't', 't')['bolt11'])
    wait_for(lambda: l2.channel(l1)['to_us_msat'] == 150_000_000)
    ours = l1.channel(l2)['to_us_msat'] // 1000

    close = l1.rpc.close(l2.info['id'])
    assert close['type'] == 'mutual'
    ev = chain.wait_event(close['txids'][0])
    assert ev.spends() == [(res['txid'], res['outnum'])]
    fee = chain.fee_of(ev.txid)
    assert sorted(a for a, _ in ev.tx.outputs) == sorted([150_000, ours - fee])
    for n in nodes:
        wait_for(lambda: any(o['txid'] == ev.txid and o['status'] == 'confirmed'
                             for o in n.rpc.listfunds()['outputs']))
    wait_forgotten(chain, l1, l2)

    # Everything each wallet holds goes to its own exit address.
    for n in nodes:
        addr = n.rpc.call('evm-exitaddr', {'ethaddress': n.eth_address})['address']
        w = n.rpc.withdraw(addr, 'all')
        chain.wait_event(w['txid'])
        wait_for(lambda: n.rpc.listfunds()['outputs'] == [])

    # What the contract owes each key: its exits, and the fee of every tx it
    # submitted (SPEC 4.4), in units.
    owed = {n.eth_address: 0 for n in nodes}
    for e in chain.events():
        if e.is_deposit:
            continue
        sender = evm.checksum_address(chain.rpc.request('eth_getTransactionByHash', e.evm_tx)['from'])
        owed[sender] = owed.get(sender, 0) + chain.fee_of(e.txid)
        for amount, spk in e.tx.outputs:
            if len(spk) == 22 and spk[:2] == b'\x60\x14':
                to = evm.checksum_address(spk[2:])
                owed[to] = owed.get(to, 0) + amount
    assert set(owed) == {n.eth_address for n in nodes}
    assert sum(owed.values()) == 1_000_000
    for x, sats in owed.items():
        assert chain.withdrawable(x) == sats * chain.units
    assert chain.token_balance(chain.contract) == 1_000_000

    for n in nodes:
        eth0 = chain.balance(n.eth_address)
        tok0 = chain.token_balance(n.eth_address)
        assert n.evm_status()['withdrawable_units'] == owed[n.eth_address]
        wd = n.rpc.call('evm-withdraw')
        assert wd['status'] == 'mined' and wd['amount_units'] == owed[n.eth_address] and wd['token'] == chain.token
        rc = chain.rpc.request('eth_getTransactionReceipt', wd['evm_tx'])
        assert chain.token_balance(n.eth_address) - tok0 == owed[n.eth_address]
        assert eth0 - chain.balance(n.eth_address) == int(rc['gasUsed'], 16) * int(rc['effectiveGasPrice'], 16)
        assert chain.withdrawable(n.eth_address) == 0
    assert chain.token_balance(chain.contract) == 0
    # Tokens conserved: l2 ends with l1's payment, less what its txs paid in
    # fees to others; together they hold exactly what was minted.
    final = {n.eth_address: chain.token_balance(n.eth_address) for n in nodes}
    assert sum(final.values()) == sum(minted.values())
    assert final[l2.eth_address] - minted[l2.eth_address] == owed[l2.eth_address]


# ------------------------------------------------------------ payment size (SPEC open question 1)

def test_max_payment(node_factory, chain):
    """Regtest's max_payment (2^32 - 1 msat) caps a single HTLC only when
    option_support_large_channel isn't negotiated, which CLN does by default."""
    l1, l2 = node_factory.get_nodes(2)
    l3 = node_factory.get_node(options={'dev-force-features': ['-63', '-19']})
    l1.deposit(40_000_000)
    big = 5_000_000_000      # msat, above 2^32
    open_channel(chain, l1, l2, 20_000_000)
    assert l1.channel(l2)['spendable_msat'] > 2**32
    inv = l2.rpc.invoice(big, 'big', 'big')
    send_direct(l1, l2, big, inv['payment_hash'], inv['payment_secret'], 50)
    r = l1.rpc.waitsendpay(inv['payment_hash'])
    assert r['status'] == 'complete' and r['amount_msat'] == big
    assert only_one(l1.rpc.listsendpays(payment_hash=inv['payment_hash'])['payments'])['amount_msat'] == big
    wait_for(lambda: l2.channel(l1)['to_us_msat'] == big)

    # Without large channels (l3 drops feature 19): capped at 2^32 - 1.
    open_channel(chain, l1, l3, 16_000_000)
    assert l1.channel(l3)['spendable_msat'] == 2**32 - 1
    inv = l3.rpc.invoice(big, 'capped', 'capped')
    with pytest.raises(RpcError) as err:
        send_direct(l1, l3, big, inv['payment_hash'], inv['payment_secret'], 50)
        l1.rpc.waitsendpay(inv['payment_hash'])
    assert 'maximum value exceeded' in str(err.value), err.value
    assert l3.rpc.listinvoices('capped')['invoices'][0]['status'] == 'unpaid'
    # ... and 2^32 - 1 itself goes through.
    inv = l3.rpc.invoice(2**32 - 1, 'cap', 'cap')
    send_direct(l1, l3, 2**32 - 1, inv['payment_hash'], inv['payment_secret'], 50)
    assert l1.rpc.waitsendpay(inv['payment_hash'])['status'] == 'complete'


# ------------------------------------------------------------ restarts, reorgs, outages

@pytest.mark.parametrize('htlc', [False, True])
def test_restart_mid_close(node_factory, chain, htlc):
    """Both nodes restart while a force close is being resolved (with an
    HTLC: after its HTLC-timeout tx, so onchaind replays HTLC spends). If
    CLN happened to take the to_local sweep as that tx's fee input, it
    may hit REPLAY_BUG: then (and only with its exact symptom) it xfails."""
    l1, l2 = node_factory.get_nodes(2, opts=[{'may_reconnect': True},
                                             {'may_reconnect': True, 'plugin': HOLD, 'hold-all': False}])
    l1.deposit(1_000_000)
    res, _ = open_channel(chain, l1, l2, 600_000)
    l1.rpc.pay(l2.rpc.invoice(100_000_000, 'r', 'r')['bolt11'])
    wait_for(lambda: l2.channel(l1)['to_us_msat'] == 100_000_000)
    if htlc:
        phash = hashlib.sha256(os.urandom(32)).hexdigest()
        l2.rpc.call('hold-add', {'payment_hash': phash})
        send_direct(l1, l2, 40_000_000, phash, '11' * 32, 45)
        htlcs_held(l1, l2, 1)
        assert l2.rpc.call('hold-list')['held'] == [phash]
    disconnect(l1, l2)
    commit = chain.wait_event(l1.rpc.close(l2.info['id'], unilateraltimeout=1)['txids'][0])
    wait_for(lambda: l1.state(l2) == 'ONCHAIN' and l2.state(l1) == 'ONCHAIN')
    chain.mine(5)
    l1.restart()
    l2.restart()
    tx, mark = None, None
    try:
        if htlc:
            tx = wait_resolved(chain, l1, 'OUR_UNILATERAL', 'OUR_HTLC', 'OUR_HTLC_TIMEOUT_TX')
            chain.mine(3)
            l1.restart()         # onchaind replays the HTLC-timeout spend
            mark = log_mark(l1)
            second = wait_resolved(chain, l1, 'OUR_HTLC_TIMEOUT_TX', 'DELAYED_OUTPUT_TO_US',
                                   'OUR_DELAYED_RETURN_TO_WALLET')
            assert second.spends()[0][0] == tx.txid
        sweep = wait_resolved(chain, l1, 'OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US', 'OUR_DELAYED_RETURN_TO_WALLET')
        assert sweep.spends()[0][0] == commit.txid
        chain.mine(3)
        l1.restart()             # and once more with the sweep on chain
        wait_for(lambda: ends_in_wallet(chain, l1, sweep.txid))
        if htlc:
            wait_for(lambda: only_one(l1.rpc.listsendpays(payment_hash=phash)['payments'])['status'] == 'failed')
        wait_forgotten(chain, l1, l2)
    except (AssertionError, ValueError):
        if tx is None or mark is None or not spends_to_local_sweep(chain, commit, tx):
            raise
        check_replay_bug(chain, l1, tx, mark)
        pytest.xfail(REPLAY_BUG)
    # The replays sent nothing that the contract refused on chain, and left
    # nothing behind.
    for n in (l1, l2):
        assert not n.daemon.is_in_log('reverted on chain')
        assert n.evm_status()['pending_sends'] == []


@pytest.mark.parametrize('restart', [False, pytest.param(True, marks=pytest.mark.xfail(
    reason=REPLAY_BUG, strict=True, raises=AssertionError))])
def test_restart_replay_two_spends(node_factory, chain, restart):
    """Minimal reproduction of REPLAY_BUG: l1's HTLC-timeout tx takes its
    own to_local sweep output as fee input, so it spends two outputs
    onchaind watches (onchaind cares about the HTLC one, not the sweep's).
    Without a restart onchaind finishes; restarted after the HTLC-timeout
    output's sweep, its replay never sees that sweep, it keeps re-sending it
    ("Expected error broadcasting tx"), and never forgets the channel."""
    l1, l2 = node_factory.get_nodes(2, opts=[{'may_reconnect': True},
                                             {'may_reconnect': True, 'plugin': HOLD, 'hold-all': False}])
    l1.deposit(700_000)
    res, _ = open_channel(chain, l1, l2, 'all')
    # Left: only the 25,000 sat emergency reserve CLN keeps for anchors.
    # Reserved here, so the to_local sweep is the only coin for the
    # HTLC-timeout tx's fee.
    reserve = only_one(l1.rpc.listfunds()['outputs'])
    assert reserve['amount_msat'] == 25_000_000
    l1.rpc.reserveinputs(psbt_spending(bytes.fromhex(reserve['scriptpubkey']), 25_000,
                                       (btc.internal(reserve['txid']), reserve['output'])), reserve=10_000)
    assert only_one(l1.rpc.listfunds()['outputs'])['reserved']
    l1.rpc.pay(l2.rpc.invoice(100_000_000, 'r', 'r')['bolt11'])
    phash = hashlib.sha256(os.urandom(32)).hexdigest()
    l2.rpc.call('hold-add', {'payment_hash': phash})
    send_direct(l1, l2, 40_000_000, phash, '11' * 32, 90)    # expires after the to_local delay (40)
    htlcs_held(l1, l2, 1)
    disconnect(l1, l2)
    commit = chain.wait_event(l1.rpc.close(l2.info['id'], unilateraltimeout=1)['txids'][0])
    sweep = wait_resolved(chain, l1, 'OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US', 'OUR_DELAYED_RETURN_TO_WALLET')
    tx = wait_resolved(chain, l1, 'OUR_UNILATERAL', 'OUR_HTLC', 'OUR_HTLC_TIMEOUT_TX')
    # The precondition (pytest.fail, not an assert: the xfail expects only
    # the final wait to fail): the HTLC-timeout tx spends the HTLC and the sweep.
    if not (len(tx.spends()) == 2 and (sweep.txid, 0) in tx.spends() and tx.spends()[0][0] == commit.txid):
        pytest.fail('the HTLC-timeout tx does not spend the to_local sweep: {}'.format(tx.spends()))
    second = wait_resolved(chain, l1, 'OUR_HTLC_TIMEOUT_TX', 'DELAYED_OUTPUT_TO_US', 'OUR_DELAYED_RETURN_TO_WALLET')
    chain.mine(3)
    mark = None
    if restart:
        l1.restart()
        mark = log_mark(l1)
    try:
        wait_forgotten(chain, l1)
    except (AssertionError, ValueError):
        # The xfail stands only for this symptom (pytest.fail otherwise).
        if mark is None:
            pytest.fail('onchaind did not finish without a restart')
        check_replay_bug(chain, l1, tx, mark)
        raise AssertionError(REPLAY_BUG)
    assert ends_in_wallet(chain, l1, second.txid)


@pytest.mark.parametrize('depth', ['above-safe', 'through-safe'])
def test_reorg(node_factory, chain, depth):
    """anvil reorganises its last blocks: only above `safe` (the plugin had
    ingested our own submit's receipt), or deeper (blocks the plugin had
    served change). The new branch drops l1's withdraw and holds another
    deposit instead. Served blocks follow the new chain; CLN neither
    crashes nor loses the channel; l1's plugin sends the withdraw again (as
    a mempool would) and its key isn't wedged by the nonce the reorg took
    back."""
    l1, l2 = node_factory.get_nodes(2, opts={'may_reconnect': True})
    l1.deposit(1_000_000)
    res, _ = open_channel(chain, l1, l2, 400_000)
    l1.rpc.pay(l2.rpc.invoice(20_000_000, 'r1', 'r1')['bolt11'])
    addr = l2.rpc.newaddr('p2tr')['p2tr']
    spk = btc.address_script(addr)
    with chain.paused():
        chain.mine(2)
        base = int(chain.latest()['number'], 16)
        w = l1.rpc.withdraw(addr, 70_000)
        old = chain.wait_event(w['txid'])
        vout = only_one([i for i, (_, s) in enumerate(old.tx.outputs) if s == spk])
        # The plugin ingested its own receipt at once (SPEC 5.2).
        wait_for(lambda: l1.rpc.call('getutxout', {'txid': w['txid'], 'vout': vout})['amount'] == 70_000)
        served = {}
        if depth == 'through-safe':
            chain.mine(3)
            wait_for(lambda: wallet_has(l2, w['txid'], vout))
            seen = chain.wait_synced()
            served = {h: l1.rpc.call('getrawblockbyheight', {'height': h})['blockhash']
                      for h in range(old.height, seen + 1)}
            assert served == {h: x for h, x in chain.reference_blocks(seen).items() if h >= old.height}
        else:
            assert old.block == int(chain.latest()['number'], 16)    # above `safe`
            assert old.height > chain.expected_tip()                  # never served
        n = int(chain.latest()['number'], 16) - base
        dep = {'from': chain.deployer.address, 'to': chain.contract, 'value': evm.qty(55_000 * SAT),
               'data': '0x' + evm.calldata('deposit(bytes)', ['bytes'], [spk]).hex(), 'gas': evm.qty(1_000_000)}
        assert chain.rpc.call('anvil_reorg', n, [[dep, n - 1]]) is None
        branch = [e for e in chain.events() if base < e.block <= base + n]
        assert w['txid'] not in [e.txid for e in branch]
        new = only_one(branch)
        assert new.is_deposit and new.tx.outputs == [(55_000, spk)]
        chain.mine(3)
    # CLN follows the new chain: the deposit is l2's.
    wait_for(lambda: wallet_has(l2, new.txid, 0))
    # The withdraw comes back: l1's plugin re-sent it, in a block after the
    # reorg, and CLN sees it at its new height.
    again = chain.wait_event(w['txid'])
    # Re-signed by l1's plugin (new fees, new EVM tx), not replayed by anvil.
    assert again.block > base + n and again.raw == old.raw and again.evm_tx != old.evm_tx
    assert l1.daemon.is_in_log('a reorg took back nonces')

    def w_heights():
        return [o['blockheight'] for o in l2.rpc.listfunds()['outputs']
                if o['txid'] == w['txid'] and o['output'] == vout and o['status'] == 'confirmed']
    # The height itself (the pre-reorg output would satisfy a wallet check).
    wait_for(lambda: w_heights() == [again.height])
    assert wallet_has(l2, w['txid'], vout)
    tip = chain.wait_synced()
    ref = chain.reference_blocks(tip)
    for h in range(old.height - 2, tip + 1):
        assert l1.rpc.call('getrawblockbyheight', {'height': h})['blockhash'] == ref[h]
        assert l2.rpc.call('getrawblockbyheight', {'height': h})['blockhash'] == ref[h]
    if served:
        # Blocks CLN had been served were replaced: it rolled back.
        assert any(ref[h] != x for h, x in served.items())
    # A later tx only l1 sends gets mined.
    w2 = l1.rpc.withdraw(l2.rpc.newaddr('p2tr')['p2tr'], 30_000)
    chain.wait_event(w2['txid'])
    # The channel is fine: it pays, and closes on chain.
    assert both_normal(l1, l2)
    l1.rpc.pay(l2.rpc.invoice(10_000_000, 'r2', 'r2')['bolt11'])
    close = l1.rpc.close(l2.info['id'])
    assert chain.wait_event(close['txids'][0]).spends() == [(res['txid'], res['outnum'])]
    wait_forgotten(chain, l1, l2)


@pytest.mark.parametrize('chain', [{'keep_state': True, 'wallclock': True}], indirect=True)
def test_outage(node_factory, chain):
    """anvil stops for longer than L (and the to_local delay of a force
    close runs out meanwhile, heights following the wall clock), then
    restarts: the plugin alarms, recovers, and the sweep isn't lost."""
    alarms = [ALARM_TAG, ALARM_LAG, ALARM_SYNC]
    l1, l2 = node_factory.get_nodes(2, opts={'broken_log': alarms})
    l1.deposit(1_000_000)
    open_channel(chain, l1, l2, 500_000)
    l1.rpc.pay(l2.rpc.invoice(50_000_000, 'o', 'o')['bolt11'])
    disconnect(l1, l2)
    commit = chain.wait_event(l1.rpc.close(l2.info['id'], unilateraltimeout=1)['txids'][0])
    # Wait (in real time: heights keep the wall clock) until the CSV has
    # about 15 blocks to go, then stop anvil for 50 s (25 blocks).
    wait_for(lambda: chain.height() >= commit.height + 25, timeout=120)
    assert not resolutions(l1, 'OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US')

    def mark(n):
        n.daemon.logs_catchup()
        return len(n.daemon.logs)
    assert not any(n.daemon.is_in_log(r'alarm') for n in (l1, l2))
    with chain.paused():
        chain.stop_anvil()
        down = time.time()
        for n in (l1, l2):
            for alarm in alarms:
                wait_for(lambda: n.daemon.is_in_log(alarm), timeout=40)
        time.sleep(max(0, 50 - (time.time() - down)))
        assert l1.running() and l2.running()
        up = {n: mark(n) for n in (l1, l2)}
        chain.start_anvil()
        chain.mine_wallclock()
    assert chain.height() >= commit.height + 40
    for n in (l1, l2):
        for name in ('lag', 'tag', 'sync'):
            wait_for(lambda: n.daemon.is_in_log('alarm cleared: ' + name, start=up[n]))
    sweep = wait_resolved(chain, l1, 'OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US', 'OUR_DELAYED_RETURN_TO_WALLET')
    assert sweep.spends()[0][0] == commit.txid
    wait_for(lambda: ends_in_wallet(chain, l1, sweep.txid))
    assert l1.evm_status()['alarms'] == {} and l2.evm_status()['alarms'] == {}
    # Every alarm was raised while anvil was down, and none since.
    for n in (l1, l2):
        n.daemon.logs_catchup()
        raised = [i for i, line in enumerate(n.daemon.logs) if 'evmbackend.py: alarm: ' in line]
        assert raised and all(i < up[n] for i in raised), raised
        assert not any('**BROKEN**' in line for line in n.daemon.logs[up[n]:])
