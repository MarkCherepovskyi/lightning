#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Force closes on Base Sepolia with the real SPEC 5.6 delays (to_self_delay
1440 blocks = 24 h, cltv 144 blocks = 2.4 h): four unmodified lightningd on
evmbackend.py, each with the spike's hold plugin so HTLCs stay in flight.

    A -> B  A (the payer) force-closes with two HTLCs A -> B in flight:
            B claims one with the preimage from A's commitment, A times the
            other out with an HTLC-timeout tx after its expiry, then sweeps
            that tx's output and its own to_local 24 h later; B's to_remote
            is in its wallet at once.
    C -> D  D (the payee) force-closes with two HTLCs C -> D: D's
            HTLC-success tx, then its sweep 24 h later; C times the other out
            directly from D's commitment; D's to_local 24 h later.
    B -> C  C broadcasts a revoked commitment with HTLCs both ways (signed
            with dev-sign-last-tx); B penalises to_local and both HTLCs.

Every resolution is checked against the chain (the tx is in a node's
proven index, spends what it should, at a height its timelock allows) and
the contract's isUnspent. When every output is resolved and each onchaind
has forgotten its channel (100 blocks later), the coins exit, are withdrawn
and the ETH goes back to the deployer. About 28 hours in all.

    INFURA_API_KEY=... DEPLOYER_PRIVATE_KEY=0x... \\
    PATH=<venv>/bin:$PATH PYTHONPATH=<tree>/contrib/pyln-client:<tree>/contrib/pyln-proto \\
        caffeinate -ims python3 smoke_forceclose.py --contract 0x... [--workdir /tmp/evf] [--resume]

--resume restarts the nodes of an interrupted run (state.json in the
workdir) and goes on waiting. A run that fails keeps its nodes' ETH: free it
with `smoke_base_sepolia.py --cleanup <workdir> --contract 0x...`."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import btc   # noqa: E402
import evm   # noqa: E402
from smoke_base_sepolia import (CHAIN_ID, FREE_TIER_PER_DAY, INTERVAL, RPC_TEMPLATE, Node, credits,   # noqa: E402
                                exit_coins, log, refund, wait_for)

HOLD = os.path.join(HERE, 'spikes', 's1-toychain', 'hold.py')
NAMES = ('A', 'B', 'C', 'D')
FUND_WEI = 17 * 10**15           # evm-min-balance (0.01) + gas + the deposit
DEPOSIT_SAT = 500_000
CHANNEL_SAT = 300_000
PAID_SAT = 100_000               # opener -> peer before the HTLCs, so both have a to_local/to_remote
HTLC_MSAT = 50_000_000
TO_SELF_DELAY = 1440             # SPEC 5.6: 24 h at INTERVAL 60
TIMEOUT_DELAY = 150              # the timed-out HTLCs' CLTV: 2.5 h
SUCCESS_DELAY = 200              # above cltv-final (144), for the payee's invoice check
ANCHOR_SAT = 330
RESOLVED = re.compile(r'Resolved (\w+)/(\w+) by our proposal (\w+) \(([0-9a-f]{64})\)')
FORGOTTEN = 'onchaind complete, forgetting peer'
CHEAT_OK = 'did *we* cheat?'     # the cheater's onchaind, on its own revoked commitment
IS_UNSPENT = 'isUnspent(bytes32,uint32,uint64,bytes,uint32)'

