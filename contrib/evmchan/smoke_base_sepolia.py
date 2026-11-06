#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Milestone M5: the Base Sepolia smoke test of SPEC 7 (manual, scripted).

Two unmodified lightningd on evmbackend.py against UtxoChain on Base Sepolia
through Infura: deposits, open, pay both ways, gossip, mutual close, then
the coins go back out (exit + withdraw) and the nodes' ETH back to the
deployer. Records the time of each step, the lag of `safe`, and every EVM
RPC call by method (evm-status's rpc_calls), priced in Infura credits.

    INFURA_API_KEY=... DEPLOYER_PRIVATE_KEY=0x... \\
    PATH=<venv>/bin:$PATH PYTHONPATH=<tree>/contrib/pyln-client:<tree>/contrib/pyln-proto \\
        python3 smoke_base_sepolia.py [--contract 0x...] [--workdir /tmp/evs] [--no-refund]

Without --contract it deploys one (deploy.sh, INTERVAL 60) and writes its
address to <workdir>/../evs-contract. Neither secret is written to a file or
printed; the RPC URL is passed to lightningd as a template
(${INFURA_API_KEY}), which the plugin fills from its environment. Node keys
are made in <workdir>/<node>/regtest/evm-key at mode 0600. Results go to
<workdir>/results.json. Takes about 40-60 minutes: heights are wall clock
(INTERVAL 60), funding needs 3 blocks under `safe`, gossip 6."""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import evm   # noqa: E402
from pyln.client import LightningRpc, RpcError   # noqa: E402

RPC_TEMPLATE = 'https://base-sepolia.infura.io/v3/${INFURA_API_KEY}'
CHAIN_ID = 84532
INTERVAL = 60
SAT = 10**10                     # wei per sat (SPEC 2)
# SPEC 5.6, public chains, INTERVAL 60: 24 h = 1440 blocks, 2.4 h = 144.
OPTIONS_56 = ['--watchtime-blocks=1440', '--cltv-delta=144', '--cltv-final=144', '--funding-confirms=3',
              '--max-concurrent-htlcs=30', '--developer', '--dev-force-features=-63']
FUND_WEI = {'A': 3 * 10**16, 'B': 2 * 10**16}       # 0.03 / 0.02 ETH; evm-min-balance is 0.01
# Infura credits per call (docs.infura.io/get-started/pricing/credit-cost, Ethereum table,
# which other networks default to). Unlisted methods count as 80.
CREDITS = {'eth_getLogs': 255, 'eth_estimateGas': 300, 'eth_chainId': 5}
CREDITS_DEFAULT = 80
FREE_TIER_PER_DAY = 3_000_000


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def wait_for(f, timeout, what, poll=5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            r = f()
        except Exception as e:      # noqa: B902
            r, last = None, e
        if r:
            return r
        time.sleep(poll)
    raise TimeoutError('timed out ({} s) waiting for {}{}'.format(
        timeout, what, ' (last error: {})'.format(type(last).__name__) if last else ''))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def credits(calls):
    return sum(n * CREDITS.get(m, CREDITS_DEFAULT) for m, n in calls.items())


class Node:
    def __init__(self, name, work, lightningd, contract):
        self.name, self.port = name, free_port()
        self.dir = os.path.join(work, name)
        keyfile = os.path.join(self.dir, 'regtest', 'evm-key')
        if os.path.exists(keyfile):        # --cleanup: an earlier run's node
            with open(keyfile) as f:
                key = bytes.fromhex(f.read().strip()[2:])
        else:
            os.makedirs(os.path.join(self.dir, 'regtest'))
            key = os.urandom(32)
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write('0x' + key.hex() + '\n')
        self.account = evm.Account(key)
        self.eth = self.account.address
        self.args = [lightningd, '--network=regtest', '--lightning-dir=' + self.dir,
                     '--disable-plugin=bcli', '--plugin=' + os.path.join(HERE, 'evmbackend.py'),
                     '--evm-rpc=' + RPC_TEMPLATE, '--evm-contract=' + contract,
                     '--evm-confirm=safe', '--evm-poll=45',
                     '--bind-addr=127.0.0.1:%d' % self.port, '--log-level=debug',
                     '--log-file=' + os.path.join(self.dir, 'log'), '--alias=' + name] + OPTIONS_56
        self.rpc = LightningRpc(os.path.join(self.dir, 'regtest', 'lightning-rpc'))
        self.proc = None
        self.info = None

    def start(self):
        self.proc = subprocess.Popen(self.args, stdout=open(os.path.join(self.dir, 'stdout'), 'a'),
                                     stderr=subprocess.STDOUT, start_new_session=True)
        wait_for(lambda: self.rpc.getinfo(), 180, self.name + ' getinfo', poll=1)
        self.info = self.rpc.getinfo()

    def stop(self):
        if self.proc is None:
            return
        try:
            self.rpc.stop()
            self.proc.wait(60)
        except Exception:           # noqa: B902
            self.proc.kill()
            self.proc.wait(10)

    def log_text(self):
        try:
            with open(os.path.join(self.dir, 'log')) as f:
                return f.read()
        except OSError:
            return ''

    def status(self):
        return self.rpc.call('evm-status')

    def chan(self, peer):
        cs = self.rpc.listpeerchannels(peer.info['id'])['channels']
        return cs[-1] if cs else None

    def confirmed_sat(self):
        return sum(o['amount_msat'] // 1000 for o in self.rpc.listfunds()['outputs'] if o['status'] == 'confirmed')


def exit_coins(nodes):
    """Each node's wallet to the exit address of its own ETH address, then
    the contract's withdraw() to that address. Waits until every channel's
    close is on chain (ONCHAIN), so its outputs are in the wallet. While a
    node still knows an anchor channel (until about 100 blocks after its
    close), CLN keeps min-emergency-msat (25,000 sat) back from `all`."""
    wait_for(lambda: all(c['state'] == 'ONCHAIN' for n in nodes for c in n.rpc.listpeerchannels()['channels']),
             1800, 'closes on chain')
    sent = []
    for n in nodes:
        st = n.status()
        exit_addr = n.rpc.call('evm-exitaddr', {'ethaddress': st['eth_address']})['address']

        def all_confirmed(n=n):
            outs = n.rpc.listfunds()['outputs']
            return outs and all(o['status'] == 'confirmed' for o in outs)
        wait_for(all_confirmed, 1800, n.name + ' funds confirmed')
        try:
            w = n.rpc.withdraw(exit_addr, 'all')
            sent.append(n)
            log('{}: exit tx {}'.format(n.name, w['txid']))
        except RpcError as e:
            if 'min-emergency-msat' not in str(e):
                raise
            log('{}: only the emergency reserve is left ({} sat)'.format(n.name, n.confirmed_sat()))
        left = n.confirmed_sat()
        if left:
            log('{}: {} sat stay in the wallet (min-emergency-msat)'.format(n.name, left))
    wait_for(lambda: all(n.status()['withdrawable_wei'] > 0 for n in sent), 600, 'exits credited')
    for n in sent:
        n.rpc.call('evm-withdraw')


def refund(rpc, nodes, to):
    """The nodes' ETH back to the deployer (what is left after gas)."""
    for n in nodes:
        try:
            bal = int(rpc.request('eth_getBalance', n.eth, 'latest'), 16)
            # gas at the max fee, plus a margin for the OP stack's L1 data fee
            cost = 21000 * rpc.fees()[1] + 10**12
            if bal > cost:
                rpc.send(n.account, to, value=bal - cost, gas=21000, timeout=180)
                log('{}: {:.6f} ETH back to the deployer'.format(n.name, (bal - cost) / 1e18))
        except Exception as e:      # noqa: B902
            log('refund from {} failed: {}'.format(n.name, type(e).__name__))


def cleanup(args, rpc, deployer, contract):
    """An earlier run that failed: restart its nodes, give them gas, close
    their channels, exit their coins and send their ETH back."""
    nodes = [Node(name, args.cleanup, args.lightningd, contract) for name in sorted(os.listdir(args.cleanup))
             if os.path.exists(os.path.join(args.cleanup, name, 'regtest', 'evm-key'))]
    try:
        for n in nodes:
            bal = int(rpc.request('eth_getBalance', n.eth, 'latest'), 16)
            if bal < 12 * 10**15:       # above evm-min-balance (0.01), with room for gas
                rpc.send(deployer, n.eth, value=12 * 10**15 - bal, timeout=180)
            n.start()
        wait_for(lambda: all(n.status()['opens_refused'] is None for n in nodes), 300, 'nodes ready')
        for n in nodes:
            for c in n.rpc.listpeerchannels()['channels']:
                if c['state'] in ('CHANNELD_NORMAL', 'CHANNELD_AWAITING_LOCKIN'):
                    peer = [m for m in nodes if m.info['id'] == c['peer_id']]
                    if peer:
                        n.rpc.connect(c['peer_id'], '127.0.0.1', peer[0].port)
                    log('{}: closing {}'.format(n.name, c['short_channel_id'] or c['channel_id']))
                    n.rpc.close(c['peer_id'])
        done = ('CLOSINGD_COMPLETE', 'FUNDING_SPEND_SEEN', 'ONCHAIN')
        wait_for(lambda: all(c['state'] in done for n in nodes for c in n.rpc.listpeerchannels()['channels']),
                 1800, 'closes seen')
        exit_coins(nodes)
        log('cleanup: closed, exited and withdrawn')
    finally:
        for n in nodes:
            n.stop()
        refund(rpc, nodes, deployer.address)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--contract')
    ap.add_argument('--workdir', default='/tmp/evs')
    ap.add_argument('--lightningd', default=os.path.join(HERE, '../../lightningd/lightningd'))
    ap.add_argument('--no-refund', action='store_true', help='leave the coins and ETH with the nodes')
    ap.add_argument('--cleanup', metavar='DIR', help='close, exit and refund a failed run\'s nodes in DIR')
    args = ap.parse_args()
    key = os.environ.get('INFURA_API_KEY')
    dkey = os.environ.get('DEPLOYER_PRIVATE_KEY')
    if not key or not dkey:
        sys.exit('INFURA_API_KEY and DEPLOYER_PRIVATE_KEY must be set')
    rpc = evm.EvmRpc(RPC_TEMPLATE.replace('${INFURA_API_KEY}', key))
    if rpc.chain_id() != CHAIN_ID:
        sys.exit('not Base Sepolia')
    deployer = evm.Account(bytes.fromhex(dkey[2:] if dkey.startswith('0x') else dkey))

    contract = args.contract
    if args.cleanup and not contract:
        sys.exit('--cleanup needs --contract')
    if not contract:
        log('deploying UtxoChain (INTERVAL {})'.format(INTERVAL))
        out = subprocess.run([os.path.join(HERE, 'contracts', 'deploy.sh'), '--rpc-url', RPC_TEMPLATE,
                              '--interval', str(INTERVAL)], capture_output=True, text=True,
                             env=dict(os.environ, PATH=os.path.expanduser('~/.foundry/bin') + ':' + os.environ['PATH']))
        contract = out.stdout.strip()
        if out.returncode or not re.fullmatch(r'0x[0-9a-fA-F]{40}', contract):
            sys.exit('deploy failed: ' + out.stderr.strip()[-300:])
        with open(os.path.join(os.path.dirname(args.workdir.rstrip('/')), 'evs-contract'), 'w') as f:
            f.write(contract + '\n')
    contract = evm.checksum_address(contract)
    log('contract ' + contract)
    if args.cleanup:
        return cleanup(args, rpc, deployer, contract)

    shutil.rmtree(args.workdir, ignore_errors=True)
    os.makedirs(args.workdir)
    results = {'contract': contract, 'chain_id': CHAIN_ID, 'interval': INTERVAL, 'steps': [], 'lag': []}
    t_last = [time.time()]
    t_start = time.time()

    def step(name, ok=True, **extra):
        now = time.time()
        results['steps'].append(dict(name=name, ok=bool(ok), seconds=round(now - t_last[0], 1), **extra))
        log('{} {:7.1f}s  {}'.format('PASS' if ok else 'FAIL', now - t_last[0], name))
        t_last[0] = now
        if not ok:
            raise AssertionError(name)

    def sample_lag(node):
        s = node.status()
        results['lag'].append({'t': round(time.time() - t_start), 'tag_age_s': s.get('tag_age_s'),
                               'lag_blocks': s.get('lag'), 'tip': s.get('tip'),
                               'current_height': s.get('current_height')})

    nodes = []
    try:
        a = Node('A', args.workdir, args.lightningd, contract)
        b = Node('B', args.workdir, args.lightningd, contract)
        nodes = [a, b]
        for n in nodes:
            rc = rpc.send(deployer, n.eth, value=FUND_WEI[n.name], timeout=180)
            assert int(rc['status'], 16) == 1
        step('deployer funds the node keys ({} / {} ETH)'.format(FUND_WEI['A'] / 1e18, FUND_WEI['B'] / 1e18))

        for n in nodes:
            n.start()
        wait_for(lambda: all('CLN options meet SPEC 5.6' in n.log_text() for n in nodes), 300, 'option checks')
        wait_for(lambda: all(n.status()['opens_refused'] is None for n in nodes), 300, 'opens allowed')
        # Height 0 holds only its coinbase: nothing applies in the first INTERVAL after deploy.
        wait_for(lambda: a.status()['current_height'] >= 1, 2 * INTERVAL + 60, 'height 1')
        sample_lag(a)
        step('both nodes start on evmbackend.py, pass the 5.6 option check and sync')

        p2tr = a.rpc.newaddr('p2tr')['p2tr']
        d1 = a.rpc.call('evm-deposit', {'amount': 400_000, 'address': p2tr})
        d2 = b.rpc.call('evm-deposit', {'amount': 100_000})
        assert d1['status'] == 'mined' and d2['status'] == 'mined', (d1, d2)
        wait_for(lambda: a.confirmed_sat() >= 400_000 and b.confirmed_sat() >= 100_000, 900, 'deposits confirmed')
        sample_lag(a)
        step('evm-deposit 400,000 sat to A (P2TR) and 100,000 to B; both wallets see them confirmed')

        a.rpc.connect(b.info['id'], '127.0.0.1', b.port)
        fund = a.rpc.fundchannel(b.info['id'], 250_000)
        wait_for(lambda: a.chan(b)['state'] == 'CHANNELD_NORMAL' and b.chan(a)['state'] == 'CHANNELD_NORMAL',
                 1800, 'CHANNELD_NORMAL')
        sample_lag(a)
        step('fundchannel 250,000 sat; CHANNELD_NORMAL on both after 3 confirmations', txid=fund['txid'])

        inv = b.rpc.invoice(30_000_000, 'm5a', 'smoke A->B')['bolt11']
        r1 = a.rpc.pay(inv)
        # Until the A->B HTLC is irrevocably removed, B can't spend what it got.
        wait_for(lambda: b.chan(a)['spendable_msat'] >= 10_000_000, 120, 'B can spend')
        inv = a.rpc.invoice(10_000_000, 'm5b', 'smoke B->A')['bolt11']
        r2 = b.rpc.pay(inv)
        step('A pays B 30,000 sat and B pays A 10,000 sat',
             r1['status'] == 'complete' and r2['status'] == 'complete')

        wait_for(lambda: len(a.rpc.listchannels()['channels']) == 2 and len(b.rpc.listchannels()['channels']) == 2,
                 1800, 'gossip')
        sample_lag(a)
        step('the channel is announced; both verify it (getutxout) after 6 confirmations')

        close = a.rpc.close(b.info['id'])
        done = ('CLOSINGD_COMPLETE', 'FUNDING_SPEND_SEEN', 'ONCHAIN')
        wait_for(lambda: a.chan(b)['state'] in done and b.chan(a)['state'] in done, 1800, 'close seen')
        step('mutual close negotiated and sent by both', txid=close['txids'][0])
        wait_for(lambda: b.confirmed_sat() >= 100_000 + 20_000 - 5_000, 1800, 'B gets its channel balance')
        sample_lag(a)
        step('the close tx is applied by the contract: B\'s wallet holds its output')

        if not args.no_refund:
            exit_coins(nodes)
            step('each node exits its coins and withdraws them to its ETH address')

        results['rpc'] = {}
        for n in nodes:
            st = n.status()
            hours = (time.time() - st['rpc_calls_since']) / 3600
            c = credits(st['rpc_calls'])
            results['rpc'][n.name] = {'calls': st['rpc_calls'], 'hours': round(hours, 3), 'credits': c,
                                      'credits_per_day': round(c / hours * 24)}
            log('{}: {} credits in {:.2f} h = {:,} / day ({:.1%} of the free tier); calls {}'.format(
                n.name, c, hours, round(c / hours * 24), c / hours * 24 / FREE_TIER_PER_DAY, st['rpc_calls']))
        broken = [ln for n in nodes for ln in n.log_text().splitlines() if '**BROKEN**' in ln]
        results['broken'] = broken
        step('no BROKEN log lines', not broken, count=len(broken))
        results['passed'] = True
    finally:
        for n in nodes:
            n.stop()
        if nodes and not args.no_refund:
            if results.get('passed'):
                refund(rpc, nodes, deployer.address)
            else:
                log('the run failed: its nodes keep their ETH; free their coins with '
                    '--cleanup {} --contract {}'.format(args.workdir, contract))
        results['total_seconds'] = round(time.time() - t_start)
        with open(os.path.join(args.workdir, 'results.json'), 'w') as f:
            json.dump(results, f, indent=1)
        log('results in ' + os.path.join(args.workdir, 'results.json'))


if __name__ == '__main__':
    main()
