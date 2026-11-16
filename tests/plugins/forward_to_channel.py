#!/usr/bin/env python3
"""After `forward-to channel_id`, sends every forwarded HTLC to that
channel, by answering htlc_accepted with it as `forward_to`.  Its hook runs
after the asset channel plugin's (contrib/assetchan), so it has the last
word: a test that core itself keeps forwards within an asset."""
from pyln.client import Plugin

plugin = Plugin()
plugin.target = None


@plugin.method('forward-to')
def forward_to_method(plugin, channel_id):
    plugin.target = channel_id
    return {'forward_to': channel_id}


@plugin.hook('htlc_accepted', after=['assetchan.py'])
def on_htlc_accepted(plugin, onion, forward_to=None, **kwargs):
    if forward_to is None or plugin.target is None:
        return {'result': 'continue'}
    plugin.log('forwarding to {} instead of {}'.format(plugin.target, forward_to))
    return {'result': 'continue', 'forward_to': plugin.target}


plugin.run()