# (name, node, txtype, outtype, proposal, from): `from` is the tx whose
# output is resolved; a 'delay' means its height + to_self_delay.
EXPECT = [
    ('AB: B claims HTLC 1 with the preimage', 'B', 'THEIR_UNILATERAL', 'THEIR_HTLC', 'THEIR_HTLC_FULFILL_TO_US'),
    ('AB: A times out HTLC 2 (HTLC-timeout tx)', 'A', 'OUR_UNILATERAL', 'OUR_HTLC', 'OUR_HTLC_TIMEOUT_TX'),
    ('AB: A sweeps the HTLC-timeout output', 'A', 'OUR_HTLC_TIMEOUT_TX', 'DELAYED_OUTPUT_TO_US',
     'OUR_DELAYED_RETURN_TO_WALLET'),
    ('AB: A sweeps its to_local', 'A', 'OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US', 'OUR_DELAYED_RETURN_TO_WALLET'),
    ('CD: D claims HTLC 1 (HTLC-success tx)', 'D', 'OUR_UNILATERAL', 'THEIR_HTLC', 'OUR_HTLC_SUCCESS_TX'),
    ('CD: D sweeps the HTLC-success output', 'D', 'OUR_HTLC_SUCCESS_TX', 'DELAYED_OUTPUT_TO_US',
     'OUR_DELAYED_RETURN_TO_WALLET'),
    ('CD: D sweeps its to_local', 'D', 'OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US', 'OUR_DELAYED_RETURN_TO_WALLET'),
    ('CD: C times out HTLC 2 from D\'s commitment', 'C', 'THEIR_UNILATERAL', 'OUR_HTLC', 'OUR_HTLC_TIMEOUT_TO_US'),
    ('BC: B penalises C\'s to_local', 'B', 'THEIR_REVOKED_UNILATERAL', 'DELAYED_CHEAT_OUTPUT_TO_THEM',
     'OUR_PENALTY_TX'),
    ('BC: B penalises the HTLC C offered', 'B', 'THEIR_REVOKED_UNILATERAL', 'THEIR_HTLC', 'OUR_PENALTY_TX'),
    ('BC: B penalises the HTLC B offered', 'B', 'THEIR_REVOKED_UNILATERAL', 'OUR_HTLC', 'OUR_PENALTY_TX'),
]


class Index:
    """A node's proven index, read-only: what the contract applied, at which
    virtual height. txids in display order."""
    def __init__(self, node):
        self.path = os.path.join(node.dir, 'regtest', 'evmchan.sqlite3')

    def _q(self, sql, args):
        db = sqlite3.connect('file:{}?mode=ro'.format(self.path), uri=True, timeout=30)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def tx(self, txid):
        """(height, Tx) or None."""
        rows = self._q('SELECT height, raw FROM txs WHERE txid = ? AND bad = 0', (btc.internal(txid),))
        return (rows[0][0], btc.Tx.parse(bytes(rows[0][1]))) if rows else None

    def output(self, txid, vout):
        rows = self._q('SELECT amount, script, height FROM outputs WHERE txid = ? AND vout = ?',
                       (btc.internal(txid), vout))
        return (rows[0][0], bytes(rows[0][1]), rows[0][2]) if rows else None

    def spender(self, txid, vout):
        rows = self._q('SELECT t.txid FROM spends s JOIN txs t ON t.id = s.tx_id '
                       'WHERE s.prev_txid = ? AND s.prev_vout = ? AND t.bad = 0', (btc.internal(txid), vout))
        return btc.display(bytes(rows[0][0])) if rows else None


def spends(tx):
    return [(btc.display(i.prev_txid), i.vout) for i in tx.inputs]


def resolutions(node):
    return [m.groups() for m in RESOLVED.finditer(node.log_text())]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--contract', required=True)
    ap.add_argument('--workdir', default='/tmp/evf')
    ap.add_argument('--resume', action='store_true')
    ap.add_argument('--lightningd', default=os.path.join(HERE, '../../lightningd/lightningd'))
    args = ap.parse_args()
    key, dkey = os.environ.get('INFURA_API_KEY'), os.environ.get('DEPLOYER_PRIVATE_KEY')
    if not key or not dkey:
        sys.exit('INFURA_API_KEY and DEPLOYER_PRIVATE_KEY must be set')
    rpc = evm.EvmRpc(RPC_TEMPLATE.replace('${INFURA_API_KEY}', key))
    if rpc.chain_id() != CHAIN_ID:
        sys.exit('not Base Sepolia')
    deployer = evm.Account(bytes.fromhex(dkey[2:] if dkey.startswith('0x') else dkey))
    contract = evm.checksum_address(args.contract)
    state_path = os.path.join(args.workdir, 'state.json')
    if args.resume:
        with open(state_path) as f:
            state = json.load(f)
    else:
        shutil.rmtree(args.workdir, ignore_errors=True)
        os.makedirs(args.workdir)
        state = {'contract': contract, 'steps': [], 'resolved': {}, 'started': time.time()}

    def save():
        with open(state_path + '.new', 'w') as f:
            json.dump(state, f, indent=1)
        os.replace(state_path + '.new', state_path)

    t_last = [time.time()]

    def step(name, ok=True, **extra):
        now = time.time()
        state['steps'].append(dict(name=name, ok=bool(ok), at=round(now), seconds=round(now - t_last[0], 1), **extra))
        log('{} {:8.1f}s  {}'.format('PASS' if ok else 'FAIL', now - t_last[0], name))
        t_last[0] = now
        save()
        if not ok:
            raise AssertionError(name)

    nodes = {}
    try:
        for name in NAMES:
            n = Node(name, args.workdir, args.lightningd, contract)
            n.args += ['--plugin=' + HOLD, '--hold-all=false']
            nodes[name] = n
        a, b, c, d = (nodes[x] for x in NAMES)
        idx = Index(b)                   # B sees all three channels' txs; any node's index would do
        if not args.resume:
            setup(rpc, deployer, nodes, state, step)
        else:
            for n in nodes.values():
                n.start()
            log('resumed: nodes restarted')
        watch(rpc, contract, nodes, idx, state, step, save)
        state['passed'] = True
        save()
    finally:
        for n in nodes.values():
            n.stop()
        if nodes and state.get('passed'):
            refund(rpc, list(nodes.values()), deployer.address)
        elif nodes:
            log('not finished: resume with --resume, or free the coins with smoke_base_sepolia.py '
                '--cleanup {} --contract {}'.format(args.workdir, contract))
        state['total_seconds'] = round(time.time() - state['started'])
        save()
        log('state in ' + state_path)


