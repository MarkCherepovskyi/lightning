#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Spike S1 + S4: two unmodified lightningd (regtest) on the toy chain.

  PATH=<venv>/bin:$PATH python3 run_s1.py [lightningd] [workdir]

Deposits to P2TR and P2WPKH wallet addresses, a channel A->B, a payment, a
mutual close, a withdraw to a segwit v16 "exit" address (S4), then a second
channel force-closed by A while B is offline, swept by both.  Every applied
transaction goes to <workdir>/txs.jsonl (test vectors for spike S3)."""
from btc import address_script, segwit_address
from chaind import Chain, serve
from pyln.client import LightningRpc

import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIGHTNINGD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '../../../../lightningd/lightningd')
WORK = sys.argv[2] if len(sys.argv) > 2 else '/tmp/evs1'


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
        os.makedirs(self.dir)
        self.args = [LIGHTNINGD, '--network=regtest', '--lightning-dir=' + self.dir,
                     '--disable-plugin=bcli', '--plugin=' + os.path.join(HERE, 'toychain.py'),
                     '--toychain-url=' + url, '--developer', '--dev-bitcoind-poll=1',
                     '--dev-fast-gossip', '--bind-addr=127.0.0.1:%d' % port,
                     '--log-level=debug', '--log-file=' + os.path.join(self.dir, 'log'),
                     '--alias=' + name]
        self.proc = None
        self.rpc = LightningRpc(os.path.join(self.dir, 'regtest', 'lightning-rpc'))

    def start(self):
        self.proc = subprocess.Popen(self.args, stdout=open(os.path.join(self.dir, 'stdout'), 'a'),
                                     stderr=subprocess.STDOUT)
        wait_for(lambda: os.path.exists(os.path.join(self.dir, 'regtest', 'lightning-rpc')), what='rpc')
        wait_for(lambda: self._ok(), what='getinfo')
        self.info = self.rpc.getinfo()

    def _ok(self):
        try:
            self.rpc.getinfo()
            return True
        except Exception:
            return False

    def stop(self):
        try:
            self.rpc.stop()
        except Exception:
            pass
        self.proc.wait(30)

    def synced(self, height):
        wait_for(lambda: self.rpc.getinfo()['blockheight'] >= height
                 and 'warning_lightningd_sync' not in self.rpc.getinfo(),
                 what='%s sync to %d' % (self.name, height))

    def chan(self, peer):
        cs = [c for c in self.rpc.listpeerchannels(peer.info['id'])['channels']]
        return cs[-1] if cs else None


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    chain = Chain(os.path.join(WORK, 'txs.jsonl'))
    srv = serve(chain)
    url = 'http://127.0.0.1:%d' % srv.server_address[1]
    a, b = Node('A', 21741, url), Node('B', 21742, url)
    def mine(n=1):
        chain.mine(n)
        for node in (a, b):
            if node.proc and node.proc.poll() is None:
                node.synced(chain.tip())
    results = []

    def step(name, ok=True):
        results.append((name, ok))
        print(('PASS ' if ok else 'FAIL ') + name, flush=True)

    a.start()
    b.start()
    step('both nodes start and sync on block 0 of the toy chain (no CLN change)')

    # S4: wallet deposits to P2TR and P2WPKH addresses.
    p2tr = a.rpc.newaddr('p2tr')['p2tr']
    wpkh = a.rpc.newaddr('bech32')['bech32']
    chain.deposit(address_script(p2tr).hex(), 1_000_000)
    chain.deposit(address_script(wpkh).hex(), 1_000_000)
    chain.deposit(address_script(b.rpc.newaddr('p2tr')['p2tr']).hex(), 1_000_000)
    mine()
    wait_for(lambda: len(a.rpc.listfunds()['outputs']) == 2, what='A sees deposits')
    step('A sees deposits to a P2TR and a P2WPKH address')

    # A channel A->B: open, confirm, pay.
    a.rpc.connect(b.info['id'], '127.0.0.1', b.port)
    res = a.rpc.fundchannel(b.info['id'], 500_000)
    funding = res['txid']
    step('fundchannel builds and sends the funding tx (wallet P2TR/P2WPKH inputs)',
         chain.find_tx(funding) is not None)
    mine(6)
    wait_for(lambda: a.chan(b)['state'] == 'CHANNELD_NORMAL'
             and b.chan(a)['state'] == 'CHANNELD_NORMAL', what='channel normal')
    step('channel reaches CHANNELD_NORMAL on both sides')
    inv = b.rpc.invoice(50_000_000, 'p1', 'p1')['bolt11']
    a.rpc.pay(inv)
    step('A pays B 50,000 sat')
    wait_for(lambda: len(a.rpc.listchannels()['channels']) == 2, timeout=90, what='gossip')
    step('the channel is announced and verified by gossip (getutxout)')

    # Mutual close.
    close = a.rpc.close(b.info['id'])
    step('mutual close tx accepted by the chain', chain.find_tx(close['txids'][0]) is not None)
    mine()
    wait_for(lambda: a.chan(b) is None or a.chan(b)['state'] in ('CLOSINGD_COMPLETE', 'FUNDING_SPEND_SEEN', 'ONCHAIN'),
             what='A sees the close')
    step('both see the mutual close on chain')

    # S4: withdraw to a segwit v16 "exit" address carrying an ETH address.
    eth = bytes.fromhex('00112233445566778899aabbccddeeff00112233')
    exit_addr = segwit_address('bcrt', 16, eth)
    try:
        w = a.rpc.withdraw(exit_addr, 10_000)
        tx = chain.find_tx(w['txid'])
        step('withdraw to a segwit v16 exit address (%s)' % exit_addr, tx is not None)
    except Exception as e:
        step('withdraw to a segwit v16 exit address: %s' % e, False)
    mine()

    # A second channel, force-closed by A while B is offline.
    res = a.rpc.fundchannel(b.info['id'], 300_000)
    mine(6)
    wait_for(lambda: a.chan(b)['state'] == 'CHANNELD_NORMAL', what='second channel')
    a.rpc.pay(b.rpc.invoice(20_000_000, 'p2', 'p2')['bolt11'])
    b.stop()
    close = a.rpc.close(b.info['id'], unilateraltimeout=1)
    commit = close['txids'][0]
    wait_for(lambda: chain.find_tx(commit) is not None, what='commitment applied')
    step('unilateral close: commitment tx (2-of-2 + anchors) accepted')
    mine()
    b.start()
    a_log = os.path.join(a.dir, 'log')

    def mine_until(cond, what, blocks=200):
        # CLN rebroadcasts every 30-60 s, so keep the chain moving.
        for _ in range(blocks):
            if cond():
                return
            mine()
            time.sleep(1)
        raise TimeoutError(what)
    mine_until(lambda: 'by our proposal OUR_DELAYED_RETURN_TO_WALLET' in open(a_log).read(),
               'A sweeps to_local')
    step('A sweeps its to_local after the CSV delay (a P2TR sweep)')
    mine(100)
    mine_until(lambda: 'onchaind complete, forgetting peer' in open(a_log).read(),
               'onchaind done')
    step('onchaind resolves everything and forgets the channel')
    # A third channel, with an HTLC in flight that B holds: A force-closes,
    # then times the HTLC out with its HTLC-timeout tx (B's signature is
    # SIGHASH_SINGLE|ANYONECANPAY; A adds wallet L1 inputs for the fee).
    b.stop()
    b.args.append('--plugin=' + os.path.join(HERE, 'hold.py'))
    b.start()
    a.rpc.connect(b.info['id'], '127.0.0.1', b.port)
    a.rpc.fundchannel(b.info['id'], 200_000)
    mine(6)
    wait_for(lambda: a.chan(b)['state'] == 'CHANNELD_NORMAL'
             and b.chan(a)['state'] == 'CHANNELD_NORMAL', what='third channel')
    inv = b.rpc.invoice(30_000_000, 'held', 'held')
    # (pay blocks while B holds the HTLC: run it aside)
    def pay_held():
        try:
            a.rpc.call('pay', {'bolt11': inv['bolt11']})
        except Exception:
            pass  # it fails once A force-closes: expected
    threading.Thread(target=pay_held, daemon=True).start()
    wait_for(lambda: any(h['state'] == 'SENT_ADD_ACK_REVOCATION' for h in a.chan(b)['htlcs']),
             what='HTLC irrevocably committed')
    b.stop()
    close = a.rpc.close(b.info['id'], unilateraltimeout=1)
    wait_for(lambda: chain.find_tx(close['txids'][0]) is not None, what='commitment with HTLC applied')
    mine_until(lambda: 'by our proposal OUR_HTLC_TIMEOUT_TX' in open(a_log).read(),
               'A times the HTLC out on chain')
    step('A broadcasts its HTLC-timeout tx (SINGLE|ANYONECANPAY + wallet inputs)')
    mine_until(lambda: 'Resolved OUR_HTLC_TIMEOUT_TX/DELAYED_OUTPUT_TO_US by our proposal'
               in open(a_log).read(), 'A sweeps the HTLC-timeout output')
    step('A sweeps the HTLC-timeout output after its CSV delay')
    print('A wallet:', sum(o['amount_msat'] for o in a.rpc.listfunds()['outputs']) // 1000, 'sat')

    a.stop()
    srv.shutdown()
    print('%d/%d passed; txs in %s' % (sum(ok for _, ok in results), len(results),
                                       os.path.join(WORK, 'txs.jsonl')))
    return all(ok for _, ok in results)


if __name__ == '__main__':
    try:
        ok = main()
    finally:
        subprocess.run(['pkill', '-f', 'lightning-dir=' + WORK])
    sys.exit(0 if ok else 1)
