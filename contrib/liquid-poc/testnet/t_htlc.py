"""HTLC in flight on a testnet asset channel, then a force close.

usage: t_htlc.py PAYER PAYEE FUNDING_PREFIX CLOSER(payer|payee) [restart_closer]
The payee holds its fulfill (dev_disconnect), the other side goes offline
when the HTLC is committed, then CLOSER force-closes.
"""
import sys
from tn import *

payer, payee, prefix, closer = ARGV[:4]
restart = len(ARGV) > 4
closing, other = (payer, payee) if closer == 'payer' else (payee, payer)

c = chan(payer, payee, funding_prefix=prefix)
cid, funding = c['channel_id'], c['funding_txid']
log(f'channel {payer}->{payee} funding {link(funding)}')
before_units = {n: sum(o['amount'] for o in ln(n, 'listassetfunds')['outputs']
                       if o['status'] == 'confirmed' and o['asset'] == ASSET) for n in (payer, payee)}

delay_htlcs(payee)
m.connect(payer, payee)
amt = 100_000_000
inv = ln(payee, 'invoice', amount_msat=amt, label=f'htlc-{int(time.time())}', description='htlc', asset=ASSET)
final = ln(payee, 'decode', string=inv['bolt11'])['min_final_cltv_expiry']
path, route = m.find_route(payer, payee, amt, ASSET, final)
log('sending', amt // 1000, 'units', ' -> '.join(path))
ln(payer, 'sendpay', route=route, payment_hash=inv['payment_hash'],
   payment_secret=inv['payment_secret'], amount_msat=amt)
h = wait('htlc committed', lambda: [x for x in htlcs_of(payer, cid)
                                    if 'ACK_REVOCATION' in x['state'] or x['state'].endswith('REVOCATION')],
         timeout=60, every=0.2)
expiry = h[0]['expiry']
log('HTLC committed, expiry block', expiry, 'now', height())
# The other side goes away; the closing side keeps the HTLC in its commitment.
dc('stop', other)
if closer == 'payee':
    log('waiting for the payee to release the HTLC (it learns the preimage)')
    time.sleep(45)
res = ln(closing, 'close', id=cid, unilateraltimeout=1)
log('close type', res.get('type'))

commit = wait('commitment tx', lambda: (outspend(funding, 0) or {}).get('txid'), timeout=600, every=10)
log('commitment tx', link(commit))
wait('commitment confirmed', lambda: (esplora(f'tx/{commit}') or {}).get('status', {}).get('confirmed'), timeout=900, every=15)
ctx = esplora(f'tx/{commit}')
log('commitment confirmed in block', ctx['status']['block_height'])
for i, v in enumerate(ctx['vout']):
    log('  out', i, v.get('asset', '')[:8], v.get('value'), v.get('scriptpubkey_type'))

if restart:
    log(f'restarting {closing} while the HTLC is unresolved (T11)')
    dc('restart', closing)
    wait(closing, lambda: ln(closing, 'getinfo'), timeout=180, every=5)

if closer == 'payer':
    # Offered HTLC: it can only be taken back after the expiry.
    wait_blocks(max(1, expiry + 2 - height()), 'for the HTLC to expire')
else:
    # Received HTLC with the preimage: claimed right away.
    wait_blocks(2, 'for the HTLC-success tx')

def spent_outputs():
    out = {}
    for i in range(len(ctx['vout'])):
        s = outspend(commit, i)
        if s:
            out[i] = s['txid']
    return out

# wait until every asset output of the commitment tx is spent (HTLC resolved)
asset_outs = [i for i, v in enumerate(ctx['vout']) if v.get('asset', '').startswith('723376e4')]
try:
    wait('commit outputs spent', lambda: all(i in spent_outputs() for i in asset_outs), timeout=2400, every=20)
except TimeoutError:
    log('timed out; spent so far', spent_outputs())
sp = spent_outputs()
for i, t in sp.items():
    tx = esplora(f'tx/{t}')
    log(f'commit output {i} spent by {link(t)} confirmed={tx["status"].get("confirmed")}'
        f' outs={[(v.get("asset","")[:8], v.get("value")) for v in tx["vout"]]}')
    # the HTLC tx output gets its own sweep after the delay
wait_blocks(8, 'for the second-stage sweep (to_self_delay)')
for node in (payer, payee):
    try:
        outs = [(o['amount'], o['status']) for o in ln(node, 'listassetfunds')['outputs'] if o['asset'] == ASSET]
        log(node, 'asset outputs', outs)
    except Exception as e:
        log(node, 'offline', str(e)[:60])
log('before', before_units)
log('DONE')