def setup(rpc, deployer, nodes, state, step):
    a, b, c, d = (nodes[x] for x in NAMES)
    for n in nodes.values():
        rc = rpc.send(deployer, n.eth, value=FUND_WEI, timeout=180)
        assert int(rc['status'], 16) == 1
    for n in nodes.values():
        n.start()
    wait_for(lambda: all(n.status()['opens_refused'] is None for n in nodes.values()), 1800, 'opens allowed')
    step('four nodes start with the public 5.6 options (to_self_delay 1440, cltv 144)')

    for n in nodes.values():
        dep = n.rpc.call('evm-deposit', {'amount': DEPOSIT_SAT})
        assert dep['status'] == 'mined', dep
    wait_for(lambda: all(n.confirmed_sat() >= DEPOSIT_SAT for n in nodes.values()), 900, 'deposits')
    step('deposits of {:,} sat each confirmed (wallet funds for HTLC txs and sweeps)'.format(DEPOSIT_SAT))

    hops = [(a, b), (c, d), (b, c)]
    errors = []

    def open_one(x, y):
        try:
            x.rpc.connect(y.info['id'], '127.0.0.1', y.port)
            res = x.rpc.fundchannel(y.info['id'], CHANNEL_SAT, announce=False)
            state[x.name + y.name] = {'funding': [res['txid'], res['outnum']]}
        except Exception as e:     # noqa: B902
            errors.append('{}{}: {}'.format(x.name, y.name, e))
    threads = [threading.Thread(target=open_one, args=h) for h in hops]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    wait_for(lambda: all(x.chan(y)['state'] == 'CHANNELD_NORMAL' and y.chan(x)['state'] == 'CHANNELD_NORMAL'
                         for x, y in hops), 1800, 'channels normal')
    step('channels A-B, C-D, B-C normal ({:,} sat each)'.format(CHANNEL_SAT))

    for x, y in hops:
        r = x.rpc.pay(y.rpc.invoice(PAID_SAT * 1000, 'paid' + x.name + y.name, 'p')['bolt11'])
        assert r['status'] == 'complete'
        wait_for(lambda: y.chan(x)['to_us_msat'] == PAID_SAT * 1000, 120, 'paid')
    step('{:,} sat paid over each channel'.format(PAID_SAT))

    # Two HTLCs in flight on A-B and C-D: one the payee can claim, one nobody can.
    for x, y in ((a, b), (c, d)):
        k = x.name + y.name
        inv = y.rpc.invoice(HTLC_MSAT, 'htlc' + k, 'h')
        dead = hashlib.sha256(os.urandom(32)).hexdigest()
        state[k].update(success_hash=inv['payment_hash'], timeout_hash=dead)
        for h in (inv['payment_hash'], dead):
            y.rpc.call('hold-add', {'payment_hash': h})
        send_direct(x, y, HTLC_MSAT, inv['payment_hash'], inv['payment_secret'], SUCCESS_DELAY)
        send_direct(x, y, HTLC_MSAT + 1000, dead, '11' * 32, TIMEOUT_DELAY)
    for x, y in ((a, b), (c, d)):
        htlcs_held(x, y, 2)
        hs = x.chan(y)['htlcs']
        state[x.name + y.name]['expiries'] = {h['payment_hash']: h['expiry'] for h in hs}
    step('two HTLCs held on A-B and on C-D (expiries {})'.format(
        {k: sorted(state[k]['expiries'].values()) for k in ('AB', 'CD')}))

    # B-C: a commitment with HTLCs both ways, which C keeps, then revoked.
    to_c = c.rpc.invoice(60_000_000, 'to-c', 'p')
    to_b = b.rpc.invoice(70_000_000, 'to-b', 'p')
    c.rpc.call('hold-add', {'payment_hash': to_c['payment_hash']})
    b.rpc.call('hold-add', {'payment_hash': to_b['payment_hash']})
    send_direct(b, c, 60_000_000, to_c['payment_hash'], to_c['payment_secret'], SUCCESS_DELAY)
    send_direct(c, b, 70_000_000, to_b['payment_hash'], to_b['payment_secret'], SUCCESS_DELAY)
    htlcs_held(b, c, 2)
    revoked = c.rpc.call('dev-sign-last-tx', {'id': b.info['id']})['tx']
    rtx = btc.Tx.parse(bytes.fromhex(revoked))
    assert len(rtx.outputs) == 6, rtx.outputs     # 2 anchors, to_local, to_remote, 2 HTLCs
    c.rpc.call('hold-release', {'payment_hash': to_c['payment_hash']})
    b.rpc.call('hold-release', {'payment_hash': to_b['payment_hash']})
    wait_for(lambda: not b.chan(c)['htlcs'] and not c.chan(b)['htlcs'], 300, 'B-C HTLCs settled')
    b.rpc.pay(c.rpc.invoice(10_000_000, 'more', 'more')['bolt11'])
    wait_for(lambda: not b.chan(c)['htlcs'] and not c.chan(b)['htlcs'], 300, 'B-C settled again')
    state['BC']['revoked'] = rtx.txid_hex()
    step('C holds a revoked B-C commitment with 6 outputs ({})'.format(rtx.txid_hex()))

    # The closes.
    for closer, peer, k in ((a, b, 'AB'), (d, c, 'CD')):
        closer.rpc.disconnect(peer.info['id'], force=True)
        r = closer.rpc.close(peer.info['id'], unilateraltimeout=1)
        assert r['type'] == 'unilateral', r
        state[k]['commit'] = r['txids'][0]
    ok = c.rpc.call('sendrawtransaction', {'tx': revoked, 'allowhighfees': False})
    assert ok['success'], ok
    state['closed_at'] = time.time()
    step('A force-closes A-B, D force-closes C-D, C broadcasts the revoked B-C commitment')

    wait_for(lambda: all(x.chan(y)['state'] == 'ONCHAIN' and y.chan(x)['state'] == 'ONCHAIN'
                         for x, y in ((a, b), (c, d), (b, c))), 1800, 'all three ONCHAIN')
    for payee, k in ((b, 'AB'), (d, 'CD')):
        rel = payee.rpc.call('hold-release', {'payment_hash': state[k]['success_hash']})['released']
        assert rel == [state[k]['success_hash']], rel
    step('all three channels ONCHAIN; B and D release HTLC 1 (they now know its preimage)')


