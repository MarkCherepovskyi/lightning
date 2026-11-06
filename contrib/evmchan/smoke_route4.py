#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Multi-hop routing and gossip on Base Sepolia: four unmodified lightningd
on evmbackend.py in a line, A - B - C - D, one ETH channel per hop.

Each node connects only to its neighbours, so D learns the A-B channel and
node A only through gossip relayed by C and B, and verifies it on chain
itself (getutxout through its own plugin). A pays D over three hops, D pays
A back, and B and C must each have forwarded both. Then every channel is
closed, the coins exit and the ETH goes back to the deployer.

    INFURA_API_KEY=... DEPLOYER_PRIVATE_KEY=0x... \\
    PATH=<venv>/bin:$PATH PYTHONPATH=<tree>/contrib/pyln-client:<tree>/contrib/pyln-proto \\
        python3 smoke_route4.py --contract 0x... [--workdir /tmp/evr]

A failed run keeps its nodes' ETH: free it with
`smoke_base_sepolia.py --cleanup <workdir> --contract 0x...`."""
import argparse
import json
import os
import shutil
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import evm   # noqa: E402
from smoke_base_sepolia import (CHAIN_ID, FREE_TIER_PER_DAY, RPC_TEMPLATE, Node, credits,   # noqa: E402
                                exit_coins, log, refund, wait_for)

NAMES = ('A', 'B', 'C', 'D')
FUND_WEI = {'A': 2 * 10**16, 'B': 2 * 10**16, 'C': 2 * 10**16, 'D': 15 * 10**15}
DEPOSIT_SAT = {'A': 300_000, 'B': 300_000, 'C': 300_000}
CHANNEL_SAT = 200_000
PAY_MSAT = 20_000_000          # A -> D
BACK_MSAT = 5_000_000          # D -> A


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--contract', required=True)
    ap.add_argument('--workdir', default='/tmp/evr')
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

    shutil.rmtree(args.workdir, ignore_errors=True)
    os.makedirs(args.workdir)
    results = {'contract': contract, 'steps': []}
    t_last, t_start = [time.time()], time.time()

    def step(name, ok=True, **extra):
        now = time.time()
        results['steps'].append(dict(name=name, ok=bool(ok), seconds=round(now - t_last[0], 1), **extra))
        log('{} {:7.1f}s  {}'.format('PASS' if ok else 'FAIL', now - t_last[0], name))
        t_last[0] = now
        if not ok:
            raise AssertionError(name)

    nodes = []
    try:
        nodes = [Node(n, args.workdir, args.lightningd, contract) for n in NAMES]
        a, b, c, d = nodes
        for n in nodes:
            rc = rpc.send(deployer, n.eth, value=FUND_WEI[n.name], timeout=180)
            assert int(rc['status'], 16) == 1
        for n in nodes:
            n.start()
        wait_for(lambda: all(n.status()['opens_refused'] is None for n in nodes), 300, 'opens allowed')
        step('four nodes start on evmbackend.py and pass the 5.6 option check')

        for n in (a, b, c):
            dep = n.rpc.call('evm-deposit', {'amount': DEPOSIT_SAT[n.name]})
            assert dep['status'] == 'mined', dep
        wait_for(lambda: all(n.confirmed_sat() >= DEPOSIT_SAT[n.name] for n in (a, b, c)), 900, 'deposits')
        step('deposits to A, B and C confirmed')

        # Only neighbours connect: A-B, B-C, C-D. The three opens run at once.
        hops = [(a, b), (b, c), (c, d)]
        funded, errors = {}, []

        def open_one(x, y):
            try:
                x.rpc.connect(y.info['id'], '127.0.0.1', y.port)
                funded[x.name + y.name] = x.rpc.fundchannel(y.info['id'], CHANNEL_SAT)
            except Exception as e:     # noqa: B902
                errors.append('{}{}: {}'.format(x.name, y.name, e))
        threads = [threading.Thread(target=open_one, args=h) for h in hops]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        wait_for(lambda: all(x.chan(y)['state'] == 'CHANNELD_NORMAL' and y.chan(x)['state'] == 'CHANNELD_NORMAL'
                             for x, y in hops), 1800, 'three channels normal')
        scids = {k: None for k in funded}
        for x, y in hops:
            scids[x.name + y.name] = x.chan(y)['short_channel_id']
        step('channels A-B, B-C, C-D ({}) normal after 3 confirmations'.format(
            ', '.join('{} {}'.format(k, v) for k, v in scids.items())))

        # Gossip: every node sees all three channels in both directions, and
        # all four nodes with their aliases. D never talked to A or B.
        def full_view(n):
            chans = n.rpc.listchannels()['channels']
            aliases = sorted(x.get('alias') or '' for x in n.rpc.listnodes()['nodes'])
            return len(chans) == 6 and {ch['short_channel_id'] for ch in chans} == set(scids.values()) \
                and aliases == list(NAMES)
        wait_for(lambda: all(full_view(n) for n in nodes), 2400, 'full gossip view on all four', poll=10)
        ab = scids['AB']
        d_ab = d.rpc.listchannels(ab)['channels']
        step('gossip: all four see 3 channels x 2 directions and 4 nodes; D knows A-B ({}, {} sat) '
             'only through C and B'.format(ab, d_ab[0]['amount_msat'] // 1000),
             len(d_ab) == 2 and all(ch['amount_msat'] == CHANNEL_SAT * 1000 for ch in d_ab))
        res = funded['AB']
        step('D verifies the A-B funding output on chain (getutxout through its plugin)',
             d.rpc.call('getutxout', {'txid': res['txid'], 'vout': res['outnum']}) ==
             a.rpc.call('getutxout', {'txid': res['txid'], 'vout': res['outnum']}))

        route = a.rpc.getroutes(a.info['id'], d.info['id'], PAY_MSAT, layers=[], maxfee_msat=100_000, final_cltv=144)
        path = [h['short_channel_id_dir'].split('/')[0] for h in route['routes'][0]['path']]
        step('askrene finds A -> D over {}'.format(' -> '.join(path)), path == [scids['AB'], scids['BC'], scids['CD']])

        inv = d.rpc.invoice(PAY_MSAT, 'r4', 'A to D, three hops')['bolt11']
        r = a.rpc.pay(inv)
        fee = r['amount_sent_msat'] - r['amount_msat']
        step('A pays D 20,000 sat over three hops (fee {} msat)'.format(fee), r['status'] == 'complete' and fee > 0)

        wait_for(lambda: d.chan(c)['spendable_msat'] >= BACK_MSAT, 120, 'D can spend')
        inv = a.rpc.invoice(BACK_MSAT, 'r4back', 'D to A, three hops')['bolt11']
        r2 = d.rpc.pay(inv)
        step('D pays A 5,000 sat back over three hops', r2['status'] == 'complete')

        def settled(n):
            return [f for f in n.rpc.listforwards()['forwards'] if f['status'] == 'settled']
        wait_for(lambda: len(settled(b)) == 2 and len(settled(c)) == 2, 60, 'forwards settled')
        earned = {n.name: sum(f['fee_msat'] for f in settled(n)) for n in (b, c)}
        step('B and C each forwarded both payments (fees earned: B {} msat, C {} msat)'.format(
            earned['B'], earned['C']), all(v > 0 for v in earned.values()))

        for x, y in hops:
            x.rpc.close(y.info['id'])
        exit_coins(nodes)
        step('all three channels closed; coins exit and are withdrawn')

        results['rpc'] = {}
        for n in nodes:
            st = n.status()
            hours = (time.time() - st['rpc_calls_since']) / 3600
            cr = credits(st['rpc_calls'])
            results['rpc'][n.name] = {'calls': st['rpc_calls'], 'credits': cr, 'per_day': round(cr / hours * 24)}
            log('{}: {:,} credits/day ({:.0%} of the free tier)'.format(
                n.name, round(cr / hours * 24), cr / hours * 24 / FREE_TIER_PER_DAY))
        broken = [ln for n in nodes for ln in n.log_text().splitlines() if '**BROKEN**' in ln]
        results['broken'] = broken
        step('no BROKEN log lines', not broken, count=len(broken))
        results['passed'] = True
    finally:
        for n in nodes:
            n.stop()
        if nodes:
            if results.get('passed'):
                refund(rpc, nodes, deployer.address)
            else:
                log('the run failed: its nodes keep their ETH; free their coins with '
                    'smoke_base_sepolia.py --cleanup {} --contract {}'.format(args.workdir, contract))
        results['total_seconds'] = round(time.time() - t_start)
        with open(os.path.join(args.workdir, 'results.json'), 'w') as f:
            json.dump(results, f, indent=1)
        log('results in ' + os.path.join(args.workdir, 'results.json'))


if __name__ == '__main__':
    main()
