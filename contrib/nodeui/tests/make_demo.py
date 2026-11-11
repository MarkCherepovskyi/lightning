"""Writes ui/demo.json (the page's ?demo data) from the test fakes:
a Bitcoin regtest pair, a Liquid pair with an asset channel, an EVM ETH set
on anvil and an EVM token set.

    PYTHONPATH=contrib/pyln-client:contrib/pyln-proto python3 contrib/nodeui/tests/make_demo.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fakes  # noqa: E402
import ui  # noqa: E402
from fakes import ASSET, CONTRACT, TOKEN, connect  # noqa: E402

T = 1794100000
a, b = fakes.bitcoin(1), fakes.bitcoin(2)
connect(a, b, '3f' * 32, to_us=740_000, total=1_000_000, scid='112x1x0')
a.r['listchannels'] = {'channels': [
    {'short_channel_id': '112x1x0', 'source': a.id, 'destination': b.id, 'amount_msat': 10**9},
    {'short_channel_id': '120x1x0', 'source': b.id, 'destination': '03' + '7a' * 32, 'amount_msat': 5 * 10**8}]}
c, d = fakes.liquid(3), fakes.liquid(4)
connect(c, d, 'a43d57113597e25b773c94de882c6d857bb8edb589689abd598b491f57db544d', asset=ASSET,
        to_us=930_000, total=1_000_000, scid='2690367x1x0')
connect(c, d, '5b' * 32, to_us=180_000, total=200_000, scid='2690380x1x0')
c.r['assetchan-sweeps'] = {'sweeps': [{
    'outpoint': '9e' * 32 + ':2', 'channel_id': '77' * 32, 'asset': ASSET,
    'minblock': 2690400, 'deadline': 2690412, 'txid': None, 'fee': None}]}
e, f = fakes.evm(5), fakes.evm(6)
e.status.update(withdrawable_wei=100_001 * ui.SAT_WEI, balance_wei=99_987_654_321_000_000_000)
f.status['eth_address'] = '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC'
connect(e, f, 'c1' * 32, to_us=450_000, total=500_000, scid='31x1x0')
connect(e, f, 'c2' * 32, to_us=300_000, total=300_000, scid='18x1x0', state='ONCHAIN',
        status=['ONCHAIN:1 outputs unresolved: in 23 blocks will spend DELAYED_OUTPUT_TO_US (aa…:0) using OUR_DELAYED_RETURN_TO_WALLET'])
g, h = fakes.evm(7, chain_id=84532, contract=CONTRACT, token=TOKEN), fakes.evm(8, chain_id=84532, contract=CONTRACT, token=TOKEN)
for x in (g, h):
    x.status.update(rpc_calls={'eth_call': 2900, 'eth_getBlockByNumber': 5800, 'eth_getLogs': 960, 'eth_chainId': 3,
                               'eth_blockNumber': 2900}, rpc_calls_since=T - 6 * 3600, tag_age_s=14.2, lag=2,
                    tip=4410211, current_height=4410213, interval=60)
h.status.update(balance_wei=6 * 10**15, eth_address='0x90F79bf6EB2c4f870365E785982E1f101E93b906',
                opens_refused="the node's ETH balance is below evm-min-balance",
                alarms={'balance': 'ETH balance 0.006 below evm-min-balance 0.01'},
                pending_sends=[{'kind': 'withdraw', 'txid': '4e' * 32, 'nonce': 17, 'evm_txs': ['0x' + '12' * 32],
                                'age_s': 41.0, 'replacements': 1, 'max_fee_per_gas': 1500000}])
g.status['withdrawable_units'] = 250_000
connect(g, h, 'd4' * 32, to_us=60_000, total=100_000, scid='4410100x1x0')
for x, alias in zip((a, b, c, d, e, f, g, h), ('alice', 'bob', 'carol', 'dave', 'erin', 'frank', 'gina', 'hank')):
    x.r['getinfo']['alias'] = alias
    x.r['getinfo']['blockheight'] = {'alice': 112, 'bob': 112, 'carol': 2690766, 'dave': 2690766, 'erin': 131,
                                     'frank': 131, 'gina': 4410211, 'hank': 4410211}[alias]
# An open on Base Sepolia, 2 of 3 confirmations; a deposit CLN hasn't reached; frank left with the reserve.
for x in (g, h):
    x.r['listconfigs']['configs']['funding-confirms'] = fakes.conf(3)
connect(g, h, 'd5' * 32, to_us=200_000, total=200_000, scid='4410210x1x0', state='CHANNELD_AWAITING_LOCKIN')
f.r['listfunds']['outputs'] = [{'txid': '7f' * 32, 'output': 1, 'amount_msat': 25_000_000, 'status': 'confirmed'}]
e.status['balance_wei'] = 15_997_549_833_998_572
app = fakes.make_app({'alice': a, 'bob': b, 'carol': c, 'dave': d, 'erin': e, 'frank': f, 'gina': g, 'hank': h},
                     labels={ASSET: 'TEST', TOKEN: 'USDC'})
app.track_deposit('gina', '5d' * 32, 4410219, 1_000_000)
app.poll_once()
st = app.state
for nd in st['nodes']:
    if nd.get('evm') and 'credits_hours' in nd['evm'] and nd['evm']['anvil']:
        nd['evm'].update(credits_hours=0.5)
st['now'] = T
LOG = ['funding tx a43d57113597e25b773c94de882c6d857bb8edb589689abd598b491f57db544d:0, 2000 sat L-BTC fee reserve next to the asset',
       'liquid:regtest/TEST channel carol -> dave ready: 1,000,000 units (2690367x1x0)']


def job(i, title, net, status, started, ended, log=(), nodes=(), **kw):
    return dict({'id': i, 'title': title, 'net': net, 'nodes': list(nodes), 'status': status, 'queued': started,
                 'started': started, 'ended': ended, 'log': list(log), 'result': {}, 'cancelling': False}, **kw)


EVM_NET = 'evm:84532:' + CONTRACT
st['jobs'] = [job(6, 'Deposit 500,000 sat to gina (evm:84532:0xB820…940E/USDC)', EVM_NET, 'running', T - 25, None,
                  ['deposited 500,000 sat (50,000,000 USDC units) to gina: EVM tx 0x5d…, pending'], ('gina',)),
              job(7, 'Exit all sat from hank to its ETH address', EVM_NET, 'waiting', T - 5, None, (), ('hank',),
                  waiting_for=['Deposit 500,000 sat to gina (evm:84532:0xB820…940E/USDC)'])]
st['jobs'][1]['started'] = None
st['history'] = [job(5, 'Open liquid:regtest/TEST channel carol to dave, 1,000,000 units', 'liquid:regtest', 'done',
                     T - 40, T - 12, LOG, ('carol', 'dave')),
                 job(3, 'Deposit 1,000,000 sat to erin (evm:31337:0x5FbDB2315678afecb367f032d93F642f64180aa3/ETH)',
                     'evm:31337:' + fakes.ANVIL_CONTRACT, 'done', T - 300, T - 290, ['erin sees it in its wallet'],
                     ('erin',)),
                 job(2, 'Pay 50,000 sat from gina to hank (evm:84532:0xB820…940E/USDC)', EVM_NET, 'error', T - 400,
                     T - 400, ['error: gina: xpay: no route'], ('gina', 'hank')),
                 job(1, 'Mine 6 on btc:regtest', 'btc:regtest', 'done', T - 500, T - 499, ['mined 6 blocks on btc:regtest'])]
with open(os.path.join(HERE, '..', 'ui', 'demo.json'), 'w') as f:
    json.dump(st, f, indent=1)
    f.write('\n')
