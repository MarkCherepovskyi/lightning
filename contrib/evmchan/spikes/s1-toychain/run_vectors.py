#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Capture real CLN transactions of every kind on the toy chain, as test
vectors for the EVM contract (SPEC 4.3 and 7).

  PATH=<venv>/bin:$PATH python3 run_vectors.py [lightningd] [workdir]

Two unmodified regtest lightningd (A, B) on chaind.py.  Scenarios, in order:
  wallet: deposits, a withdraw spending P2TR and P2WPKH inputs, an exit withdraw;
  mutual closes started by the opener and by the peer, from both nodes;
  Q: B restored from an old backup (taken with an HTLC in flight) broadcasts
     its revoked commitment and an HTLC-success; A penalizes to_local and the
     HTLC-success output;
  P: B's revoked commitment (dev-sign-last-tx, HTLCs both ways) sent to the
     chain by us: A's penalties on to_local and both HTLC kinds;
  X: B force-closes under fee pressure (anchor CPFP) with four HTLCs: B's
     HTLC-success and HTLC-timeout and their sweeps, A's remote success and
     remote timeout.
After each force close A spends its to_remote output with withdraw.
Each applied tx is labelled with a "kind" (from the nodes' own logs and RPC
results), written to vectors/cln-regtest-txs-full.jsonl, then checked by
verify_vectors.py."""
from btc import Tx, address_script, segwit_address
from chaind import DEFAULT_FEES, Chain, serve
from pyln.client import LightningRpc

import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import verify_vectors

HERE = os.path.dirname(os.path.abspath(__file__))
LIGHTNINGD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '../../../../lightningd/lightningd')
WORK = sys.argv[2] if len(sys.argv) > 2 else '/tmp/evv1'
OUT = os.path.join(HERE, 'vectors', 'cln-regtest-txs-full.jsonl')
HIGH_FEES = [(b, 25000) for b, _ in DEFAULT_FEES]   # sat/kVB, for anchor CPFP

# onchaind's "Resolved <tx>/<output> by our proposal <proposal> (txid)" -> kind
RESOLUTIONS = {
    ('OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US'): 'to_local-sweep',
    ('OUR_UNILATERAL', 'THEIR_HTLC'): 'htlc-success',
    ('OUR_UNILATERAL', 'OUR_HTLC'): 'htlc-timeout',
    ('OUR_HTLC_SUCCESS_TX', 'DELAYED_OUTPUT_TO_US'): 'htlc-success-sweep',
    ('OUR_HTLC_TIMEOUT_TX', 'DELAYED_OUTPUT_TO_US'): 'htlc-timeout-sweep',
    ('THEIR_UNILATERAL', 'THEIR_HTLC'): 'remote-success',
    ('THEIR_UNILATERAL', 'OUR_HTLC'): 'remote-timeout',
    ('THEIR_REVOKED_UNILATERAL', 'DELAYED_CHEAT_OUTPUT_TO_THEM'): 'penalty-to_local',
    ('THEIR_REVOKED_UNILATERAL', 'THEIR_HTLC'): 'penalty-offered-htlc',
    ('THEIR_REVOKED_UNILATERAL', 'OUR_HTLC'): 'penalty-received-htlc',
}
RESOLVED = re.compile(r'Resolved (\w+)/(\w+) by our proposal (\w+) \(([0-9a-f]{64})\)')
ANCHOR = re.compile(r'Creating anchor spend for (\w+) commit tx ([0-9a-f]{64})')
ANCHOR_RBF = re.compile(r'RBF anchor spend: Old tx ([0-9a-f]{64}) new ([0-9a-f]{64})')
FINAL = ('SENT_ADD_ACK_REVOCATION', 'RCVD_ADD_ACK_REVOCATION')


def wait_for(f, timeout=60, what=''):
    end = time.time() + timeout
    while time.time() < end:
        r = f()
        if r:
            return r
        time.sleep(0.25)
    raise TimeoutError('timed out waiting for ' + what)


class Node:
    def __init__(self, name, port, url):
        self.name, self.port = name, port
        self.dir = os.path.join(WORK, name)
        self.logf = os.path.join(WORK, name + '.log')   # outside the dir: survives a restore
        os.makedirs(self.dir)
        self.args = [LIGHTNINGD, '--network=regtest', '--lightning-dir=' + self.dir,
                     '--disable-plugin=bcli', '--plugin=' + os.path.join(HERE, 'toychain.py'),
                     '--toychain-url=' + url, '--plugin=' + os.path.join(HERE, 'hold.py'),
                     '--hold-all=false', '--developer', '--dev-bitcoind-poll=1', '--dev-no-reconnect',
                     '--dev-fast-gossip', '--bind-addr=127.0.0.1:%d' % port,
                     '--log-level=debug', '--log-file=' + self.logf, '--alias=' + name]
        self.proc = None
        self.rpc = LightningRpc(os.path.join(self.dir, 'regtest', 'lightning-rpc'))

    def start(self):
        self.proc = subprocess.Popen(self.args, stdout=open(os.path.join(WORK, self.name + '.out'), 'a'),
                                     stderr=subprocess.STDOUT)
        wait_for(lambda: os.path.exists(os.path.join(self.dir, 'regtest', 'lightning-rpc')), what='rpc')
        wait_for(self._ok, what='getinfo')
        self.id = self.rpc.getinfo()['id']

    def _ok(self):
        try:
            self.rpc.getinfo()
            return True
        except Exception:
            return False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        try:
            self.rpc.stop()
        except Exception:
            pass
        self.proc.wait(30)
        os.remove(os.path.join(self.dir, 'regtest', 'lightning-rpc')) \
            if os.path.exists(os.path.join(self.dir, 'regtest', 'lightning-rpc')) else None

    def log(self):
        return open(self.logf).read() if os.path.exists(self.logf) else ''

    def count(self, pattern):
        return self.log().count(pattern)

    def synced(self, height):
        wait_for(lambda: self.rpc.getinfo()['blockheight'] >= height
                 and 'warning_lightningd_sync' not in self.rpc.getinfo(),
                 what='%s sync to %d' % (self.name, height))

    def chan(self, funding):
        for c in self.rpc.listpeerchannels()['channels']:
            if c.get('funding_txid') == funding:
                return c
        return None

    def state(self, funding):
        c = self.chan(funding)
        return c['state'] if c else None


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    chain_log = os.path.join(WORK, 'txs.jsonl')
    chain = Chain(chain_log)
    srv = serve(chain)
    url = 'http://127.0.0.1:%d' % srv.server_address[1]
    a, b = Node('A', 21751, url), Node('B', 21752, url)
    labels = collections.defaultdict(list)     # txid -> [kind]
    notes = collections.defaultdict(list)      # txid -> [note]
    results = []

    def label(txid, kind, note=None):
        if kind not in labels[txid]:
            labels[txid].append(kind)
        if note and note not in notes[txid]:
            notes[txid].append(note)

    def step(name, ok=True):
        results.append((name, ok))
        print(('PASS ' if ok else 'FAIL ') + name, flush=True)

    def mine(n=1):
        chain.mine(n)
        for node in (a, b):
            if node.running():
                node.synced(chain.tip())

    def mine_until(cond, what, blocks=60):
        # CLN rebroadcasts every 30-60 s, so keep the chain moving.
        for _ in range(blocks):
            if cond():
                return True
            mine()
            time.sleep(1)
        if cond():
            return True
        step('timed out: ' + what, False)
        return False

    def in_chain(txid):
        # close/withdraw can return before the backend plugin has sent the tx
        try:
            return wait_for(lambda: chain.find_tx(txid) is not None, timeout=10, what=txid)
        except TimeoutError:
            return False

    def connect():
        a.rpc.connect(b.id, '127.0.0.1', b.port)

    def open_channel(opener, peer, sats, push_msat=None):
        params = {'id': peer.id, 'amount': sats}
        if push_msat:
            params['push_msat'] = push_msat
        funding = opener.rpc.call('fundchannel', params)['txid']
        label(funding, 'funding', 'opened by ' + opener.name)
        mine(6)
        wait_for(lambda: opener.state(funding) == 'CHANNELD_NORMAL'
                 and peer.state(funding) == 'CHANNELD_NORMAL', what='channel normal')
        return funding

    def pay_bg(payer, bolt11):
        def run():
            try:
                payer.rpc.call('pay', {'bolt11': bolt11})
            except Exception:
                pass   # fails if the channel goes on chain first: expected
        threading.Thread(target=run, daemon=True).start()

    def held_invoice(payee, payer, msat, name, cltv=None):
        inv = payee.rpc.invoice(msat, name, name, cltv=cltv)
        payee.rpc.call('hold-add', {'payment_hash': inv['payment_hash']})
        pay_bg(payer, inv['bolt11'])
        return inv

    def committed(funding, n):
        def ok():
            for node in (a, b):
                hs = node.chan(funding)['htlcs']
                if len(hs) != n or any(h['state'] not in FINAL for h in hs):
                    return False
            return True
        wait_for(ok, what='%d HTLCs irrevocably committed' % n)

    def no_htlcs(funding):
        wait_for(lambda: not a.chan(funding)['htlcs'] and not b.chan(funding)['htlcs'],
                 what='HTLCs settled')

    def resolutions(node):
        return {m.group(4) for m in RESOLVED.finditer(node.log())}

    def marks():
        # log offsets: wait_resolved counts what was logged after these
        return {node.name: len(node.log()) for node in (a, b)}

    def wait_resolved(node, txtype, outtype, n, what, since):
        pat = 'Resolved %s/%s by our proposal' % (txtype, outtype)
        ok = mine_until(lambda: node.log()[since[node.name]:].count(pat) >= n, what)
        if ok:
            step(what)
        return ok

    def spend_to_remote(commits, what):
        outs = [o for o in a.rpc.listfunds()['outputs']
                if o['txid'] in commits and o['status'] == 'confirmed']
        if not outs:
            step(what + ': no to_remote output in the wallet', False)
            return
        dest = b.rpc.newaddr('bech32')['bech32']
        w = a.rpc.withdraw(dest, 'all', utxos=['%s:%d' % (o['txid'], o['output']) for o in outs])
        label(w['txid'], 'to_remote-spend', 'A withdraws its to_remote output(s) from ' + what)
        step('A spends %d to_remote output(s) from %s with withdraw' % (len(outs), what),
             in_chain(w['txid']))
        mine()

    a.start()
    b.start()

    # Wallet: deposits to P2TR and P2WPKH, then a withdraw spending both kinds.
    for node, n in ((a, 3), (b, 2)):
        for kind in ('p2tr', 'bech32'):
            for _ in range(n):
                addr = node.rpc.newaddr(kind)[kind]
                label(chain.deposit(address_script(addr).hex(), 2_000_000), 'deposit',
                      '%s %s' % (node.name, kind))
    mine()
    wait_for(lambda: len(a.rpc.listfunds()['outputs']) == 6 and len(b.rpc.listfunds()['outputs']) == 4,
             what='deposits')
    outs = a.rpc.listfunds()['outputs']
    mixed = [o for o in outs if o['scriptpubkey'].startswith('5120')][:2] + \
            [o for o in outs if o['scriptpubkey'].startswith('0014')][:2]
    w = a.rpc.withdraw(b.rpc.newaddr('p2tr')['p2tr'], 3_000_000,
                       utxos=['%s:%d' % (o['txid'], o['output']) for o in mixed])
    label(w['txid'], 'wallet-mixed', 'A withdraw: 2 P2TR + 2 P2WPKH inputs')
    step('withdraw spending 2 P2TR + 2 P2WPKH wallet inputs', in_chain(w['txid']))
    exit_addr = segwit_address('bcrt', 16, bytes.fromhex('00112233445566778899aabbccddeeff00112233'))
    w = a.rpc.withdraw(exit_addr, 10_000)
    label(w['txid'], 'withdraw-exit', 'A withdraw to a segwit v16 exit address')
    mine()

    # Mutual closes: started by the opener and by the peer, from both nodes.
    connect()
    for opener, peer, closer in ((a, b, a), (b, a, b), (a, b, b)):
        f = open_channel(opener, peer, 400_000)
        payer, payee = opener, peer
        payer.rpc.pay(payee.rpc.invoice(25_000_000, 'mc' + f[:8], 'mc')['bolt11'])
        other = b if closer is a else a
        txid = closer.rpc.close(other.id)['txids'][0]
        label(txid, 'mutual-close', 'opened by %s, close started by %s' % (opener.name, closer.name))
        step('mutual close (opener %s, started by %s)' % (opener.name, closer.name),
             in_chain(txid))
        mine()

    # Q: B restored from a backup taken with an HTLC in flight broadcasts the
    # revoked commitment and its HTLC-success tx; A penalizes the HTLC tx.
    # (First of the force closes: restarting a node whose onchain channels
    # replay HTLC spends crashed lightningd in replay_block, see the report.)
    # The restored B carries on for the rest: the newer B made no wallet txs.
    connect()
    f = open_channel(a, b, 600_000, push_msat=200_000_000)   # B needs a to_local
    hq = held_invoice(b, a, 80_000_000, 'q1')
    committed(f, 1)
    b.stop()
    snap = os.path.join(WORK, 'B.snap')
    shutil.copytree(b.dir, snap)
    b.start()          # replays the HTLC: no hold now, so B fulfills it
    connect()
    no_htlcs(f)
    a.stop()
    b.stop()
    os.rename(b.dir, os.path.join(WORK, 'B.cur'))
    shutil.copytree(snap, b.dir)
    b.start()
    base = marks()
    wait_for(lambda: b.rpc.listinvoices('q1')['invoices'][0]['status'] == 'paid', what='restored B fulfills')
    qcommit = b.rpc.close(a.id, unilateraltimeout=1)['txids'][0]
    label(qcommit, 'revoked-commitment', 'B restored from an old backup broadcasts it (close, A offline)')
    step('Q: restored B broadcasts its revoked commitment', in_chain(qcommit))
    mine()
    wait_resolved(b, 'OUR_UNILATERAL', 'THEIR_HTLC', 1, 'Q: restored B broadcasts its HTLC-success tx', base)
    a.start()
    ok = mine_until(lambda: a.log()[base['A']:].count('DELAYED_CHEAT_OUTPUT_TO_THEM by our proposal OUR_PENALTY_TX') >= 2,
                    'Q: A penalizes to_local and the HTLC-success output', blocks=10)
    step('Q: A penalizes to_local and the HTLC-success output', ok)
    mine()
    spend_to_remote({qcommit}, 'Q')

    connect()
    # P: a revoked commitment of B's, with to_local, to_remote and HTLCs both
    # ways, signed with dev-sign-last-tx and sent to the chain by us.
    f = open_channel(a, b, 1_000_000, push_msat=400_000_000)
    ha = held_invoice(b, a, 60_000_000, 'pA')
    hb = held_invoice(a, b, 70_000_000, 'pB')
    committed(f, 2)
    revoked = b.rpc.call('dev-sign-last-tx', {'id': a.id})['tx']
    rtx = Tx.parse(bytes.fromhex(revoked))
    step('B signs its commitment with 2 HTLCs (%d outputs)' % len(rtx.outputs), len(rtx.outputs) == 6)
    b.rpc.call('hold-release', {'payment_hash': ha['payment_hash']})
    a.rpc.call('hold-release', {'payment_hash': hb['payment_hash']})
    no_htlcs(f)
    a.rpc.pay(b.rpc.invoice(10_000_000, 'pC', 'pC')['bolt11'])
    no_htlcs(f)
    base = marks()
    res = chain.send(revoked)
    label(rtx.txid(), 'revoked-commitment', "B's, signed by dev-sign-last-tx, sent by the test")
    step('the revoked commitment is accepted by the chain', res['success'])
    mine()
    wait_resolved(a, 'THEIR_REVOKED_UNILATERAL', 'DELAYED_CHEAT_OUTPUT_TO_THEM', 1, 'P: A penalizes to_local', base)
    wait_resolved(a, 'THEIR_REVOKED_UNILATERAL', 'THEIR_HTLC', 1, 'P: A penalizes the offered HTLC', base)
    wait_resolved(a, 'THEIR_REVOKED_UNILATERAL', 'OUR_HTLC', 1, 'P: A penalizes the received HTLC', base)
    mine()
    spend_to_remote({rtx.txid()}, 'P')

    # X: B force-closes with four HTLCs in flight, under fee pressure.
    f = open_channel(a, b, 1_000_000, push_msat=400_000_000)
    scid = a.chan(f)['short_channel_id']
    h1 = held_invoice(b, a, 50_000_000, 'x1')   # B claims it: HTLC-success
    held_invoice(b, a, 40_000_000, 'x2')         # never released: A's remote timeout
    # A claims x3 on chain: a remote success.  Its final CLTV leaves room for
    # A's own "expiry too soon" check (height + cltv-final) when it is released.
    held_invoice(a, b, 45_000_000, 'x3', cltv=40)
    # x4: B -> A to a hash A has no invoice for; A holds it, then fails it
    # once on chain: B times it out with its HTLC-timeout tx.
    h4 = hashlib.sha256(b'x4').hexdigest()
    a.rpc.call('hold-add', {'payment_hash': h4})
    b.rpc.call('sendpay', {'route': [{'id': a.id, 'channel': scid, 'amount_msat': 35_000_000,
                                      'delay': 20, 'style': 'tlv'}],
                           'payment_hash': h4, 'payment_secret': '11' * 32})
    committed(f, 4)
    # A stays up but offline (no reconnect): no update_fee, and no race with
    # A dropping its own commitment on B's error.  (Restarting A here instead
    # crashed lightningd in replay_block, see the report.)
    a.rpc.disconnect(b.id, force=True)
    wait_for(lambda: not a.chan(f)['peer_connected'] and not b.chan(f)['peer_connected'],
             what='disconnected')
    base = marks()
    chain.set_fees(HIGH_FEES)
    time.sleep(4)        # B polls estimatefees every second
    commit = b.rpc.close(a.id, unilateraltimeout=1)['txids'][0]
    label(commit, 'commitment', "B's, 4 HTLCs, broadcast by B (close, A offline)")
    step('X: B broadcasts its commitment', in_chain(commit))
    try:
        wait_for(lambda: ANCHOR.search(b.log()), timeout=20, what='anchor')
        step('X: B spends its anchor to CPFP the commitment (fees raised after it was signed)')
    except TimeoutError:
        step('X: B spends its anchor to CPFP the commitment', False)
    mine()
    chain.set_fees(DEFAULT_FEES)
    wait_for(lambda: b.state(f) == 'ONCHAIN' and a.state(f) == 'ONCHAIN', what='onchain')
    a.rpc.call('hold-release', {})   # x3: A fulfills it on chain; x4: no invoice, A fails it
    b.rpc.call('hold-release', {'payment_hash': h1['payment_hash']})
    wait_resolved(b, 'OUR_UNILATERAL', 'THEIR_HTLC', 1, 'X: B claims an HTLC with its HTLC-success tx', base)
    wait_resolved(a, 'THEIR_UNILATERAL', 'THEIR_HTLC', 1, 'X: A claims an HTLC from B\'s commitment with the preimage', base)
    wait_resolved(b, 'OUR_UNILATERAL', 'DELAYED_OUTPUT_TO_US', 1, 'X: B sweeps its to_local', base)
    wait_resolved(b, 'OUR_HTLC_SUCCESS_TX', 'DELAYED_OUTPUT_TO_US', 1, 'X: B sweeps its HTLC-success output', base)
    wait_resolved(a, 'THEIR_UNILATERAL', 'OUR_HTLC', 1, 'X: A times out an HTLC on B\'s commitment', base)
    wait_resolved(b, 'OUR_UNILATERAL', 'OUR_HTLC', 1, 'X: B times out an HTLC with its HTLC-timeout tx', base)
    wait_resolved(b, 'OUR_HTLC_TIMEOUT_TX', 'DELAYED_OUTPUT_TO_US', 1, 'X: B sweeps its HTLC-timeout output', base)
    mine()
    spend_to_remote({commit}, 'X')

    # Labels from the nodes' logs.
    for node in (a, b):
        log = node.log()
        for m in RESOLVED.finditer(log):
            txtype, outtype, proposal, txid = m.groups()
            kind = RESOLUTIONS.get((txtype, outtype))
            if kind is None and outtype == 'DELAYED_CHEAT_OUTPUT_TO_THEM':
                kind = 'penalty-htlc-tx'
            label(txid, kind or '%s/%s->%s' % (txtype, outtype, proposal), 'by ' + node.name)
        for m in ANCHOR.finditer(log):
            label(m.group(2), 'anchor-cpfp', 'by %s, for its %s commitment' % (node.name, m.group(1)))
        for m in ANCHOR_RBF.finditer(log):
            label(m.group(2), 'anchor-cpfp', 'by %s, RBF of %s' % (node.name, m.group(1)))
    a.stop()
    b.stop()
    srv.shutdown()

    rows, unlabelled = [], []
    for line in open(chain_log):
        r = json.loads(line)
        ks = labels.get(r['txid'])
        if not ks:
            unlabelled.append(r['txid'])
        r['kind'] = '+'.join(ks) if ks else 'unlabelled'
        if notes.get(r['txid']):
            r['note'] = '; '.join(notes[r['txid']])
        rows.append(r)
    with open(OUT, 'w') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    counts = collections.Counter(r['kind'] for r in rows)
    print('\nkinds captured:', dict(sorted(counts.items())))
    # Refused txs, by the output they tried to spend and what spent it first.
    kind_of = {r['txid']: r['kind'] for r in rows}
    spent_by = {}
    for r in rows:
        for txid_in, vout, _, _ in Tx.parse(bytes.fromhex(r['tx'])).inputs:
            spent_by[(txid_in, vout)] = r['kind']
    outputs = {}
    for r in rows:
        for n, (sats, spk) in enumerate(Tx.parse(bytes.fromhex(r['tx'])).outputs):
            outputs[(r['txid'], n)] = {'amount': sats, 'script': spk.hex()}
    logs = {node.name: node.log() for node in (a, b)}
    refused = collections.OrderedDict()
    for h, txid, err, raw in chain.rejects:
        tx = Tx.parse(bytes.fromhex(raw))
        why = []
        for n, (txid_in, vout, _, _) in enumerate(tx.inputs):
            if (txid_in, vout) in spent_by:
                try:
                    _, tpl, path, _ = verify_vectors.classify_input(tx, n, outputs[(txid_in, vout)])
                except Exception as e:
                    tpl, path = '?', str(e)
                why.append('%s:%d (%s by %s) already spent by %s' % (
                    kind_of.get(txid_in, '?'), vout, tpl, path, spent_by[(txid_in, vout)]))
        sender = [name for name, log in logs.items() if raw in log]
        key = (err.split(' ')[0], '%s: %s' % ('+'.join(sender) or '?', '; '.join(why) or err))
        e = refused.setdefault(key, [h, h, set(), 0])
        e[1], e[3] = h, e[3] + 1
        e[2].add(txid)
    print('\ntxs the chain refused (reason: distinct txs, sends, heights):')
    for (_, why), (h0, h1, txids, n) in refused.items():
        print('  %s: %d txs, %d sends, heights %d-%d' % (why, len(txids), n, h0, h1))
    print()
    ok = verify_vectors.main(OUT)
    step('every kind spends the script it claims', ok)
    print('%d/%d passed; %d txs in %s' % (sum(ok for _, ok in results), len(results), len(rows), OUT))
    return all(ok for _, ok in results)


if __name__ == '__main__':
    try:
        ok = main()
    finally:
        subprocess.run(['pkill', '-f', 'lightning-dir=' + WORK])
    sys.exit(0 if ok else 1)