def watch(rpc, contract, nodes, idx, state, step, save):
    a, b, c, d = (nodes[x] for x in NAMES)
    commits = {'AB': state['AB']['commit'], 'CD': state['CD']['commit'], 'BC': state['BC']['revoked']}
    if 'heights' not in state:
        wait_for(lambda: all(idx.tx(t) for t in commits.values()), 1800, 'commitments indexed', poll=15)
        state['heights'] = {}
        for k, t in commits.items():
            h, tx = idx.tx(t)
            fund = tuple(state[k]['funding'])
            assert spends(tx) == [fund], (k, spends(tx), fund)
            assert idx.spender(*fund) == t
            assert not is_unspent(rpc, contract, idx, *fund)
            state['heights'][k] = h
        anchors = {k: sorted(x for x, _ in idx.tx(t)[1].outputs)[:2] for k, t in commits.items()}
        step('the three commitments are applied at heights {}, each spending its funding output '
             '(isUnspent false); anchors {}'.format(state['heights'], anchors),
             all(v == [ANCHOR_SAT, ANCHOR_SAT] for v in anchors.values()))

    def remote_in_wallet(n, commit):
        return [o for o in n.rpc.listfunds()['outputs'] if o['txid'] == commit and o['status'] == 'confirmed']
    for n, k in ((b, 'AB'), (c, 'CD')):
        if 'to_remote_' + k not in state:
            wait_for(lambda: remote_in_wallet(n, commits[k]), 1800, n.name + ' to_remote')
            o = remote_in_wallet(n, commits[k])[0]
            amount = idx.output(commits[k], o['output'])[0]
            state['to_remote_' + k] = amount
            step('{}: {}\'s to_remote ({:,} sat) is in its wallet, spendable at once'.format(
                k, n.name, amount), o['amount_msat'] == amount * 1000)

    pending = [e for e in EXPECT if e[0] not in state['resolved']]
    last_report = 0
    while pending:
        for e in list(pending):
            name, who, txtype, outtype, proposal = e
            found = [r for r in resolutions(nodes[who]) if r[:3] == (txtype, outtype, proposal)]
            if not found:
                continue
            txid = found[-1][3]
            got = idx.tx(txid)
            if got is None:
                continue                 # resolved by CLN, not yet in the proven index
            height, tx = got
            for t, v in check_resolution(name, txid, height, tx, state, commits, idx) or []:
                assert not is_unspent(rpc, contract, idx, t, v), (name, t, v)
            state['resolved'][name] = {'txid': txid, 'height': height, 'at': round(time.time()),
                                       'hours_after_close': round((time.time() - state['closed_at']) / 3600, 2)}
            step('{} at height {} ({:.1f} h after the closes): {}'.format(
                name, height, (time.time() - state['closed_at']) / 3600, txid))
            pending.remove(e)
        if not pending:
            break
        if time.time() - last_report > 1800:
            tip = b.rpc.getinfo()['blockheight']
            due = {k: h + TO_SELF_DELAY for k, h in state['heights'].items() if k != 'BC'}
            log('waiting for {} resolutions; tip {}; to_local due at {} (~{:.1f} h); HTLC expiries {}'.format(
                len(pending), tip, due, (min(due.values()) - tip) * INTERVAL / 3600,
                sorted(state['AB']['expiries'].values()) + sorted(state['CD']['expiries'].values())))
            for n in nodes.values():
                al = n.status().get('alarms')
                if al:
                    log('{} alarms: {}'.format(n.name, al))
            last_report = time.time()
        save()
        time.sleep(60)

    # Payment outcomes learnt from the chain.
    for payer, k in ((a, 'AB'), (c, 'CD')):
        def outcome(h):
            ps = payer.rpc.listsendpays(payment_hash=h)['payments']
            return ps[-1]['status'] if ps else None
        wait_for(lambda: outcome(state[k]['success_hash']) == 'complete'
                 and outcome(state[k]['timeout_hash']) == 'failed', 1800, k + ' payment outcomes')
        pay = payer.rpc.listsendpays(payment_hash=state[k]['success_hash'])['payments'][-1]
        ok = hashlib.sha256(bytes.fromhex(pay['payment_preimage'])).hexdigest() == state[k]['success_hash']
        step('{}: {} sees HTLC 1 complete (preimage from the chain) and HTLC 2 failed'.format(k, payer.name), ok)

    # The penalty took everything of the revoked commitment but the anchors.
    rev = idx.tx(commits['BC'])[1]
    outs = [i for i, (amt, _) in enumerate(rev.outputs) if amt != ANCHOR_SAT]
    to_b = [i for i in outs if idx.spender(commits['BC'], i) is None]
    penalised = {idx.spender(commits['BC'], i) for i in outs} - {None}
    b_outs = {(o['txid'], o['output']) for o in b.rpc.listfunds(spent=True)['outputs']}
    c_outs = {(o['txid'], o['output']) for o in c.rpc.listfunds()['outputs']}
    swept = [(t, v) for t in penalised for v in range(len(idx.tx(t)[1].outputs))]
    step('BC: of the revoked commitment, B holds its to_remote and every penalty output, C none of them',
         len(to_b) == 1 and len(penalised) >= 1 and all(o in b_outs for o in swept + [(commits['BC'], to_b[0])])
         and not (c_outs & set(swept)))

    if 'forgotten' not in state:
        try:
            wait_for(lambda: all(n.log_text().count(FORGOTTEN) >= cnt
                                 for n, cnt in ((a, 1), (b, 2), (c, 2), (d, 1))), 4 * 3600, 'onchainds done', poll=60)
            state['forgotten'] = True
            step('every onchaind resolved its channel to depth 100 and forgot it')
        except Exception:           # noqa: B902
            step('onchaind did not forget every channel within 4 h (not required for the exit)', True)
    broken = [(n.name, ln) for n in nodes.values() for ln in n.log_text().splitlines()
              if '**BROKEN**' in ln and not (n.name == 'C' and CHEAT_OK in ln)]
    state['broken'] = broken
    step('no BROKEN log lines (but C\'s expected "did *we* cheat?")', not broken, count=len(broken))

    exit_coins(list(nodes.values()))
    step('every wallet exits to its ETH address and withdraws')
    state['rpc'] = {}
    for n in nodes.values():
        st = n.status()
        hours = (time.time() - st['rpc_calls_since']) / 3600
        cr = credits(st['rpc_calls'])
        state['rpc'][n.name] = {'calls': st['rpc_calls'], 'credits': cr, 'per_day': round(cr / hours * 24)}
        log('{}: {:,} credits/day since its last start ({:.0%} of the free tier)'.format(
            n.name, round(cr / hours * 24), cr / hours * 24 / FREE_TIER_PER_DAY))


