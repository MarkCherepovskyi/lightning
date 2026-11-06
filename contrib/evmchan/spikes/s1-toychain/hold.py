#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Spike helper: hold incoming HTLCs (never answer the hook until told to),
so a force close has HTLCs in flight.

By default every HTLC is held forever (run_s1.py).  With --hold-all=false only
the payment hashes given to `hold-add` are held; the rest continue at once.
`hold-release [payment_hash]` continues held HTLCs (all of them, or those with
that hash); `hold-list` shows what is held.  State is in memory only: after a
restart, replayed HTLCs are held only if --hold-all is set."""
from pyln.client import Plugin

plugin = Plugin()
plugin.held = []          # [(payment_hash, request)]
plugin.hashes = set()


@plugin.async_hook('htlc_accepted')
def on_htlc_accepted(plugin, htlc, onion, request, **kwargs):
    if plugin.get_option('hold-all') or htlc['payment_hash'] in plugin.hashes:
        plugin.held.append((htlc['payment_hash'], request))
    else:
        request.set_result({'result': 'continue'})


@plugin.method('hold-add')
def hold_add(plugin, payment_hash, **kwargs):
    plugin.hashes.add(payment_hash)
    return {'holding': sorted(plugin.hashes)}


@plugin.method('hold-list')
def hold_list(plugin, **kwargs):
    return {'held': [h for h, _ in plugin.held]}


@plugin.method('hold-release')
def hold_release(plugin, payment_hash=None, **kwargs):
    released, kept = [], []
    for h, req in plugin.held:
        if payment_hash is None or h == payment_hash:
            req.set_result({'result': 'continue'})
            released.append(h)
        else:
            kept.append((h, req))
    plugin.held = kept
    if payment_hash is not None:
        plugin.hashes.discard(payment_hash)
    return {'released': released}


plugin.add_option('hold-all', True, 'hold every incoming HTLC', opt_type='bool')
plugin.run()
