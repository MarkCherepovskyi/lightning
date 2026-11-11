from tn import *
log('waiting for gossip: 2 announced channels = 4 directions')
def pub():
    return [c for c in ln('alice','listchannels')['channels'] if c.get('liquid_asset')==ASSET and c['public']]
try:
    wait('gossip', lambda: len(pub()) >= 4, timeout=1500, every=20)
except TimeoutError:
    pass
chs = pub()
log('alice gossip map:', len(chs), 'directions')
for c in sorted(chs, key=lambda c:(c['short_channel_id'], c['direction'])):
    log('  ', c['short_channel_id'], m.node_name(c['source']), '->', m.node_name(c['destination']), c['amount_msat']//1000)
amt = 50_000_000
inv = ln('carol','invoice', amount_msat=amt, label=f't10-{int(time.time())}', description='t10', asset=ASSET)
res = ln('alice','xpay', invstring=inv['bolt11'], asset=ASSET)
log('xpay result', {k: res[k] for k in ('amount_msat','amount_sent_msat','successful_parts') if k in res})
log('DONE')