def check_resolution(name, txid, height, tx, state, commits, idx):
    """The tx spends the right commitment (or HTLC tx) at a height its
    timelock allows."""
    k = name[:2]
    commit = commits[k]
    ch = state['heights'][k]
    srcs = {t for t, _ in spends(tx)}
    if 'sweeps the HTLC' in name:
        first = state['resolved'][name.replace('sweeps the HTLC-timeout output', 'times out HTLC 2 (HTLC-timeout tx)')
                                  .replace('sweeps the HTLC-success output', 'claims HTLC 1 (HTLC-success tx)')]
        assert first['txid'] in srcs, (name, srcs)
        assert height - first['height'] >= TO_SELF_DELAY, (name, height, first)
        return
    assert commit in srcs, (name, srcs, commit)
    if 'sweeps its to_local' in name:
        assert height - ch >= TO_SELF_DELAY, (name, height, ch)
    elif 'times out' in name:
        expiry = state[k]['expiries'][state[k]['timeout_hash']]
        assert height > expiry, (name, height, expiry)
        if 'HTLC-timeout tx' in name:
            assert tx.locktime == expiry, (tx.locktime, expiry)
    elif 'claims' in name:
        assert height <= state[k]['expiries'][state[k]['success_hash']], (name, height)
    for t, v in spends(tx):
        if t == commit:
            assert idx.spender(t, v) == txid
    # Every commitment output it spends is spent for the contract too.
    return [(t, v) for t, v in spends(tx) if t == commit]


