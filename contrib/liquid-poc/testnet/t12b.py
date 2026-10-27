from tn import *
import subprocess
LN = HERE + '/lnpoc.py'
env = dict(os.environ, LNPOC_NET='testnet')
log('open asset channel alice -> bob')
r = subprocess.run(['python3', '-u', LN, 'open-asset', 'alice', 'bob', '500000', '--fee-reserve', '6000', '--change', '2000'],
                   capture_output=True, text=True, env=env)
log(r.stdout.strip().splitlines()[-2:] if r.stdout else r.stderr[-300:])
c = wait('channel', lambda: [x for x in ln('alice','listpeerchannels',id=m.node_id('bob'))['channels']
                             if x['state']=='CHANNELD_NORMAL' and x['total_msat']==500000000], timeout=300)[0]
cid, funding = c['channel_id'], c['funding_txid']
log('channel', link(funding))
# a payment so bob has a balance (a to_remote output in alice's commitment)
amt = 100_000_000
inv = ln('bob','invoice', amount_msat=amt, label=f't12b-{int(time.time())}', description='t12b', asset=ASSET)
final = ln('bob','decode', string=inv['bolt11'])['min_final_cltv_expiry']
path, route = m.find_route('alice','bob',amt, ASSET, final)
ln('alice','sendpay', route=route, payment_hash=inv['payment_hash'], payment_secret=inv['payment_secret'], amount_msat=amt)
log('payment', ln('alice','waitsendpay', payment_hash=inv['payment_hash'])['status'])
wait('settled', lambda: not htlcs_of('alice', cid), timeout=60, every=2)
# alice force-closes while bob is offline
dc('stop', 'bob')
ln('alice','close', id=cid, unilateraltimeout=1)
commit = wait('commitment tx', lambda: (outspend(funding, 0) or {}).get('txid'), timeout=600, every=10)
log('commitment tx', link(commit))
wait('commit confirmed', lambda: (esplora(f'tx/{commit}') or {}).get('status', {}).get('confirmed'), timeout=900, every=15)
ctx = esplora(f'tx/{commit}')
for i, v in enumerate(ctx['vout']):
    log('  out', i, v.get('asset', '')[:8], v.get('value'), v.get('scriptpubkey_type'))
dc('start', 'bob')
wait('bob up', lambda: ln('bob','getinfo'), timeout=180, every=3)
asset_outs = [i for i, v in enumerate(ctx['vout']) if v.get('asset','').startswith('723376e4')]
wait('both swept', lambda: all(outspend(commit, i) for i in asset_outs), timeout=1500, every=20)
for i in range(len(ctx['vout'])):
    s = outspend(commit, i)
    if s:
        tx = esplora(f'tx/{s["txid"]}')
        log(f'commit output {i} spent by {link(s["txid"])} confirmed={tx["status"].get("confirmed")} outs={[(v.get("asset","")[:8], v.get("value")) for v in tx["vout"]]}')
wait_blocks(10, 'for confirmations and the CSV')
for node in ('alice','bob'):
    try:
        accts = ln(node,'bkpr-listbalances')['accounts']
        log(node, 'bkpr', [(a['account'][:8], [b['balance_msat']//1000 for b in a['balances']]) for a in accts])
    except Exception as e:
        log(node, 'bkpr FAILED', str(e)[:200])
log('DONE')
