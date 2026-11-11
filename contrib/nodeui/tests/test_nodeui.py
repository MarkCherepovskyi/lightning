"""Unit tests of contrib/nodeui/ui.py, on fake RPC objects (no daemons).

    PYTHONPATH=contrib/pyln-client:contrib/pyln-proto python3 -m pytest contrib/nodeui/tests
"""
import http.client
import json
import threading
import time

import pytest

import fakes
from fakes import ASSET, CONTRACT, OTHER_ASSET, TOKEN, connect, make_app
import ui


def body_job(app, **body):
    return ui.build_job(app, body)


def run(app, body):
    """Builds the job and runs it, waiting for its end; returns (status, log, result)."""
    title, net, fn, nodes = ui.build_job(app, body)
    job = app.jobs.get(app.jobs.start(title, net, fn, nodes, wait=True))
    return job['status'], job['log'], job['result']


def wait_until(cond, timeout=5):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, 'timed out'
        time.sleep(0.02)


def node_of(app, name):
    return next(x for x in app.state['nodes'] if x['name'] == name)


# ---------------------------------------------------------------- detection
def test_detect_kinds():
    app = make_app({'b': fakes.bitcoin(1), 'l': fakes.liquid(2), 'e': fakes.evm(3),
                    'plain-liquid': fakes.liquid(4, 'liquid-testnet', plugin=False)})
    kinds = {x['name']: x['kind'] for x in app.state['nodes']}
    assert kinds == {'b': 'bitcoin', 'l': 'liquid', 'e': 'evm', 'plain-liquid': 'liquid'}
    # The EVM node runs on CLN's regtest, but evm-status wins.
    assert app.nodes['e'].network == 'regtest'


def test_detect_kind_function():
    assert ui.detect_kind({'evm-status', 'assetchan-open'}, 'liquid-regtest') == 'evm'
    assert ui.detect_kind({'assetchan-open'}, 'regtest') == 'liquid'
    assert ui.detect_kind(set(), 'liquid') == 'liquid'
    assert ui.detect_kind({'getinfo'}, 'signet') == 'bitcoin'


def test_down_node_does_not_break_the_page_and_is_detected_again():
    a, b = fakes.bitcoin(1), fakes.evm(2)
    b.down = True
    app = make_app({'a': a, 'b': b})
    down = node_of(app, 'b')
    assert down['ok'] is False and 'not reachable' in down['error']
    assert node_of(app, 'a')['ok'] is True
    assert [n['id'] for n in app.state['networks']] == ['btc:regtest']
    with pytest.raises(ui.BadRequest, match='down'):
        body_job(app, action='deposit', node='b', amount=1000)
    b.down = False
    app.poll_once()
    assert node_of(app, 'b')['kind'] == 'evm' and node_of(app, 'b')['ok']
    helps = len(b.called('help'))
    app.poll_once()
    assert len(b.called('help')) == helps, 'detects once, not on every poll'
    b.down = True
    app.poll_once()
    b.down = False
    app.poll_once()
    assert len(b.called('help')) == helps + 1, 'detects again on reconnect'


def test_evm_node_whose_status_fails_is_detected_again():
    e = fakes.evm(1)
    e.r['evm-status'] = ui.RpcError('evm-status', {}, {'code': -1, 'message': 'backend not ready'})
    app = make_app({'e': e})
    assert app.nodes['e'].kind == 'evm' and not app.nodes['e'].detected
    assert app.nodes['e'].net.startswith('evm:?:')
    e.r['evm-status'] = lambda p: dict(e.status)
    app.poll_once()
    assert app.nodes['e'].detected and app.nodes['e'].net == 'evm:31337:' + fakes.ANVIL_CONTRACT


# ---------------------------------------------------------------- network IDs
def test_network_ids():
    app = make_app({'b': fakes.bitcoin(1, 'testnet'), 'l': fakes.liquid(2, 'liquid-testnet'),
                    'e': fakes.evm(3, chain_id=84532, contract=CONTRACT), 'r': fakes.bitcoin(4),
                    's': fakes.bitcoin(5, 'signet')})
    nets = {x['name']: x['net'] for x in app.state['nodes']}
    assert nets == {'b': 'btc:testnet', 'l': 'liquid:testnet', 'e': 'evm:84532:' + CONTRACT,
                    'r': 'btc:regtest', 's': 'btc:signet'}
    assert ui.short_net_id('evm:84532:' + CONTRACT) == 'evm:84532:0xB820…940E'
    assert ui.short_net_id('evm:84532:' + CONTRACT + '/ETH') == 'evm:84532:0xB820…940E/ETH'
    titles = {x['id']: x['title'] for x in app.state['networks']}
    assert '0f9188f1…466e2206' in titles['btc:regtest']      # the chain hash, short
    assert 'd77f4943' in titles['btc:testnet']
    assert ui.node_net_id('liquid', 'liquid') == 'liquid:mainnet'
    assert ui.node_net_id('liquid', 'liquid-regtest') == 'liquid:regtest'


def test_channel_network_ids():
    l1, l2 = fakes.liquid(1), fakes.liquid(2)
    connect(l1, l2, 'aa' * 32, asset=ASSET)
    connect(l1, l2, 'bb' * 32, scid='104x1x0')
    e1, e2 = fakes.evm(3), fakes.evm(4)
    connect(e1, e2, 'cc' * 32)
    t1, t2 = fakes.evm(5, contract=CONTRACT, token=TOKEN), fakes.evm(6, contract=CONTRACT, token=TOKEN)
    connect(t1, t2, 'dd' * 32)
    app = make_app({'l1': l1, 'l2': l2, 'e1': e1, 'e2': e2, 't1': t1, 't2': t2})
    nets = {c['id']: c['net'] for c in app.state['channels']}
    assert nets['aa' * 32] == 'liquid:regtest/ddc8da3f'
    assert nets['bb' * 32] == 'liquid:regtest/L-BTC'
    assert nets['cc' * 32] == 'evm:31337:{}/ETH'.format(fakes.ANVIL_CONTRACT)
    assert nets['dd' * 32] == 'evm:31337:{}/{}'.format(CONTRACT, TOKEN[:8])
    assert len(app.state['channels']) == 4, 'each channel once, though both sides list it'
    app2 = make_app({'l1': fakes.liquid(1), 't1': fakes.evm(5, contract=CONTRACT, token=TOKEN)},
                    labels={ASSET: 'TEST', TOKEN: 'USDC'})
    assert app2.channel_net(app2.nodes['l1'], ASSET) == 'liquid:regtest/TEST'
    assert app2.channel_net(app2.nodes['t1']) == 'evm:31337:{}/USDC'.format(CONTRACT)


def test_switcher_lists_every_network_and_maps():
    e1, e2 = fakes.evm(3), fakes.evm(4)
    connect(e1, e2, 'cc' * 32)
    e1.r['listchannels'] = {'channels': [{'short_channel_id': '103x1x0', 'source': e1.id, 'destination': e2.id,
                                          'amount_msat': 10**9},
                                         {'short_channel_id': '200x1x0', 'source': e2.id,
                                          'destination': '03' + 'ab' * 32, 'amount_msat': 10**9}]}
    app = make_app({'b1': fakes.bitcoin(1), 'b2': fakes.bitcoin(2), 'e1': e1, 'e2': e2})
    assert [x['id'] for x in app.state['networks']] == ['btc:regtest', 'evm:31337:' + fakes.ANVIL_CONTRACT]
    m = app.state['maps']['evm:31337:' + fakes.ANVIL_CONTRACT]
    assert {x['name'] for x in m['nodes']} == {'e1', 'e2', '03ababab'}
    assert sum(e['local'] for e in m['edges']) == 1 and len(m['edges']) == 2
    assert app.state['maps']['btc:regtest']['edges'] == []