def is_unspent(rpc, contract, idx, txid, vout):
    amount, script, height = idx.output(txid, vout)
    data = evm.calldata(IS_UNSPENT, ['bytes32', 'uint32', 'uint64', 'bytes', 'uint32'],
                        [btc.internal(txid), vout, amount, script, height])
    for attempt in range(10):
        try:
            return evm.abi_decode(['bool'], rpc.eth_call(contract, data, 'latest'))[0]
        except evm.EvmError as e:
            if not e.rate_limited or attempt == 9:
                raise
            time.sleep(5 * (attempt + 1))    # Infura's per-second credit limit (HTTP 429)


def htlcs_held(a, b, n):
    held = ('SENT_ADD_ACK_REVOCATION', 'RCVD_ADD_ACK_REVOCATION')

    def ok():
        return all(len(x.chan(y)['htlcs']) == n and all(h['state'] in held for h in x.chan(y)['htlcs'])
                   for x, y in ((a, b), (b, a)))
    wait_for(ok, 300, 'HTLCs held')


def send_direct(payer, payee, msat, payment_hash, payment_secret, delay):
    scid = payer.chan(payee)['short_channel_id']
    route = [{'id': payee.info['id'], 'channel': scid, 'amount_msat': msat, 'delay': delay, 'style': 'tlv'}]
    payer.rpc.sendpay(route, payment_hash, payment_secret=payment_secret)


if __name__ == '__main__':
    main()
