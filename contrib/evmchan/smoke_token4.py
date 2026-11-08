#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""ERC-20 token channels on Base Sepolia (SPEC 4.6): four unmodified
lightningd in a line, A - B - C - D, on UtxoChainToken for a 6-decimal test
token, plus E on the ETH UtxoChain, connected only to D.

- Deploys a mintable test token (contracts/test/Tokens.sol MockERC20, 6
  decimals: 1 unit per sat) and UtxoChainToken for it, unless given.
- The deployer gives each node ETH for gas and mints A, B and C tokens. The
  nodes deposit them (approve + deposit) and open A-B, B-C, C-D.
- Gossip: every token node sees all three channels and four nodes, and D
  learns A-B only through C and B and checks it with getutxout. E, on the
  other contract, gets the same announcements from D and must drop them,
  since the funding outputs aren't in its contract (SPEC open question 2).
- A pays D over three hops and D pays A back; B and C forward both.
- The channels close, the coins exit and are withdrawn as tokens. Every
  token balance must add up exactly, and the nodes' ETH goes back to the
  deployer.

The token contract and plugin live on the ERC-20 branch, so --tree points at
that checkout (its lightningd, evmbackend.py and contracts/):

    INFURA_API_KEY=... DEPLOYER_PRIVATE_KEY=0x... \\
    PATH=<venv>/bin:$PATH PYTHONPATH=<tree>/contrib/pyln-client:<tree>/contrib/pyln-proto \\
        caffeinate -i python3 smoke_token4.py --tree <erc20 checkout> --eth-contract 0x... \\
        [--token 0x... --contract 0x...] [--workdir /tmp/evk]

