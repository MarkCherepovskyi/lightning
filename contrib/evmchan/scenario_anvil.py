#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Milestone M3: spike S1's scenario (spikes/s1-toychain/run_s1.py) on anvil
and the real UtxoChain contract instead of the toy chain, with two unmodified
lightningd on evmbackend.py. Not pytest: M4 writes the pytest suite.

    PATH=<venv>/bin:$PATH PYTHONPATH=<tree>/contrib/pyln-client:<tree>/contrib/pyln-proto \\
        python3 scenario_anvil.py [lightningd] [workdir]

"Mining" is advancing anvil's time by INTERVAL (evm_setNextBlockTimestamp +
evm_mine). CLN rebroadcasts every 30-60 s and sends timelocked txs early, so
the waits keep the chain moving. Lightning dirs are short (/tmp/evm3: the
unix socket limit)."""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import evm        # noqa: E402
import evmindex   # noqa: E402
from pyln.client import LightningRpc, RpcError   # noqa: E402

LIGHTNINGD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '../../lightningd/lightningd')
WORK = sys.argv[2] if len(sys.argv) > 2 else '/tmp/evm3'
PLUGIN = os.path.join(HERE, 'evmbackend.py')
HOLD = os.path.join(HERE, 'spikes', 's1-toychain', 'hold.py')
INTERVAL = 2
OPTIONS_56 = ['--watchtime-blocks=40', '--cltv-delta=40', '--cltv-final=40', '--funding-confirms=1',
              '--max-concurrent-htlcs=30', '--developer', '--dev-force-features=-63', '--dev-bitcoind-poll=1']
SAT = 10**10
T0 = time.time()


def wait_for(f, timeout=60, what=''):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = f()
        except Exception:
            r = None
        if r:
            return r
        time.sleep(0.25)
    raise TimeoutError('timed out waiting for ' + what)


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Anvil:
    def __init__(self):
        self.port = free_port()
        self.url = 'http://127.0.0.1:{}'.format(self.port)
        env = dict(os.environ, PATH=os.path.expanduser('~/.foundry/bin') + ':' + os.environ['PATH'])
        self.proc = subprocess.Popen([os.path.join(HERE, 'run-anvil.sh'), '--port', str(self.port),
                                      '--interval', str(INTERVAL)], stdout=subprocess.PIPE,
                                     stderr=open(os.path.join(WORK, 'anvil.log'), 'w'), text=True,
                                     start_new_session=True, env=env)
        line = self.proc.stdout.readline().strip()
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}', line):
            raise RuntimeError('run-anvil.sh printed {!r}'.format(line))
        self.contract = evm.checksum_address(line)
        self.rpc = evm.EvmRpc(self.url)
        self.lock = threading.Lock()

        def const(name):
            return evm.abi_decode(['uint256'], self.rpc.eth_call(self.contract, evm.selector(name + '()')))[0]
        self.G, self.I = const('GENESIS_TIME'), const('INTERVAL')

    def mine1(self):
        with self.lock:
            for _ in range(10):
                ts = int(self.rpc.block('latest')['timestamp'], 16) + self.I
                try:
                    self.rpc.call('evm_setNextBlockTimestamp', evm.qty(ts))
                    self.rpc.call('evm_mine')
                    return
                except evm.EvmError:
                    continue     # an automined submit got there first
            raise RuntimeError('cannot mine')

    def expected_tip(self):
        ts = int(self.rpc.block('safe')['timestamp'], 16)
        return max((ts - self.G) // self.I - 1, 0)

    def stop(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(10)
        except Exception:
            pass
        subprocess.run(['pkill', '-f', 'anvil --port {} '.format(self.port)])


class Node:
    def __init__(self, name, port, anvil, extra=(), options=OPTIONS_56):
        self.name, self.port = name, port
        self.dir = os.path.join(WORK, name)
        os.makedirs(os.path.join(self.dir, 'regtest'))
        # The node's ETH key, made before start and funded (the plugin would
        # create it itself, but then it starts at balance 0 and alarms).
        key = os.urandom(32)
        fd = os.open(os.path.join(self.dir, 'regtest', 'evm-key'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write('0x' + key.hex() + '\n')
        self.eth = evm.Account(key).address
        anvil.rpc.call('anvil_setBalance', self.eth, evm.qty(100 * 10**18))
        self.args = [LIGHTNINGD, '--network=regtest', '--lightning-dir=' + self.dir,
                     '--disable-plugin=bcli', '--plugin=' + PLUGIN,
                     '--evm-rpc=' + anvil.url, '--evm-contract=' + anvil.contract, '--evm-poll=1',
                     '--bind-addr=127.0.0.1:%d' % port, '--log-level=debug',
                     '--log-file=' + os.path.join(self.dir, 'log'), '--alias=' + name] + list(options) + list(extra)
        self.proc = None
        self.rpc = LightningRpc(os.path.join(self.dir, 'regtest', 'lightning-rpc'))
        self.info = None

    def start(self):
        self.proc = subprocess.Popen(self.args, stdout=open(os.path.join(self.dir, 'stdout'), 'a'),
                                     stderr=subprocess.STDOUT)
        wait_for(lambda: os.path.exists(os.path.join(self.dir, 'regtest', 'lightning-rpc')), what='rpc')
        wait_for(lambda: self.rpc.getinfo(), what=self.name + ' getinfo')
        self.info = self.rpc.getinfo()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        try:
            self.rpc.stop()
        except Exception:
            pass
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)

    def log(self):
        try:
            with open(os.path.join(self.dir, 'log')) as f:
                return f.read()
        except OSError:
            return ''

    def synced(self, height, timeout=60):
        def ok():
            i = self.rpc.getinfo()
            return i['blockheight'] >= height and 'warning_bitcoind_sync' not in i \
                and 'warning_lightningd_sync' not in i
        wait_for(ok, timeout, '%s sync to %d' % (self.name, height))

    def chan(self, peer):
        cs = self.rpc.listpeerchannels(peer.info['id'])['channels']
        return cs[-1] if cs else None


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    anvil = Anvil()
    nodes = []
    results = []
    last = [time.time()]

    def step(name, ok=True):
        now = time.time()
        results.append((name, bool(ok), now - last[0]))
        print('{} {:6.1f}s  {}'.format('PASS' if ok else 'FAIL', now - last[0], name), flush=True)
        last[0] = now

    try:
        ix = evmindex.Index(os.path.join(WORK, 'view.sqlite3'), anvil.rpc, anvil.contract, tag='latest')

        def on_chain(txid):
            ix.sync()
            return ix.tx(txid) is not None

        def mine(n=1, sync=True):
            for _ in range(n):
                anvil.mine1()
            anvil.mine1()        # one more, so `safe` covers the last one
            if sync:
                tip = anvil.expected_tip()
                for node in nodes:
                    if node.running():
                        node.synced(tip)

        def mine_until(cond, what, blocks=400, pause=0.5):
            for _ in range(blocks):
                if cond():
                    return
                mine()
                time.sleep(pause)
            raise TimeoutError(what)

        for _ in range(3):
            anvil.mine1()
        a = Node('A', free_port(), anvil)
        b = Node('B', free_port(), anvil)
        nodes += [a, b]
        a.start()
        b.start()
        mine(2)
        wait_for(lambda: 'CLN options meet SPEC 5.6' in a.log() and 'CLN options meet SPEC 5.6' in b.log(),
                 what='option checks')
        step('both nodes start on evmbackend.py, pass the 5.6 option check and sync (no CLN change)')

        # Deposits to P2TR and P2WPKH wallet addresses (evm-deposit).
        p2tr = a.rpc.newaddr('p2tr')['p2tr']
        wpkh = a.rpc.newaddr('bech32')['bech32']
        d1 = a.rpc.call('evm-deposit', {'amount': 1_000_000, 'address': p2tr})
        d2 = a.rpc.call('evm-deposit', {'amount': 1_000_000, 'address': wpkh})
        d3 = b.rpc.call('evm-deposit', {'amount': 1_000_000})
        assert all(d['status'] == 'mined' for d in (d1, d2, d3)), (d1, d2, d3)
        mine(2)
        wait_for(lambda: len([o for o in a.rpc.listfunds()['outputs'] if o['status'] == 'confirmed']) == 2,
                 what='A sees deposits')
        wait_for(lambda: len(b.rpc.listfunds()['outputs']) == 1, what='B sees its deposit')
        step('evm-deposit to a P2TR and a P2WPKH address; both wallets see them')

        # A channel A->B: open, confirm, pay.
        a.rpc.connect(b.info['id'], '127.0.0.1', b.port)
        res = a.rpc.fundchannel(b.info['id'], 500_000)
        step('fundchannel: funding tx (wallet P2TR + P2WPKH inputs) applied by the contract',
             wait_for(lambda: on_chain(res['txid']), timeout=30, what='funding tx'))
        mine_until(lambda: a.chan(b)['state'] == 'CHANNELD_NORMAL' and b.chan(a)['state'] == 'CHANNELD_NORMAL',
                   'channel normal', blocks=30)
        step('channel reaches CHANNELD_NORMAL on both sides')
        inv = b.rpc.invoice(50_000_000, 'p1', 'p1')['bolt11']
        a.rpc.pay(inv)
        step('A pays B 50,000 sat')
        mine_until(lambda: len(a.rpc.listchannels()['channels']) == 2 and len(b.rpc.listchannels()['channels']) == 2,
                   'gossip', blocks=60)
        step('the channel is announced and verified by gossip (getutxout)')

        # Mutual close.
        close = a.rpc.close(b.info['id'])
        step('mutual close tx applied by the contract',
             wait_for(lambda: on_chain(close['txids'][0]), timeout=30, what='mutual close'))
        mine_until(lambda: a.chan(b)['state'] in ('CLOSINGD_COMPLETE', 'FUNDING_SPEND_SEEN', 'ONCHAIN')
                   and b.chan(a)['state'] in ('CLOSINGD_COMPLETE', 'FUNDING_SPEND_SEEN', 'ONCHAIN'),
                   'close seen', blocks=10)
        step('both see the mutual close on chain')

        # Exit: withdraw to the exit address of A's own ETH address, then the
        # contract's withdraw() pays it.
        st = a.rpc.call('evm-status')
        exit_addr = a.rpc.call('evm-exitaddr', {'ethaddress': st['eth_address']})['address']
        before_w = st['withdrawable_wei']
        w = a.rpc.withdraw(exit_addr, 100_000)
        ok = on_chain(w['txid'])
        st2 = a.rpc.call('evm-status')
        credited = st2['withdrawable_wei'] - before_w
        step('withdraw to the exit address %s credits %d wei (100,000 sat + the fee)' % (exit_addr, credited),
             ok and credited >= 100_000 * SAT)
        bal0 = int(anvil.rpc.request('eth_getBalance', st['eth_address'], 'latest'), 16)
        wd = a.rpc.call('evm-withdraw')
        rc = anvil.rpc.request('eth_getTransactionReceipt', wd['evm_tx'])
        gas_cost = int(rc['gasUsed'], 16) * int(rc['effectiveGasPrice'], 16)
        bal1 = int(anvil.rpc.request('eth_getBalance', st['eth_address'], 'latest'), 16)
        step('evm-withdraw pays the ETH address its withdrawable (%d wei)' % wd['amount_wei'],
             wd['status'] == 'mined' and bal1 - bal0 == wd['amount_wei'] - gas_cost
             and a.rpc.call('evm-status')['withdrawable_wei'] == 0)
        mine(2)

        # Rejections: a non-anchor channel_type.
        try:
            a.rpc.fundchannel(b.info['id'], 100_000, channel_type=[12])
            step('a non-anchor channel_type is refused', False)
        except RpcError as e:
            step('a non-anchor channel_type is refused ({})'.format(e.error.get('message', '')[:60]),
                 'channel_type' in e.error.get('message', ''))

        # A second channel, force-closed by A while B is offline.
        a.rpc.fundchannel(b.info['id'], 300_000)
        mine_until(lambda: a.chan(b)['state'] == 'CHANNELD_NORMAL' and b.chan(a)['state'] == 'CHANNELD_NORMAL',
                   'second channel', blocks=30)
        a.rpc.pay(b.rpc.invoice(20_000_000, 'p2', 'p2')['bolt11'])
        b.stop()
        close = a.rpc.close(b.info['id'], unilateraltimeout=1)
        commit = close['txids'][0]
        wait_for(lambda: on_chain(commit), what='commitment applied')
        step('unilateral close: commitment tx (2-of-2, anchors) applied by the contract')
        mine()
        b.start()
        mine_until(lambda: 'by our proposal OUR_DELAYED_RETURN_TO_WALLET' in a.log(), 'A sweeps to_local')
        step('A sweeps its to_local after the 40-block CSV (a P2TR sweep)')
        mine(100)
        mine_until(lambda: 'onchaind complete, forgetting peer' in a.log(), 'onchaind done')
        step('onchaind resolves everything and forgets the channel')

        # A third channel with an HTLC that B holds: A force-closes and times
        # the HTLC out (SINGLE|ANYONECANPAY plus wallet inputs).
        mine_until(lambda: 'onchaind complete, forgetting peer' in b.log(), 'B onchaind done', blocks=200)
        b.stop()
        b.args.append('--plugin=' + HOLD)
        b.start()
        mine()
        a.rpc.connect(b.info['id'], '127.0.0.1', b.port)
        a.rpc.fundchannel(b.info['id'], 200_000)
        mine_until(lambda: a.chan(b)['state'] == 'CHANNELD_NORMAL' and b.chan(a)['state'] == 'CHANNELD_NORMAL',
                   'third channel', blocks=30)
        inv = b.rpc.invoice(30_000_000, 'held', 'held')

        def pay_held():
            try:
                a.rpc.call('pay', {'bolt11': inv['bolt11']})
            except Exception:
                pass     # it fails once A force-closes: expected
        threading.Thread(target=pay_held, daemon=True).start()
        wait_for(lambda: any(h['state'] == 'SENT_ADD_ACK_REVOCATION' for h in a.chan(b)['htlcs']),
                 what='HTLC irrevocably committed')
        b.stop()
        close = a.rpc.close(b.info['id'], unilateraltimeout=1)
        wait_for(lambda: on_chain(close['txids'][0]), what='commitment with HTLC applied')
        mine_until(lambda: 'by our proposal OUR_HTLC_TIMEOUT_TX' in a.log(), 'A times the HTLC out')
        step('A broadcasts its HTLC-timeout tx (SINGLE|ANYONECANPAY + wallet inputs), the contract applies it')
        mine_until(lambda: 'Resolved OUR_HTLC_TIMEOUT_TX/DELAYED_OUTPUT_TO_US by our proposal' in a.log(),
                   'A sweeps the HTLC-timeout output')
        step('A sweeps the HTLC-timeout output after its CSV delay')

        # A node with regtest's default options refuses to open.
        c = Node('C', free_port(), anvil, options=['--developer'])
        nodes.append(c)
        c.start()
        mine()
        wait_for(lambda: 'CLN option below SPEC 5.6' in c.log(), what='C option check')
        c.rpc.connect(a.info['id'], '127.0.0.1', a.port)
        refused_out = refused_in = False
        try:
            c.rpc.fundchannel(a.info['id'], 100_000)
        except RpcError as e:
            refused_out = 'options below SPEC 5.6' in e.error.get('message', '')
        try:
            a.rpc.fundchannel(c.info['id'], 100_000)
        except RpcError as e:
            refused_in = 'options below SPEC 5.6' in json.dumps(e.error)
        step('a node with regtest defaults refuses opens (outbound: {}, inbound: {})'.format(
            refused_out, refused_in), refused_out and refused_in)

        # A peer without anchors is disconnected.
        c.stop()
        c.args.append('--dev-force-features=-23')
        c.start()
        try:
            c.rpc.connect(a.info['id'], '127.0.0.1', a.port)
        except RpcError:
            pass
        said = wait_for(lambda: 'disconnecting peer {}: no anchors'.format(c.info['id']) in a.log(), timeout=10,
                        what='A disconnects C')
        time.sleep(1)
        connected = any(p.get('connected') for p in a.rpc.listpeers(c.info['id'])['peers'])
        step('a peer without anchors (feature 22/23) is disconnected', said and not connected)

        print('A wallet:', sum(o['amount_msat'] for o in a.rpc.listfunds()['outputs']) // 1000, 'sat')
        for n in nodes:
            broken = [ln for ln in n.log().splitlines() if '**BROKEN**' in ln]
            kinds = sorted(set(re.sub(r'\d+(\.\d+)?', 'N', ln.split('**BROKEN**', 1)[1])[:110] for ln in broken))
            print('{}: {} BROKEN lines'.format(n.name, len(broken)))
            for k in kinds:
                print('   ', k)
            if 'Segmentation' in n.log() or 'FATAL SIGNAL' in n.log():
                print('{}: lightningd crashed (see its log)'.format(n.name))
        for n in nodes:
            if n.running():
                n.stop()
    except Exception as e:
        import traceback
        traceback.print_exc()
        step('scenario aborted: {}: {}'.format(type(e).__name__, e), False)
    finally:
        for n in nodes:
            if n.running():
                n.proc.kill()
        subprocess.run(['pkill', '-f', 'lightning-dir=' + WORK])
        anvil.stop()
    passed = sum(ok for _, ok, _ in results)
    print('{}/{} passed in {:.0f}s; logs in {}'.format(passed, len(results), time.time() - T0, WORK))
    return passed == len(results)


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
