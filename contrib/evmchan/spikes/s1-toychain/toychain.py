#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Backend plugin for spike S1: bcli's five methods, served from chaind.py
over HTTP.  Run lightningd with --disable-plugin=bcli --plugin=toychain.py
--toychain-url=http://127.0.0.1:PORT --network=regtest."""
from pyln.client import Plugin

import json
import urllib.request

plugin = Plugin(dynamic=False)


def chaind(method, *params):
    req = urllib.request.Request(plugin.get_option('toychain-url'),
                                 json.dumps({'method': method, 'params': list(params)}).encode(),
                                 {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())['result']


@plugin.method('getchaininfo')
def getchaininfo(plugin, last_height=None, **kwargs):
    tip = chaind('tip')
    return {'chain': 'regtest', 'headercount': tip, 'blockcount': tip, 'ibd': False}


@plugin.method('getrawblockbyheight')
def getrawblockbyheight(plugin, height, **kwargs):
    b = chaind('block', int(height))
    return b or {'blockhash': None, 'block': None}


@plugin.method('sendrawtransaction')
def sendrawtransaction(plugin, tx, allowhighfees=False, **kwargs):
    res = chaind('send', tx)
    if not res['success']:
        plugin.log('sendrawtransaction: {}'.format(res['errmsg']), level='debug')
    return res


@plugin.method('getutxout')
def getutxout(plugin, txid, vout, **kwargs):
    u = chaind('utxo', txid, int(vout))
    return u or {'amount': None, 'script': None}


@plugin.method('estimatefees')
def estimatefees(plugin, **kwargs):
    # sat/kVB: the floor, and what chaind says (constants unless a test
    # raises them: the EVM chain has no fee market in-tx)
    try:
        fees = chaind('fees')
    except Exception:
        fees = ((2, 5000), (6, 4000), (12, 3000), (100, 1000))
    return {'feerate_floor': 1000,
            'feerates': [{'blocks': b, 'feerate': r} for b, r in fees]}


plugin.add_option('toychain-url', None, 'URL of the toy chain (chaind.py)')
plugin.run()