A failed run keeps its nodes' ETH and tokens: free them with
`smoke_base_sepolia.py --cleanup <workdir> --contract <token contract>` (the
ERC-20 tree's plugin is needed for that, via the same PYTHONPATH)."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import evm   # noqa: E402
from smoke_base_sepolia import (CHAIN_ID, RPC_TEMPLATE, INTERVAL, Node, exit_coins, log,   # noqa: E402
                                refund, wait_for)

NAMES = ('A', 'B', 'C', 'D')
GAS_WEI = {'A': 2 * 10**16, 'B': 2 * 10**16, 'C': 2 * 10**16, 'D': 12 * 10**15, 'E': 12 * 10**15}
DECIMALS = 6
MINT_UNITS = {'A': 5_000_000, 'B': 5_000_000, 'C': 5_000_000}     # 5 test dollars each
DEPOSIT_SAT = {'A': 3_000_000, 'B': 3_000_000, 'C': 3_000_000}    # 1 unit per sat
CHANNEL_SAT = 2_000_000
PAY_MSAT = 300_000_000           # A -> D: 300,000 units = 0.30 test dollars
BACK_MSAT = 100_000_000          # D -> A


def forge_create(contracts_dir, target, args, rpc_url_template, env):
    out = subprocess.run(['forge', 'create', target, '--rpc-url', rpc_url_template.replace(
        '${INFURA_API_KEY}', env['INFURA_API_KEY']), '--private-key', env['DEPLOYER_PRIVATE_KEY'],
        '--broadcast', '--json', '--constructor-args'] + args, cwd=contracts_dir, capture_output=True, text=True,
        env=env)
    m = re.search(r'"deployedTo":\s*"(0x[0-9a-fA-F]{40})"', out.stdout)
    if out.returncode or not m:
        # forge's stderr can echo the command line: print only its last line, scrubbed.
        tail = (out.stderr.strip().splitlines() or [''])[-1]
        sys.exit('forge create {} failed: {}'.format(target, tail.replace(env['INFURA_API_KEY'], '***')
                                                     .replace(env['DEPLOYER_PRIVATE_KEY'], '***')[:300]))
    return evm.checksum_address(m.group(1))


def token_balance(rpc, token, who):
    data = evm.calldata('balanceOf(address)', ['address'], [who])
    return evm.abi_decode(['uint256'], rpc.eth_call(token, data))[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tree', required=True, help='the ERC-20 branch checkout (lightningd, plugin, contracts)')
    ap.add_argument('--eth-contract', required=True, help='the ETH UtxoChain, for node E')
    ap.add_argument('--token')
    ap.add_argument('--contract', help='UtxoChainToken for --token')
    ap.add_argument('--workdir', default='/tmp/evk')
    args = ap.parse_args()
    key, dkey = os.environ.get('INFURA_API_KEY'), os.environ.get('DEPLOYER_PRIVATE_KEY')
    if not key or not dkey:
        sys.exit('INFURA_API_KEY and DEPLOYER_PRIVATE_KEY must be set')
    tree = os.path.abspath(args.tree)
    lightningd = os.path.join(tree, 'lightningd', 'lightningd')
    plugin = os.path.join(tree, 'contrib', 'evmchan', 'evmbackend.py')
    contracts_dir = os.path.join(tree, 'contrib', 'evmchan', 'contracts')
    rpc = evm.EvmRpc(RPC_TEMPLATE.replace('${INFURA_API_KEY}', key))
    if rpc.chain_id() != CHAIN_ID:
        sys.exit('not Base Sepolia')
    deployer = evm.Account(bytes.fromhex(dkey[2:] if dkey.startswith('0x') else dkey))
    env = dict(os.environ, PATH=os.path.expanduser('~/.foundry/bin') + ':' + os.environ['PATH'])

    shutil.rmtree(args.workdir, ignore_errors=True)
    os.makedirs(args.workdir)
    results = {'steps': []}
    t_last, t_start = [time.time()], time.time()

    def step(name, ok=True, **extra):
        now = time.time()
        results['steps'].append(dict(name=name, ok=bool(ok), seconds=round(now - t_last[0], 1), **extra))
        log('{} {:7.1f}s  {}'.format('PASS' if ok else 'FAIL', now - t_last[0], name))
        t_last[0] = now
        if not ok:
            raise AssertionError(name)

    token, contract = args.token, args.contract
    if not token:
        token = forge_create(contracts_dir, 'test/Tokens.sol:MockERC20', [str(DECIMALS)], RPC_TEMPLATE, env)
        log('test token ' + token)
    if not contract:
        out = subprocess.run([os.path.join(contracts_dir, 'deploy.sh'), '--rpc-url', RPC_TEMPLATE,
                              '--interval', str(INTERVAL), '--token', token], capture_output=True, text=True, env=env)
        contract = out.stdout.strip()
        if out.returncode or not re.fullmatch(r'0x[0-9a-fA-F]{40}', contract):
            sys.exit('deploy.sh --token failed: ' + (out.stderr.strip().splitlines() or [''])[-1][:300])
    token, contract = evm.checksum_address(token), evm.checksum_address(contract)
    results.update({'token': token, 'contract': contract, 'eth_contract': args.eth_contract})
    with open(os.path.join(os.path.dirname(args.workdir.rstrip('/')), 'evk-contracts'), 'w') as f:
        f.write('token {}\ncontract {}\n'.format(token, contract))
    log('token {}  UtxoChainToken {}'.format(token, contract))
    step('test token and UtxoChainToken deployed')

    nodes, e = [], None
    try:
        nodes = [Node(n, args.workdir, lightningd, contract, plugin=plugin) for n in NAMES]
        e = Node('E', args.workdir, lightningd, evm.checksum_address(args.eth_contract), plugin=plugin)
        a, b, c, d = nodes
        for n in nodes + [e]:
            rc = rpc.send(deployer, n.eth, value=GAS_WEI[n.name], timeout=180)
            assert int(rc['status'], 16) == 1
        for n in (a, b, c):
            rc = rpc.send(deployer, token, data=evm.calldata('mint(address,uint256)', ['address', 'uint256'],
                                                             [n.eth, MINT_UNITS[n.name]]), timeout=180)
            assert int(rc['status'], 16) == 1
        minted = sum(MINT_UNITS.values())
        base = token_balance(rpc, token, contract)     # an earlier run's reserves, if the contract is reused
        step('the deployer gives the five nodes ETH for gas and mints A, B and C {:,} units each'.format(
            MINT_UNITS['A']))

        for n in nodes + [e]:
            n.start()
        wait_for(lambda: all(n.status()['opens_refused'] is None for n in nodes + [e]), 300, 'opens allowed')
        st = a.status()
        assert st.get('token') == token and st.get('units_per_sat') == 1, st
        assert e.status().get('token') is None
        wait_for(lambda: a.status()['current_height'] >= 1, 2 * INTERVAL + 60, 'height 1')
        step('A-D start in token mode ({} unit per sat), E in ETH mode; all pass the 5.6 option check'.format(
            st['units_per_sat']))

        for n in (a, b, c):
            dep = n.rpc.call('evm-deposit', {'amount': DEPOSIT_SAT[n.name]})
            assert dep['status'] == 'mined', dep
        wait_for(lambda: all(n.confirmed_sat() >= DEPOSIT_SAT[n.name] for n in (a, b, c)), 900, 'deposits')
        held = token_balance(rpc, token, contract) - base
        step('A, B and C deposit {:,} units each (approve + deposit); the contract holds {:,} more units'.format(
            DEPOSIT_SAT['A'], held), held == sum(DEPOSIT_SAT.values()))

        e.rpc.connect(d.info['id'], '127.0.0.1', d.port)
        hops = [(a, b), (b, c), (c, d)]
        funded, errors = {}, []

        def open_one(x, y):
            try:
                x.rpc.connect(y.info['id'], '127.0.0.1', y.port)
                funded[x.name + y.name] = x.rpc.fundchannel(y.info['id'], CHANNEL_SAT)
            except Exception as ex:     # noqa: B902
                errors.append('{}{}: {}'.format(x.name, y.name, ex))
        threads = [threading.Thread(target=open_one, args=h) for h in hops]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        wait_for(lambda: all(x.chan(y)['state'] == 'CHANNELD_NORMAL' and y.chan(x)['state'] == 'CHANNELD_NORMAL'
                             for x, y in hops), 1800, 'three channels normal')
        scids = {x.name + y.name: x.chan(y)['short_channel_id'] for x, y in hops}
        step('token channels A-B, B-C, C-D ({:,} units each) normal: {}'.format(
            CHANNEL_SAT, ', '.join('{} {}'.format(k, v) for k, v in scids.items())))

        def full_view(n):
            chans = n.rpc.listchannels()['channels']
            aliases = sorted(x.get('alias') or '' for x in n.rpc.listnodes()['nodes'] if x.get('alias') in NAMES)
            return len(chans) == 6 and {ch['short_channel_id'] for ch in chans} == set(scids.values()) \
                and aliases == list(NAMES)
        wait_for(lambda: all(full_view(n) for n in nodes), 2400, 'full gossip view on A-D', poll=10)
        d_ab = d.rpc.listchannels(scids['AB'])['channels']
        step('gossip: A-D see 3 channels x 2 directions and 4 nodes; D knows A-B ({:,} sat) only through C and B'
             .format(d_ab[0]['amount_msat'] // 1000),
             len(d_ab) == 2 and all(ch['amount_msat'] == CHANNEL_SAT * 1000 for ch in d_ab))
        res = funded['AB']
        got = d.rpc.call('getutxout', {'txid': res['txid'], 'vout': res['outnum']})
        step('D verifies the A-B funding output in the token contract (getutxout: {} sat)'.format(
            got.get('amount')), got == a.rpc.call('getutxout', {'txid': res['txid'], 'vout': res['outnum']}))
        # E (ETH contract) got the same announcements from D: none verify there.
        e_seen = e.rpc.call('getutxout', {'txid': res['txid'], 'vout': res['outnum']})
        step('E, on the ETH contract, drops all token-channel gossip (listchannels {}, A-B funding output '
             'unknown there: {})'.format(len(e.rpc.listchannels()['channels']), e_seen),
             e.rpc.listchannels()['channels'] == [] and not e_seen.get('amount'))

        route = a.rpc.getroutes(a.info['id'], d.info['id'], PAY_MSAT, layers=[], maxfee_msat=1_000_000,
                                final_cltv=144)
        path = [h['short_channel_id_dir'].split('/')[0] for h in route['routes'][0]['path']]
        step('askrene finds A -> D over {}'.format(' -> '.join(path)), path == [scids['AB'], scids['BC'], scids['CD']])
        inv = d.rpc.invoice(PAY_MSAT, 'k4', 'A to D in tokens')['bolt11']
        r = a.rpc.pay(inv)
        step('A pays D {:,} units over three hops (fee {} msat)'.format(PAY_MSAT // 1000,
             r['amount_sent_msat'] - r['amount_msat']), r['status'] == 'complete')
        wait_for(lambda: d.chan(c)['spendable_msat'] >= BACK_MSAT, 120, 'D can spend')
        r2 = d.rpc.pay(a.rpc.invoice(BACK_MSAT, 'k4back', 'D to A in tokens')['bolt11'])
        step('D pays A {:,} units back over three hops'.format(BACK_MSAT // 1000), r2['status'] == 'complete')

        def settled(n):
            return [f for f in n.rpc.listforwards()['forwards'] if f['status'] == 'settled']
        wait_for(lambda: len(settled(b)) == 2 and len(settled(c)) == 2, 60, 'forwards settled')
        step('B and C each forwarded both payments (fees B {} msat, C {} msat)'.format(
            sum(f['fee_msat'] for f in settled(b)), sum(f['fee_msat'] for f in settled(c))))

        for x, y in hops:
            x.rpc.close(y.info['id'])
        exit_coins(nodes)
        # Exact accounting: the contract holds what the wallets still have
        # (CLN's emergency reserves), every other unit went back as tokens.

        def balances():
            on_nodes = sum(token_balance(rpc, token, n.eth) for n in nodes)
            return on_nodes, token_balance(rpc, token, contract) - base, sum(n.confirmed_sat() for n in nodes)
        wait_for(lambda: balances()[0] + balances()[1] == minted and all(
            n.status().get('withdrawable_units', 0) == 0 for n in nodes), 600, 'tokens withdrawn')
        # CLN marks the exits' inputs spent only once they are under `safe`.
        wait_for(lambda: balances()[1] == balances()[2], 900, 'wallets match the contract')
        on_nodes, held, wallets = balances()
        results['tokens'] = {'minted': minted, 'on_nodes': on_nodes, 'in_contract': held, 'in_wallets_sat': wallets}
        step('closed, exited and withdrawn: {:,} units back with the nodes, {:,} in the contract = the {:,} sat '
             'still in wallets (emergency reserves); {:,} minted'.format(on_nodes, held, wallets, minted),
             on_nodes + held == minted and held == wallets)

        broken = [ln for n in nodes + [e] for ln in n.log_text().splitlines() if '**BROKEN**' in ln]
        results['broken'] = broken
        step('no BROKEN log lines', not broken, count=len(broken))
        results['passed'] = True
    finally:
        everyone = nodes + ([e] if e else [])
        for n in everyone:
            n.stop()
        if everyone:
            if results.get('passed'):
                refund(rpc, everyone, deployer.address)
            else:
                log('the run failed: its nodes keep their ETH and tokens; free them with '
                    'smoke_base_sepolia.py --cleanup {} --contract {}'.format(args.workdir, contract))
        results['total_seconds'] = round(time.time() - t_start)
        with open(os.path.join(args.workdir, 'results.json'), 'w') as f:
            json.dump(results, f, indent=1)
        log('results in ' + os.path.join(args.workdir, 'results.json'))


if __name__ == '__main__':
    main()
