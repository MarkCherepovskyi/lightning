"""T9: revoked commitment.  bob (opener) broadcasts an old commitment from a
restored database while carol is offline; carol comes back and penalizes.
usage: t9.py FUNDING_PREFIX   (channel bob -> carol)"""
import sys
from tn import *

prefix = ARGV[0]
DB = '/data/liquid-testnet/lightningd.sqlite3'
py = lambda node, code: dc('run', '--rm', '--no-deps', '-T', '--entrypoint', 'python3', node, '-c', code)

c = chan('bob', 'carol', funding_prefix=prefix)
cid, funding = c['channel_id'], c['funding_txid']
log('channel bob->carol', link(funding), 'bob ours', c['to_us_msat'] // 1000)

# 1. backup bob's database (the old state)
dc('stop', 'bob')
py('bob', f"import shutil;shutil.copy('{DB}','/data/old.sqlite3')")
dc('start', 'bob')
wait('bob up', lambda: ln('bob', 'getinfo'), timeout=180, every=3)
sync_nodes(['bob'])
m.connect('bob', 'carol')

# 2. move the channel state forward: bob pays carol
amt = 100_000_000
inv = ln('carol', 'invoice', amount_msat=amt, label=f't9-{int(time.time())}', description='t9', asset=ASSET)
final = ln('carol', 'decode', string=inv['bolt11'])['min_final_cltv_expiry']
path, route = m.find_route('bob', 'carol', amt, ASSET, final)
ln('bob', 'sendpay', route=route, payment_hash=inv['payment_hash'],
   payment_secret=inv['payment_secret'], amount_msat=amt)
res = ln('bob', 'waitsendpay', payment_hash=inv['payment_hash'])
log('payment', res['status'])
wait('settled', lambda: not htlcs_of('bob', cid) and not htlcs_of('carol', cid), timeout=120, every=2)

# 3. carol offline; bob goes back to the old state and drops that commitment
dc('stop', 'bob')
dc('stop', 'carol')
py('bob', f"import shutil;shutil.copy('/data/old.sqlite3','{DB}')")
dc('start', 'bob')
wait('bob up', lambda: ln('bob', 'getinfo'), timeout=180, every=3)
log('bob restored to the old state; dropping its old commitment')
log(ln('bob', 'dev-fail', id=m.node_id('carol')))

commit = wait('old commitment tx', lambda: (outspend(funding, 0) or {}).get('txid'), timeout=300, every=5)
log('revoked commitment tx', link(commit))
# bob must not sweep before carol punishes
dc('stop', 'bob')
wait('commitment confirmed', lambda: (esplora(f'tx/{commit}') or {}).get('status', {}).get('confirmed'), timeout=600, every=10)
ctx = esplora(f'tx/{commit}')
log('confirmed in block', ctx['status']['block_height'], 'now', height())
for i, v in enumerate(ctx['vout']):
    log('  out', i, v.get('asset', '')[:8], v.get('value'), v.get('scriptpubkey_type'))

# 4. carol comes back inside bob's to_self_delay and takes everything
dc('start', 'carol')
wait('carol up', lambda: ln('carol', 'getinfo'), timeout=180, every=3)
log('carol back at block', height())
asset_outs = [i for i, v in enumerate(ctx['vout']) if v.get('asset', '').startswith('723376e4')]
try:
    wait('penalty', lambda: all(outspend(commit, i) for i in asset_outs), timeout=900, every=10)
except TimeoutError:
    pass
for i in range(len(ctx['vout'])):
    s = outspend(commit, i)
    if s:
        tx = esplora(f'tx/{s["txid"]}')
        log(f'commit output {i} spent by {link(s["txid"])} confirmed={tx["status"].get("confirmed")}'
            f' outs={[(v.get("asset","")[:8], v.get("value")) for v in tx["vout"]]}')
wait_blocks(2, 'for confirmation')
log('carol asset outputs', [(o['amount'], o['status']) for o in ln('carol', 'listassetfunds')['outputs'] if o['asset'] == ASSET])
log('DONE')