# ---------------------------------------------------------------- cross-network refusals
def mixed_app():
    b1, b2, e1, l1, l2 = fakes.bitcoin(1), fakes.bitcoin(2), fakes.evm(3), fakes.liquid(4), \
        fakes.liquid(5, assets=(OTHER_ASSET,))
    return make_app({'b1': b1, 'b2': b2, 'e1': e1, 'l1': l1, 'l2': l2}), (b1, b2, e1, l1, l2)


def test_open_across_networks_is_refused():
    app, _ = mixed_app()
    with pytest.raises(ui.BadRequest, match='same network'):
        body_job(app, action='open', frm='b1', to='e1', amount=100000)
    with pytest.raises(ui.BadRequest, match='same network'):
        body_job(app, action='open', frm='l1', to='b1', amount=100000)
    with pytest.raises(ui.BadRequest, match='different'):
        body_job(app, action='open', frm='b1', to='b1', amount=100000)
    title, net, _, _ = body_job(app, action='open', frm='b1', to='b2', amount=100000)
    assert net == 'btc:regtest' and 'b1 to b2' in title


def test_liquid_asset_must_be_allowed_by_both():
    app, _ = mixed_app()
    with pytest.raises(ui.BadRequest, match='l2 does not allow'):
        body_job(app, action='open', frm='l1', to='l2', amount=1000, asset=ASSET)
    with pytest.raises(ui.BadRequest, match='l1 does not allow'):
        body_job(app, action='open', frm='l1', to='l2', amount=1000, asset=OTHER_ASSET)
    with pytest.raises(ui.BadRequest, match='only Liquid'):
        body_job(app, action='open', frm='b1', to='b2', amount=1000, asset=ASSET)
    with pytest.raises(ui.BadRequest, match='not an asset id'):
        body_job(app, action='invoice', node='l1', amount=1000, asset='zz')
    title, net, _, _ = body_job(app, action='open', frm='l1', to='l2', amount=1000, asset='lbtc')
    assert 'liquid:regtest/L-BTC' in title


def test_pay_across_networks_is_refused():
    app, (b1, b2, e1, l1, l2) = mixed_app()
    with pytest.raises(ui.BadRequest, match='same network'):
        body_job(app, action='pay', frm='b1', to='e1', amount=1000)
    # An EVM invoice looks like a regtest one (lnbcrt): the payee gives it away.
    inv = e1.bolt11({'amount_msat': 10**6})
    b1.payees[inv] = e1.id
    with pytest.raises(ui.BadRequest, match='invoice of e1 on evm:31337'):
        body_job(app, action='pay', frm='b1', bolt11=inv)
    # A Liquid invoice into a Bitcoin node: the prefix gives it away.
    with pytest.raises(ui.BadRequest, match='another network'):
        body_job(app, action='pay', frm='b1', bolt11=l1.bolt11({'amount_msat': 10**6}))
    with pytest.raises(ui.BadRequest, match='not a bolt11'):
        body_job(app, action='pay', frm='b1', bolt11='lnbcrt1<script>')
    ok = b2.bolt11({'amount_msat': 10**6})
    b1.payees[ok] = b2.id
    assert body_job(app, action='pay', frm='b1', bolt11='LIGHTNING:' + ok.upper())[1] == 'btc:regtest'


