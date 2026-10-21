#!/usr/bin/env python3
"""Plugin which holds every incoming HTLC until `releasehtlcs` is called.

Then they all continue: we fulfill the ones we have an invoice for, and fail
the others.  Lets a test keep HTLCs in flight (committed, but unresolved)
for as long as it wants, e.g. until the channel is closed on-chain.
"""
from pyln.client import Plugin


plugin = Plugin()


@plugin.async_hook('htlc_accepted')
def on_htlc_accepted(htlc, request, plugin, **kwargs):
    plugin.log("holding htlc {}".format(htlc['payment_hash']))
    plugin.held.append(request)


@plugin.method('releasehtlcs')
def releasehtlcs(plugin):
    """Let every HTLC we're holding continue."""
    held, plugin.held = plugin.held, []
    for request in held:
        request.set_result({'result': 'continue'})
    return {'released': len(held)}


@plugin.init()
def init(**kwargs):
    plugin.held = []


plugin.run()