def test_http_refuses_cross_network_open_with_400_and_needs_the_token():
    app, _ = mixed_app()
    srv = ui.serve(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    token = srv.RequestHandlerClass.token

    def post(body, headers=None, host=None):
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        h = {'Content-Type': 'application/json', 'X-NodeUI-Token': token, 'Host': host or '127.0.0.1:%d' % port}
        h.update(headers or {})
        c.request('POST', '/api/job', json.dumps(body), h)
        r = c.getresponse()
        return r.status, json.loads(r.read())

    try:
        st, res = post({'action': 'open', 'frm': 'b1', 'to': 'e1', 'amount': 1000})
        assert st == 400 and 'same network' in res['error']
        st, res = post({'action': 'pay', 'frm': 'l1', 'to': 'b2', 'amount': 1000})
        assert st == 400
        assert post({'action': 'mine', 'net': 'btc:regtest', 'blocks': 1}, {'X-NodeUI-Token': 'wrong'})[0] == 403
        assert post({'action': 'mine', 'net': 'btc:regtest', 'blocks': 1}, {'Origin': 'http://evil.test'})[0] == 403
        assert post({'action': 'mine', 'net': 'btc:regtest', 'blocks': 1}, host='evil.test:%d' % port)[0] == 403
        assert post({'action': 'mine', 'net': 'btc:regtest', 'blocks': 1},
                    {'Content-Type': 'text/plain'})[0] == 403
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('GET', '/api/state')
        assert c.getresponse().status == 403
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('GET', '/')
        page = c.getresponse().read().decode()
        assert token in page and '__NODEUI_TOKEN__' not in page
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('GET', '/api/state', headers={'X-NodeUI-Token': token})
        r = c.getresponse()
        assert r.status == 200 and json.loads(r.read())['networks']
        assert srv.server_address[0] == '127.0.0.1'
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- job building, every action
def full_app():
    b1, b2 = fakes.bitcoin(1), fakes.bitcoin(2)
    connect(b1, b2, 'aa' * 32)
    l1, l2 = fakes.liquid(3), fakes.liquid(4)
    connect(l1, l2, 'bb' * 32, asset=ASSET, scid='105x1x0')
    e1, e2 = fakes.evm(5), fakes.evm(6)
    connect(e1, e2, 'cc' * 32, scid='106x1x0')
    t1 = fakes.evm(7, chain_id=84532, contract=CONTRACT, token=TOKEN)
    return make_app({'b1': b1, 'b2': b2, 'l1': l1, 'l2': l2, 'e1': e1, 'e2': e2, 't1': t1}), \
        dict(b1=b1, b2=b2, l1=l1, l2=l2, e1=e1, e2=e2, t1=t1)


@pytest.mark.parametrize('body,net,words', [
    ({'action': 'open', 'frm': 'b1', 'to': 'b2', 'amount': '100000', 'announce': True}, 'btc:regtest', 'Open btc:regtest'),
    ({'action': 'open', 'frm': 'l1', 'to': 'l2', 'amount': 5000, 'asset': ASSET}, 'liquid:regtest', 'regtest/ddc8da3f'),
    ({'action': 'pay', 'frm': 'e1', 'to': 'e2', 'amount': 1000}, 'evm:31337:' + fakes.ANVIL_CONTRACT, 'Pay 1,000 sat'),
    ({'action': 'invoice', 'node': 'l2', 'amount': 10, 'asset': ASSET, 'description': 'hi'}, 'liquid:regtest', 'Invoice'),
    ({'action': 'close', 'node': 'b1', 'chan': 'aa' * 32}, 'btc:regtest', 'Close btc:regtest channel b1 and b2'),
    ({'action': 'close', 'node': 'e2', 'chan': 'cc' * 32, 'force': True}, 'evm:31337:' + fakes.ANVIL_CONTRACT, 'Force close'),
    ({'action': 'send', 'node': 'b1', 'address': 'bcrt1q' + 'q' * 38, 'amount': 'all'}, 'btc:regtest', 'Send all'),
    ({'action': 'send', 'node': 'l1', 'address': 'el1qq' + 'q' * 60, 'amount': 7, 'asset': ASSET}, 'liquid:regtest', 'ddc8da3f'),
    ({'action': 'newaddr', 'node': 'l1', 'type': 'elements'}, 'liquid:regtest', 'New elements'),
    ({'action': 'fund', 'node': 'b2', 'amount': 1000000}, 'btc:regtest', 'from bitcoind'),
    ({'action': 'fund', 'node': 'l2', 'amount': 1000}, 'liquid:regtest', 'L-BTC'),
    ({'action': 'deposit', 'node': 't1', 'amount': 1000}, 'evm:84532:' + CONTRACT, 'Deposit 1,000 sat'),
    ({'action': 'exit', 'node': 'e1', 'all': True}, 'evm:31337:' + fakes.ANVIL_CONTRACT, 'Exit all'),
    ({'action': 'withdraw', 'node': 'e2'}, 'evm:31337:' + fakes.ANVIL_CONTRACT, 'Withdraw'),
    ({'action': 'mine', 'net': 'evm:31337:' + fakes.ANVIL_CONTRACT, 'blocks': 41}, 'evm:31337:' + fakes.ANVIL_CONTRACT, 'Mine 41'),
])
def test_build_every_action(body, net, words):
    app, _ = full_app()
    title, got_net, fn, nodes = ui.build_job(app, body)
    assert got_net == net and words in title and callable(fn)
    names = {k: body[k] for k in ('frm', 'to', 'node') if k in body}
    assert set(names.values()) <= set(nodes) and all(app.nodes[x].net == net for x in nodes)


@pytest.mark.parametrize('body,match', [
    ({'action': 'nope'}, 'unknown action'),
    ({'action': 'open', 'frm': 'zz', 'to': 'b2', 'amount': 1}, 'frm: pick one'),
    ({'action': 'open', 'frm': 'b1', 'to': 'b2', 'amount': -5}, 'out of range'),
    ({'action': 'open', 'frm': 'b1', 'to': 'b2', 'amount': 1.5}, 'whole number'),
    ({'action': 'open', 'frm': 'b1', 'to': 'b2', 'amount': True}, 'whole number'),
    ({'action': 'open', 'frm': 'b1', 'to': 'b2', 'amount': 10**16}, 'out of range'),
    ({'action': 'open', 'frm': 'b1', 'to': 'b2', 'amount': '1; rm -rf /'}, 'whole number'),
    ({'action': 'send', 'node': 'b1', 'address': 'tb1q' + 'q' * 38, 'amount': 5}, 'not for regtest'),
    ({'action': 'send', 'node': 'b1', 'address': '$(reboot)', 'amount': 5}, 'not a regtest address'),
    ({'action': 'send', 'node': 'l1', 'address': 'x' * 10, 'amount': 5}, 'not a Liquid'),
    ({'action': 'send', 'node': 'l1', 'address': 'el1qq' + 'q' * 60, 'amount': 'all'}, 'whole number'),
    ({'action': 'close', 'node': 'b1', 'chan': 'cc' * 32}, 'no such channel'),
    ({'action': 'close', 'node': 'b1', 'chan': 'xyz'}, 'not a channel id'),
    ({'action': 'deposit', 'node': 'b1', 'amount': 5}, 'is a bitcoin node'),
    ({'action': 'fund', 'node': 'e1', 'amount': 5}, 'is a evm node'),
    ({'action': 'mine', 'net': 'evm:84532:' + CONTRACT, 'blocks': 1}, 'nobody mines'),
    ({'action': 'mine', 'net': 'btc:regtest', 'blocks': 501}, 'at most 500'),
    ({'action': 'invoice', 'node': 'b1', 'amount': 5, 'description': 'a\nb'}, 'printable'),
    ({'action': 'newaddr', 'node': 'b1', 'type': 'elements'}, 'type'),
])
def test_bad_requests(body, match):
    app, _ = full_app()
    with pytest.raises(ui.BadRequest, match=match):
        ui.build_job(app, body)


def test_no_mining_off_anvil():
    app, _ = full_app()
    nets = {x['id']: x['mining'] for x in app.state['networks']}
    assert nets['evm:84532:' + CONTRACT] is False
    assert nets['evm:31337:' + fakes.ANVIL_CONTRACT] is True
    # The real make_miner: only chain id 31337 gets an AnvilMiner.
    real = ui.App([ui.Node('x', '/nonexistent')], env={})
    n84532 = ui.Node('t', '/x', factory=fakes.evm(9, chain_id=84532, contract=CONTRACT))
    n84532.detect()
    assert real.make_miner(n84532) is None
    n31337 = ui.Node('a', '/x', factory=fakes.evm(10))
    n31337.detect()
    assert isinstance(real.make_miner(n31337), ui.AnvilMiner)
    b = ui.Node('b', '/x', factory=fakes.bitcoin(11, 'testnet'))
    b.detect()
    assert real.make_miner(b) is None


def test_explorer_path_takes_only_addresses_and_hashes():
    base = 'https://base-sepolia.blockscout.com'
    assert ui.explorer_path(base, 'tx', '0x' + 'ab' * 32) == '/tx/0x' + 'ab' * 32
    assert ui.explorer_path(base, 'address', CONTRACT) == '/address/' + CONTRACT
    for what, value in (('tx', 'zz' * 32), ('tx', 'ab' * 31), ('address', 'tex1q"><b>x</b>' + 'q' * 20),
                        ('address', 'short'), ('address', None)):
        assert ui.explorer_path(base, what, value) is None
    assert ui.explorer_path(None, 'tx', 'ab' * 32) is None


def test_explorer_links_for_addresses_and_funding_txs(monkeypatch):
    """Node cards link the ETH address, contract and token (EVM) or the
    wallet addresses (Bitcoin, Liquid) to the chain's explorer; channels link
    their funding tx, on an EVM chain through evm-txinfo's EVM tx. The state
    carries paths, and each network's explorer in state['explorers'] (added
    after the scrub, which blanks URLs). Regtest has no explorer."""
    monkeypatch.setattr(ui, 'TXINFO_RETRY', 0)
    e1, e2 = fakes.evm(1, chain_id=84532, contract=CONTRACT, token=TOKEN), fakes.evm(2, chain_id=84532,
                                                                                     contract=CONTRACT, token=TOKEN)
    info = {'txid': 'f0' * 32, 'height': 4013, 'evm_block': 56450000, 'evm_tx': None}
    e1.methods.append('evm-txinfo')
    e1.r['evm-txinfo'] = lambda p: dict(info, txid=p['txid'])
    connect(e1, e2, 'cc' * 32, scid='106x1x0')
    l1, l2 = fakes.liquid(3, 'liquid-testnet'), fakes.liquid(4, 'liquid-testnet')
    connect(l1, l2, 'bb' * 32, asset=ASSET, scid='105x1x0')
    for f, txid in ((e1, 'f0' * 32), (e2, 'f0' * 32), (l1, 'f1' * 32), (l2, 'f1' * 32)):
        for c in f.r['listpeerchannels']['channels']:
            c.update(funding_txid=txid, funding_outnum=0)
    l1.r['listfunds']['outputs'] = [{'txid': '%02x' % i * 32, 'output': 0, 'amount_msat': (i + 1) * 1000_000,
                                     'status': 'confirmed', 'address': 'tex1q' + chr(97 + i) * 38} for i in range(6)]
    b1 = fakes.bitcoin(5)
    b1.r['listfunds']['outputs'][0]['address'] = 'bcrt1q' + 'q' * 38
    app = make_app({'e1': e1, 'e2': e2, 'l1': l1, 'l2': l2, 'b1': b1})

    bs, liq = 'https://base-sepolia.blockscout.com', 'https://blockstream.info/liquidtestnet'
    ex = app.state['explorers']
    assert ex == {node_of(app, 'e1')['net']: bs, 'liquid:testnet': liq}       # none for btc:regtest
    links = {x['label']: x['path'] for x in node_of(app, 'e1')['links']}
    assert links == {'ETH address': '/address/0x70997970C51812dc3A010C7d01b50e0d17dc79C8',
                     'Contract': '/address/' + CONTRACT, 'Token': '/address/' + TOKEN}
    wl = node_of(app, 'l1')['links']
    assert [x['sat'] for x in wl] == [6000, 5000, 4000, 3000] and node_of(app, 'l1')['links_more'] == 2
    assert ex[node_of(app, 'l1')['net']] + wl[0]['path'] == liq + '/address/tex1q' + 'f' * 38
    assert [(x['label'], x['path']) for x in node_of(app, 'b1')['links']] == [('Wallet', None)]

    chans = {c['scid']: c for c in app.state['channels']}
    assert ex[chans['105x1x0']['node_net']] + chans['105x1x0']['funding']['path'] == liq + '/tx/' + 'f1' * 32
    # Not applied yet at the tag: no link, asked again on the next poll.
    assert chans['106x1x0']['funding']['path'] is None and 'not yet' in chans['106x1x0']['funding']['title']
    info['evm_tx'] = '0x' + 'ab' * 32
    app.poll_once()
    f = {c['scid']: c for c in app.state['channels']}['106x1x0']['funding']
    assert f['path'] == '/tx/0x' + 'ab' * 32 and 'block 56450000' in f['title']
    asked = len(e1.called('evm-txinfo'))
    app.poll_once()
    assert len(e1.called('evm-txinfo')) == asked       # remembered


def test_invoice_hints_private_channels_and_shows_warnings():
    """A payee whose channels are all private gets them named in
    exposeprivatechannels (CLN drops dead-end hints otherwise); one with an
    announced channel, or none that can take the amount, is left to CLN.
    CLN's warning_* fields go to the log."""
    app, f = full_app()
    e2 = f['e2']
    for c in e2.r['listpeerchannels']['channels']:
        c['private'] = True
    st, log, _ = run(app, {'action': 'pay', 'frm': 'e1', 'to': 'e2', 'amount': 1000})
    assert st == 'done'
    assert e2.called('invoice')[-1]['exposeprivatechannels'] == ['106x1x0']
    assert any('e2 can receive this only on private channels: the invoice hints 106x1x0' in x for x in log)

    # More than the channel can receive (600,000 sat is on e1's side): CLN's choice, and the log says why.
    st, log, _ = run(app, {'action': 'invoice', 'node': 'e2', 'amount': 700_000})
    assert st == 'done' and 'exposeprivatechannels' not in e2.called('invoice')[-1]
    assert any('e2 can receive at most 600,000 sat in one channel' in x for x in log)

    # An announced channel that can't take it (all ours, as when we opened it): the private one is hinted.
    e2.r['listpeerchannels']['channels'].append(dict(e2.r['listpeerchannels']['channels'][0], private=False,
                                                     short_channel_id='107x1x0', to_us_msat=10**9,
                                                     total_msat=10**9))
    st, log, _ = run(app, {'action': 'invoice', 'node': 'e2', 'amount': 1000})
    assert e2.called('invoice')[-1]['exposeprivatechannels'] == ['106x1x0']

    # One announced channel that can take it: CLN's choice, and its warnings are shown.
    e2.r['listpeerchannels']['channels'][-1].update(to_us_msat=0)
    warning = 'Insufficient incoming capacity, once dead-end peers were excluded'
    e2.r['invoice'] = lambda p: {'bolt11': e2.bolt11(p), 'payment_hash': '11' * 32, 'warning_deadends': warning}
    st, log, res = run(app, {'action': 'invoice', 'node': 'e2', 'amount': 1000})
    assert st == 'done' and 'exposeprivatechannels' not in e2.called('invoice')[-1]
    assert any('CLN warning_deadends: Insufficient incoming capacity' in x for x in log)
    assert res['bolt11'].startswith('lnbcrt')


def test_run_pay_invoice_send_newaddr_mine():
    app, f = full_app()
    st, log, _ = run(app, {'action': 'pay', 'frm': 'b1', 'to': 'b2', 'amount': 1000})
    assert st == 'done' and any('paid: preimage' in x for x in log)
    assert f['b2'].called('invoice')[0]['amount_msat'] == 1_000_000 and f['b1'].called('xpay')
    st, log, _ = run(app, {'action': 'pay', 'frm': 'l1', 'to': 'l2', 'amount': 5, 'asset': ASSET})
    assert st == 'done' and f['l2'].called('assetchan-invoice')[0]['asset'] == ASSET
    assert f['l1'].called('assetchan-pay')[0]['asset'] == ASSET
    st, log, res = run(app, {'action': 'invoice', 'node': 'e1', 'amount': 2000})
    assert st == 'done' and res['bolt11'].startswith('lnbcrt')
    st, log, _ = run(app, {'action': 'send', 'node': 'b1', 'address': 'bcrt1q' + 'q' * 38, 'amount': 'all'})
    assert st == 'done' and f['b1'].called('withdraw')[0]['satoshi'] == 'all'
    st, log, res = run(app, {'action': 'newaddr', 'node': 'b2', 'type': 'p2tr'})
    assert res['address'].startswith('bcrt1p')
    miner = app.miners['btc:regtest']
    before = miner.mined
    st, log, _ = run(app, {'action': 'mine', 'net': 'btc:regtest', 'blocks': 3})
    assert st == 'done' and miner.mined == before + 3


def test_run_evm_deposit_exit_withdraw():
    app, f = full_app()
    e1 = f['e1']
    e1.r['listfunds']['outputs'].append({'txid': 'ee' * 32, 'output': 0, 'amount_msat': 10**9,
                                         'status': 'confirmed'})
    st, log, _ = run(app, {'action': 'deposit', 'node': 'e1', 'amount': 1000})
    assert st == 'done', log
    assert e1.called('evm-deposit')[0] == {'amount': 1000, 'address': 'bcrt1p' + 'q' * 58}
    # exit: withdraw to evm-exitaddr of the node's own ETH address; the contract credits it.
    calls = [0]

    def status(p):
        calls[0] += 1
        return dict(e1.status, withdrawable_wei=0 if calls[0] == 1 else 100_000 * ui.SAT_WEI)
    e1.r['evm-status'] = status
    st, log, _ = run(app, {'action': 'exit', 'node': 'e1', 'amount': 100000})
    assert st == 'done', log
    assert e1.called('evm-exitaddr')[-1] == {'ethaddress': e1.status['eth_address']}
    assert e1.called('withdraw')[-1] == {'destination': 'bcrt1s' + 'q' * 34, 'satoshi': 100000}
    assert any('+0.001 ETH · 100,000 sat' in x for x in log)
    st, log, _ = run(app, {'action': 'withdraw', 'node': 'e2'})
    assert st == 'done' and any('≈0.001 ETH · 100,001 sat' in x for x in log)
    st, log, _ = run(app, {'action': 'withdraw', 'node': 't1'})
    assert any('1,234,500 {} units · 12,345 sat'.format(TOKEN[:8]) in x for x in log)


def test_pay_shows_its_route_and_explains_a_missing_one():
    """A page-to-page payment logs its route from the forwards that appeared
    on the page's nodes; a routing failure says what the payer knows."""
    e1, e2, e3 = fakes.evm(1), fakes.evm(2), fakes.evm(3)
    connect(e1, e2, 'c1' * 32, scid='201x1x0')
    connect(e2, e3, 'c2' * 32, scid='202x1x0')
    paid = dict(e1.r['xpay'])

    def xpay(p):
        e2.r['listforwards']['forwards'].append({'in_channel': '201x1x0', 'out_channel': '202x1x0',
                                                 'status': 'settled', 'fee_msat': 1001})
        return paid
    e1.r['xpay'] = xpay
    app = make_app({'e1': e1, 'e2': e2, 'e3': e3})
    st, log, _ = run(app, {'action': 'pay', 'frm': 'e1', 'to': 'e3', 'amount': 1000})
    assert st == 'done', log
    assert any('route: e1 -> e2 -> e3 (2 hops, forwarded by e2 over 201x1x0 -> 202x1x0 (fee 1,001 msat))' in x
               for x in log)
    assert 'retry_for' not in e1.called('xpay')[-1]          # anvil: the page mines, heights agree
    st, log, _ = run(app, {'action': 'pay', 'frm': 'e2', 'to': 'e3', 'amount': 1000})
    assert any('route: e2 -> e3 (1 hop, direct)' in x for x in log)

    e1.r['xpay'] = ui.RpcError('xpay', {}, {'code': 203, 'message':
                                            'Failed: There is no connection between source and destination at all'})
    st, log, _ = run(app, {'action': 'pay', 'frm': 'e1', 'to': 'e3', 'amount': 1000})
    assert st == 'error'
    assert any('e1 knows 0 channels through gossip and has 1 open channel of its own' in x for x in log)
    assert any('"Announce to the network" ticked' in x for x in log)


def test_close_on_a_public_chain_reports_the_share_and_the_wallet():
    """No mining: the close waits for the share to reach the wallet, says
    how much, links the EVM tx that applied the closing tx, and says what
    turns it into ETH."""
    e1, e2 = fakes.evm(1, chain_id=84532, contract=CONTRACT), fakes.evm(2, chain_id=84532, contract=CONTRACT)
    connect(e1, e2, 'cc' * 32, scid='106x1x0', to_us=600_000)
    e1.methods.append('evm-txinfo')
    e1.r['evm-txinfo'] = lambda p: {'txid': p['txid'], 'evm_tx': '0x' + 'cd' * 32, 'evm_block': 56_000_000,
                                    'height': 4100}

    def close(p):
        e1.r['listpeerchannels']['channels'][0]['state'] = 'ONCHAIN'
        e1.r['listfunds']['outputs'].append({'txid': 'ab' * 32, 'output': 0, 'amount_msat': 599_000_000,
                                             'status': 'confirmed'})
        return {'type': 'mutual', 'txid': 'ab' * 32}
    e1.r['close'] = close
    app = make_app({'e1': e1, 'e2': e2})
    st, log, _ = run(app, {'action': 'pay', 'frm': 'e1', 'to': 'e2', 'amount': 1000})
    assert st == 'done' and e1.called('xpay')[-1]['retry_for'] == ui.PAY_RETRY_FOR    # nobody mines here
    st, log, _ = run(app, {'action': 'close', 'node': 'e1', 'chan': 'cc' * 32})
    assert st == 'done', log
    text = '\n'.join(log)
    assert "e1's share of the channel: 600,000 sat" in text
    assert "e1's wallet: 5,000,000 -> 5,599,000 sat (+599,000" in text
    assert 'closing tx {} was applied by EVM tx 0x{} (block 56,000,000, virtual height 4,100)'.format(
        'ab' * 32, 'cd' * 32) in text
    assert 'Exit moves wallet sat into the contract' in text


def test_cash_out_exits_withdraws_and_shows_the_eth_balance():
    e1 = fakes.evm(1, chain_id=84532, contract=CONTRACT)

    def exit_(p):
        e1.status['withdrawable_wei'] = 10**15
        return {'txid': 'ee' * 32, 'tx': '00'}

    def withdraw(p):
        e1.status.update(withdrawable_wei=0, balance_wei=e1.status['balance_wei'] + 10**15 - 10**12)
        return {'address': e1.status['eth_address'], 'evm_tx': '0x' + 'cd' * 32, 'status': 'mined',
                'amount_wei': 10**15}
    e1.r['withdraw'], e1.r['evm-withdraw'] = exit_, withdraw
    app = make_app({'e1': e1})
    st, log, _ = run(app, {'action': 'cashout', 'node': 'e1'})
    assert st == 'done', log
    assert e1.called('withdraw')[-1]['satoshi'] == 'all' and e1.called('evm-withdraw')
    line = next(x for x in log if x.startswith('ETH balance of'))
    assert '{} -> {}'.format(ui.fmt_eth(10**18), ui.fmt_eth(10**18 + 10**15 - 10**12)) in line
    assert "the EVM tx's gas: {}".format(ui.fmt_eth(10**12)) in line


def test_jobs_run_in_parallel_across_networks_and_in_turn_on_one():
    """One running job per network ID: another network's job runs at once,
    the same network's waits for it (in order), and only MAX_WAITING wait."""
    app, _ = full_app()
    gates = {k: threading.Event() for k in ('btc1', 'btc2', 'evm', 'nodeclash')}
    ran = []

    def job(name):
        def fn(ctx):
            ran.append(name)
            assert gates[name].wait(5)
        return fn
    btc, evm_net = 'btc:regtest', 'evm:31337:' + fakes.ANVIL_CONTRACT
    j1 = app.jobs.start('btc 1', btc, job('btc1'), ('b1', 'b2'))
    j2 = app.jobs.start('btc 2', btc, job('btc2'), ('b1',))
    j3 = app.jobs.start('evm', evm_net, job('evm'), ('e1',))
    wait_until(lambda: sorted(ran) == ['btc1', 'evm'])
    active, _ = app.jobs.current()
    status = {j['title']: j['status'] for j in active}
    assert status == {'btc 1': 'running', 'btc 2': 'waiting', 'evm': 'running'}
    assert next(j for j in active if j['id'] == j2)['waiting_for'] == ['btc 1']
    # A job naming a node of a running job waits too, whatever its network says
    # (and then refuses to run: the node is not on its network).
    j4 = app.jobs.start('node clash', 'liquid:regtest', job('nodeclash'), ('e1',))
    assert app.jobs.get(j4)['status'] == 'waiting'
    gates['evm'].set()
    wait_until(lambda: app.jobs.get(j3)['status'] == 'done' and app.jobs.get(j4)['status'] == 'error')
    assert 'nodeclash' not in ran
    assert app.jobs.get(j2)['status'] == 'waiting', 'btc 1 still runs'
    gates['btc1'].set()
    wait_until(lambda: 'btc2' in ran)
    assert ran.index('btc2') > ran.index('btc1') and app.jobs.get(j1)['status'] == 'done'
    # MAX_WAITING per network, then Busy; a waiting job can be cancelled.
    waiting = [app.jobs.start('more', btc, lambda ctx: None, ()) for _ in range(ui.Jobs.MAX_WAITING)]
    with pytest.raises(ui.Busy):
        app.jobs.start('too many', btc, lambda ctx: None, ())
    assert app.jobs.cancel(waiting[-1]) and app.jobs.get(waiting[-1])['status'] == 'cancelled'
    gates['btc2'].set()
    gates['nodeclash'].set()
    wait_until(lambda: not app.jobs.running())
    _, history = app.jobs.current()
    assert {h['status'] for h in history} == {'done', 'cancelled', 'error'}
    assert all(not k.startswith('_') and k != 'fn' for h in history for k in h)


def test_a_failing_job_frees_its_network():
    app, _ = full_app()

    def boom(ctx):
        raise RuntimeError('boom')
    j = app.jobs.start('boom', 'btc:regtest', boom, ('b1',), wait=True)
    assert app.jobs.get(j)['status'] == 'error'
    j2 = app.jobs.start('next', 'btc:regtest', lambda ctx: None, ('b1',), wait=True)
    assert app.jobs.get(j2)['status'] == 'done'


def test_http_runs_jobs_on_two_networks_and_cancels_by_id():
    app, _ = full_app()
    gate = threading.Event()
    srv = ui.serve(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port, token = srv.server_address[1], srv.RequestHandlerClass.token

    def post(path, body):
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('POST', path, json.dumps(body), {'Content-Type': 'application/json', 'X-NodeUI-Token': token,
                                                   'Host': '127.0.0.1:%d' % port})
        r = c.getresponse()
        return r.status, json.loads(r.read())
    try:
        app.miners['btc:regtest'].mine = lambda n: gate.wait(5)          # a slow mine
        st1, r1 = post('/api/job', {'action': 'mine', 'net': 'btc:regtest', 'blocks': 1})
        st2, r2 = post('/api/job', {'action': 'mine', 'net': 'btc:regtest', 'blocks': 2})
        st3, r3 = post('/api/job', {'action': 'invoice', 'node': 'e1', 'amount': 5})
        assert (st1, r1['status'], st2, r2['status'], st3) == (200, 'running', 200, 'waiting', 200)
        wait_until(lambda: app.jobs.get(r3['id'])['status'] == 'done')      # the EVM one did not wait
        assert post('/api/cancel', {'id': r2['id']}) == (200, {'cancelled': True})
        assert post('/api/cancel', {'id': 'x'})[0] == 400
        assert post('/api/cancel', {'id': 999}) == (200, {'cancelled': False})
        gate.set()
        wait_until(lambda: not app.jobs.running())
        assert app.jobs.get(r2['id'])['status'] == 'cancelled'
        st, res = post('/api/refresh', {'node': 'e1'})
        assert st == 200 and res['refreshed'] is False      # the invoice job's end just read it afresh
        assert post('/api/refresh', {'node': 'zz'})[0] == 400
    finally:
        gate.set()
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- secrets
def test_scrubber():
    s = ui.Scrubber()
    s.add('hunter2pw', 'https://base-sepolia.infura.io/v3/KEY123456789')
    t = s.text('failed: https://base-sepolia.infura.io/v3/KEY123456789 with hunter2pw and http://u:p@1.2.3.4:5')
    assert 'KEY123456789' not in t and 'hunter2pw' not in t and 'u:p@' not in t and '1.2.3.4' not in t
    assert s.text('user:secret@host.example rejected') == '<secret> rejected'
    assert s.deep({'a': ['x https://x.test/abc y']}) == {'a': ['x <url> y']}


def test_rpc_errors_never_carry_urls_or_keys():
    e = fakes.evm(1, rpc='https://base-sepolia.infura.io/v3/${INFURA_API_KEY}')
    e.r['evm-deposit'] = ui.RpcError('evm-deposit', {}, {
        'code': -1, 'message': 'eth_sendRawTransaction to https://base-sepolia.infura.io/v3/abcdef0123456789 failed'})
    e.status['alarms'] = {'sync': 'index sync failing: abcdef0123456789 refused'}
    nodes = [ui.Node('e', '/x', factory=e)]
    app = ui.App(nodes, env={'INFURA_API_KEY': 'abcdef0123456789'})
    app.poll_once()
    text = json.dumps(app.state)
    assert 'abcdef0123456789' not in text and 'infura' not in text and '${INFURA' not in text
    title, net, fn, nodes = ui.build_job(app, {'action': 'deposit', 'node': 'e', 'amount': 5})
    job = app.jobs.get(app.jobs.start(title, net, fn, nodes, wait=True))
    assert job['status'] == 'error'
    assert 'abcdef0123456789' not in json.dumps(job) and 'infura' not in json.dumps(job)


def test_jsonrpc_failure_says_no_url():
    sc = ui.Scrubber()
    r = ui.JsonRpc('http://user:pa55word@127.0.0.1:1', name='bitcoind', scrub=sc, timeout=2)
    with pytest.raises(ui.Fail) as e:
        r.call('getblockcount')
    assert 'pa55word' not in str(e.value) and '127.0.0.1' not in str(e.value) and 'bitcoind' in str(e.value)
    assert 'pa55word' in ''.join(sc.secrets)


def test_liquid_elements_url_never_in_state():
    app, _ = full_app()
    assert 's3cretpw' not in json.dumps(app.state) and '18884' not in json.dumps(app.state)


def test_rpc_password_from_config_file(tmp_path):
    p = tmp_path / 'config'
    p.write_text('network=regtest\nbitcoin-rpcpassword=pw0rd\n')
    cfg = {'bitcoin-rpcuser': {'value_str': 'u'},
           'bitcoin-rpcpassword': {'value_str': '...', 'source': '{}:2'.format(p)}}
    assert ui.rpc_password(cfg) == 'pw0rd'
    assert ui.App.core_rpc_url(cfg, 'regtest') == 'http://u:pw0rd@127.0.0.1:18443'


# ---------------------------------------------------------------- token mode and amounts
def test_fmt_amounts():
    assert ui.fmt_eth(10**18) == '1 ETH · 100,000,000 sat'
    assert ui.fmt_eth(10 * 10**18) == '10 ETH · 1,000,000,000 sat'
    assert ui.fmt_eth(0) == '0 ETH · 0 sat'
    assert ui.fmt_eth(15 * 10**15) == '0.015 ETH · 1,500,000 sat'
    # At most 6 decimals, trimmed, with ≈ when rounded; the tooltip has them all.
    assert ui.fmt_eth(15997549833998572) == '≈0.015998 ETH · ~1,599,754 sat'
    assert ui.fmt_eth_exact(15997549833998572) == '0.015997549833998572 ETH = 15,997,549,833,998,572 wei'
    assert ui.fmt_eth(12345 * ui.SAT_WEI) == '≈0.000123 ETH · 12,345 sat'
    assert ui.fmt_eth(123456 * ui.SAT_WEI) == '≈0.001235 ETH · 123,456 sat'
    assert ui.fmt_eth(1) == '≈0 ETH · ~0 sat' and ui.fmt_eth_exact(1) == '0.000000000000000001 ETH = 1 wei'
    assert ui.fmt_units(5_000_000_000, 100) == '5,000,000,000 units · 50,000,000 sat'
    assert ui.fmt_units(150, 100, 'USDC') == '150 USDC units · ~1 sat'
    # A unit that is a sat: the number once, with the token's label.
    assert ui.fmt_units(1_076_195, 1, 'USDC') == '1,076,195 USDC units'
    assert ui.fmt_eth(None) is None
    assert ui.eth_to_wei('0.01') == 10**16 and ui.eth_to_wei('x') is None


def test_parse_evm_status_eth_mode():
    st = fakes.evm_status(eth=5 * 10**15, withdrawable=3 * ui.SAT_WEI)
    cfg = {'evm-min-balance': {'value_str': '0.01'}}
    e = ui.parse_evm_status(st, cfg, now=86400)
    assert e['token'] is None and e['low_gas'] is True
    assert e['gas_text'] == '0.005 ETH · 500,000 sat' and e['min_balance_text'] == '0.01 ETH · 1,000,000 sat'
    assert e['withdrawable_sat'] == 3 and e['withdrawable_text'] == '≈0 ETH · 3 sat'
    assert e['withdrawable_title'] == '0.00000003 ETH = 30,000,000,000 wei'
    assert e['gas_title'] == '0.005 ETH = 5,000,000,000,000,000 wei'
    # eth_call 100 x 80 + eth_getLogs 10 x 255 + eth_chainId 2 x 5 = 10,560 credits in one day
    assert e['credits'] == 10560 and e['credits_per_day'] == 10560 and e['credits_pct'] == 0.4
    assert e['anvil'] is True and e['lag'] == 1


def test_parse_evm_status_token_mode_and_optional_fields():
    st = fakes.evm_status(chain_id=84532, contract=CONTRACT, token=TOKEN, withdrawable=12345)
    del st['rpc_calls'], st['rpc_calls_since']       # the ERC-20 branch has no rpc_calls
    assert 'withdrawable_wei' not in st
    e = ui.parse_evm_status(st, {})
    assert e['token'] == TOKEN and e['units_per_sat'] == 100
    assert e['token_balance_text'] == '5,000,000,000 units · 50,000,000 sat'
    assert e['withdrawable'] == 12345 and e['withdrawable_sat'] == 123
    assert e['withdrawable_text'] == '12,345 units · ~123 sat'
    assert ui.parse_evm_status(dict(st, units_per_sat=1), {}, label='USDC')['token_balance_text'] \
        == '5,000,000,000 USDC units'
    assert 'credits_per_day' not in e and e['anvil'] is False and e['low_gas'] is False
    assert ui.parse_evm_status({}, {})['gas_text'] is None


def test_token_node_in_state():
    app, _ = full_app()
    t = node_of(app, 't1')
    assert t['evm']['token'] == TOKEN and t['evm']['token_label'] == TOKEN[:8]
    assert t['mining'] is False and t['evm']['credits_per_day'] > 0


def test_alarms_reach_the_page():
    e = fakes.evm(1)
    e.status.update(alarms={'low_balance': 'ETH balance below evm-min-balance'}, opens_refused='balance low',
                    option_problems=['watchtime-blocks 6 < 40'])
    app = make_app({'e': e})
    ev = node_of(app, 'e')['evm']
    assert ev['alarms'] == {'low_balance': 'ETH balance below evm-min-balance'}
    assert ev['opens_refused'] == 'balance low' and ev['option_problems'] == ['watchtime-blocks 6 < 40']


def test_onchaind_states_shown():
    b1, b2 = fakes.bitcoin(1), fakes.bitcoin(2)
    connect(b1, b2, 'aa' * 32, state='ONCHAIN', status=['ONCHAIN:2 outputs unresolved: in 5 blocks will spend '
                                                        'DELAYED_OUTPUT_TO_US'])
    connect(b1, b2, 'bb' * 32, state='AWAITING_UNILATERAL', scid='104x1x0')
    app = make_app({'b1': b1, 'b2': b2})
    ch = {c['id']: c for c in app.state['channels']}
    assert ch['aa' * 32]['state'] == 'onchain' and 'DELAYED_OUTPUT_TO_US' in ch['aa' * 32]['status']
    assert ch['bb' * 32]['state'] == 'closing' and ch['bb' * 32]['raw_state'] == 'AWAITING_UNILATERAL'
    with pytest.raises(ui.BadRequest, match='already closed'):
        ui.build_job(app, {'action': 'close', 'node': 'b1', 'chan': 'aa' * 32})


def test_demo_json_has_every_kind():
    import os
    with open(os.path.join(os.path.dirname(ui.__file__), 'ui', 'demo.json')) as f:
        d = json.load(f)
    kinds = {n['kind'] for n in d['nodes']}
    assert kinds == {'bitcoin', 'liquid', 'evm'}
    assert any(n.get('evm') and n['evm']['token'] for n in d['nodes'])
    assert any(n.get('evm') and not n['evm']['token'] for n in d['nodes'])
    assert any(c['asset'] for c in d['channels'])
    for key in ('networks', 'nodes', 'channels', 'maps', 'assets', 'jobs', 'history'):
        assert key in d


# ---------------------------------------------------------------- review fixes
def test_lbtc_send_takes_the_bitcoin_label():
    app, _ = full_app()
    title, net, _, _ = ui.build_job(app, {'action': 'send', 'node': 'l1', 'address': 'el1qq' + 'q' * 60,
                                          'amount': 7, 'asset': 'bitcoin'})
    assert 'L-BTC' in title


def test_undetected_evm_nodes_cannot_open_to_each_other():
    e1, e2 = fakes.evm(1), fakes.evm(2, chain_id=84532)
    for e in (e1, e2):
        e.r['evm-status'] = ui.RpcError('evm-status', {}, {'code': -1, 'message': 'not ready'})
    app = make_app({'e1': e1, 'e2': e2})
    assert app.nodes['e1'].net == app.nodes['e2'].net          # both evm:?:<contract>
    with pytest.raises(ui.BadRequest, match='not fully detected'):
        ui.build_job(app, {'action': 'open', 'frm': 'e1', 'to': 'e2', 'amount': 1000})


def test_redetects_when_a_node_comes_back_on_another_network():
    b = fakes.bitcoin(1)
    app = make_app({'b': b})
    assert app.nodes['b'].net == 'btc:regtest'
    b.r['getinfo']['network'] = 'signet'
    app.poll_once()
    assert app.nodes['b'].net == 'btc:signet'


def test_a_hung_node_keeps_one_snapshot_in_flight():
    gate = threading.Event()
    h = fakes.bitcoin(2)
    app = make_app({'a': fakes.bitcoin(1), 'h': h})
    h.r['listfunds'] = lambda p: gate.wait(30) and {'outputs': []}
    app.NODE_TIMEOUT = 0.3
    for _ in range(5):
        app.poll_once()
    assert node_of(app, 'h')['error'] == 'h: not answering' and node_of(app, 'a')['ok']
    assert len(h.called('listfunds')) == 2      # make_app's poll, then the hung one
    gate.set()


def test_cancel_stops_a_waiting_job():
    app, _ = full_app()
    started = threading.Event()

    def fn(ctx):
        started.set()
        ctx.wait_for('forever', lambda: False, timeout=60)
    j = app.jobs.start('wait', None, fn)
    started.wait(5)
    assert app.jobs.cancel(j)
    wait_until(lambda: not app.jobs.running())
    job = app.jobs.get(j)
    assert job['status'] == 'cancelled' and 'cancelled' in job['log'][-1]


def test_coreminer_refuses_a_non_regtest_chain():
    class Rpc:
        name = 'bitcoind'

        def call(self, method, *params):
            assert method == 'getblockchaininfo'
            return {'chain': 'main'}
    with pytest.raises(ui.Fail, match='not regtest'):
        ui.CoreMiner(Rpc()).mine(1)


def test_scrubs_bare_api_keys():
    assert 'abcdef0123456789abcd' not in ui.Scrubber().text('infura.io/v3/abcdef0123456789abcd refused')


def test_odd_requests_get_400_not_a_dropped_connection():
    app, _ = mixed_app()
    srv = ui.serve(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port, token = srv.server_address[1], srv.RequestHandlerClass.token
    try:
        for body, headers in (('[' * 5000 + ']' * 5000, {}), ('{}', {'X-NodeUI-Token': 'tök'})):
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
            h = {'Content-Type': 'application/json', 'X-NodeUI-Token': token}
            h.update(headers)
            c.putrequest('POST', '/api/job')
            for k, v in h.items():
                c.putheader(k, v.encode('utf-8') if isinstance(v, str) else v)
            c.putheader('Content-Length', str(len(body)))
            c.endheaders(body.encode())
            assert c.getresponse().status in (400, 403)
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- RPC credits, progress, the reserve
def test_evm_status_off_anvil_is_cached_every_300s_with_refresh():
    t = fakes.evm(1, chain_id=84532, contract=CONTRACT)
    app = make_app({'t': t})
    reads = lambda: t.called('evm-status')
    assert app.evm_every(app.nodes['t']) == 300
    assert reads()[-1] == {'cached': True}, 'the poll asks for no fresh balance reads'
    n0 = len(reads())
    for _ in range(5):
        app.poll_once()
    assert len(reads()) == n0, 'cached for 300 s'
    # Refresh: one plain (fresh) read, then at most one per 30 s.
    assert app.refresh_node('t') == 0
    app.poll_once()
    assert len(reads()) == n0 + 1 and reads()[-1] == {}
    assert 0 < app.refresh_node('t') <= 30
    app.poll_once()
    assert len(reads()) == n0 + 1
    ev = node_of(app, 't')['evm']
    assert ev['refresh_wait_s'] > 0 and ev['read_age_s'] == 0 and ev['poll_every_s'] == 300
    # Past 300 s: cached again.
    app.cache[('t', 'evm')] = (time.time() - 301, app.cache[('t', 'evm')][1])
    app.poll_once()
    assert reads()[-1] == {'cached': True}


def test_evm_status_falls_back_when_the_plugin_has_no_cached():
    t = fakes.evm(1, chain_id=84532, contract=CONTRACT)

    def status(p):
        if 'cached' in p:
            raise ui.RpcError('evm-status', p, {'code': -32602, 'message': "unexpected keyword argument 'cached'"})
        return dict(t.status)
    t.r['evm-status'] = status
    app = make_app({'t': t})
    assert node_of(app, 't')['evm']['gas_text'] and app.nodes['t'].status_cached is False
    app.cache.clear()
    n0 = len(t.called('evm-status'))
    app.poll_once()
    assert t.called('evm-status')[n0:] == [{}], 'no more tries with cached'


def test_anvil_keeps_fast_plain_polling():
    e = fakes.evm(1)
    app = make_app({'e': e})
    assert app.evm_every(app.nodes['e']) == ui.App.POLL
    assert all(p == {} for p in e.called('evm-status'))


def test_a_job_end_refreshes_its_evm_nodes_only():
    t1, t2 = fakes.evm(1, chain_id=84532, contract=CONTRACT), fakes.evm(2, chain_id=84532, contract=CONTRACT)
    app = make_app({'t1': t1, 't2': t2})
    n1, n2 = len(t1.called('evm-status')), len(t2.called('evm-status'))
    st, log, _ = run(app, {'action': 'invoice', 'node': 't1', 'amount': 5})
    app.poll_once()
    assert t1.called('evm-status')[n1:] == [{}] and len(t2.called('evm-status')) == n2


def test_open_off_the_miners_networks_ends_once_funded():
    t1, t2 = fakes.evm(1, chain_id=84532, contract=CONTRACT), fakes.evm(2, chain_id=84532, contract=CONTRACT)
    for x in (t1, t2):
        x.r['listconfigs']['configs']['funding-confirms'] = fakes.conf(3)
    t1.r['fundchannel'] = {'txid': 'ab' * 32, 'channel_id': 'cd' * 32, 'outnum': 0}
    app = make_app({'t1': t1, 't2': t2})
    st, log, res = run(app, {'action': 'open', 'frm': 't1', 'to': 't2', 'amount': 100000})
    assert st == 'done' and res['channel_id'] == 'cd' * 32
    assert any('opens after 3 confirmations' in x for x in log)
    # The page shows the funding depth from the channel state.
    connect(t1, t2, 'cd' * 32, state='CHANNELD_AWAITING_LOCKIN', scid='100x1x0')
    app.poll_once()
    c = next(c for c in app.state['channels'] if c['id'] == 'cd' * 32)
    assert c['state'] == 'opening' and c['confirm'] == {'depth': 2, 'need': 3}    # blockheight 101


def test_deposit_off_anvil_ends_when_mined_and_shows_progress():
    t = fakes.evm(1, chain_id=84532, contract=CONTRACT)
    app = make_app({'t': t})
    st, log, _ = run(app, {'action': 'deposit', 'node': 't', 'amount': 1000})
    assert st == 'done' and any('tip reaches height 130' in x for x in log)
    app.poll_once()
    d = node_of(app, 't')['deposits']
    assert d == [{'sat': 1000, 'txid': 'ee' * 32, 'height': 130, 'in_wallet': False}]
    t.r['listfunds']['outputs'].append({'txid': 'ee' * 32, 'output': 0, 'amount_msat': 10**6,
                                        'status': 'unconfirmed'})
    app.poll_once()
    assert node_of(app, 't')['deposits'][0]['in_wallet'] is True
    assert node_of(app, 't')['wallet']['unconfirmed'] == 1000
    t.r['listfunds']['outputs'][-1]['status'] = 'confirmed'
    app.poll_once()
    assert node_of(app, 't')['deposits'] == []


def test_exit_all_says_the_emergency_reserve_stays():
    app, f = full_app()
    e1 = f['e1']
    e1.r['listfunds']['outputs'] = [{'txid': 'aa' * 32, 'output': 1, 'amount_msat': 25_000_000,
                                     'status': 'unconfirmed'}]
    calls = [0]

    def status(p):
        calls[0] += 1
        return dict(e1.status, withdrawable_wei=0 if calls[0] == 1 else 10**15)
    e1.r['evm-status'] = status
    st, log, _ = run(app, {'action': 'exit', 'node': 'e1', 'all': True})
    assert st == 'done', log
    assert any("25,000 sat stayed in e1's wallet: CLN keeps 25,000 sat (min-emergency-msat)" in x for x in log)
    app.poll_once()
    w = node_of(app, 'e1')['wallet']
    assert w['reserve'] == 25_000 and 'about 100 blocks after a close' in w['reserve_hint']
    # No channels known: no hint (CLN keeps no reserve then).
    assert 'reserve' not in node_of(app, 't1')['wallet']


def test_review_fixes_for_jobs_and_refresh():
    # A failed fresh read is tried again at the next poll.
    t = fakes.evm(1, chain_id=84532, contract=CONTRACT)
    app = make_app({'t': t, 'b': fakes.bitcoin(2)})
    good = t.r['evm-status']
    t.r['evm-status'] = ui.RpcError('evm-status', {}, {'code': -1, 'message': 'timeout'})
    assert app.refresh_node('t') == 0
    app.poll_once()
    assert 't' in app.fresh
    t.r['evm-status'] = good
    app.poll_once()
    assert 't' not in app.fresh and t.called('evm-status')[-1] == {}
    # A job whose node moved to another network meanwhile does not run.
    ran = []
    app.nodes['b'].net = 'btc:signet'
    j = app.jobs.start('x', 'btc:regtest', lambda ctx: ran.append(1), ('b',), wait=True)
    assert app.jobs.get(j)['status'] == 'error' and not ran and 'btc:signet' in app.jobs.get(j)['log'][-1]
    # Refresh is for EVM nodes.
    srv = ui.serve(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port, token = srv.server_address[1], srv.RequestHandlerClass.token
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        h = {'Content-Type': 'application/json', 'X-NodeUI-Token': token}
        c.request('POST', '/api/refresh', json.dumps({'node': 'b'}), h)
        assert c.getresponse().status == 400
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        c.request('POST', '/api/refresh', json.dumps({'node': 't'}), {'Content-Type': 'application/json'})
        assert c.getresponse().status == 403, 'the token is needed'
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_password_that_is_a_network_name_does_not_poison_the_state():
    """rpcpassword=regtest must not turn every 'btc:regtest' into 'btc:<secret>';
    inside a URL the password is still removed."""
    sc = ui.Scrubber()
    sc.add('regtest', 's3cretpw')
    assert sc.deep({'net': 'btc:regtest', 'e': 'bad s3cretpw'}) == {'net': 'btc:regtest', 'e': 'bad <secret>'}
    assert 'regtest' not in sc.text('see http://u:regtest@127.0.0.1:18443/') and '<' in sc.text('u:regtest@h')


@pytest.mark.parametrize('amount', [0, -5, '0', 10**16])
def test_amounts_outside_one_to_max_are_refused(amount):
    app, _ = full_app()
    with pytest.raises(ui.BadRequest, match='amount'):
        ui.build_job(app, {'action': 'pay', 'frm': 'b1', 'to': 'b2', 'amount': amount})
