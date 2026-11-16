#!/usr/bin/env python3
"""Answers `reject` to one of the dual-funding hooks (`df-reject-hook`:
openchannel2_changed or openchannel2_sign), ahead of the funder plugin.
lightningd must not touch the hook's payload after that (it used to, from
a destructor on dualopend: a use-after-free)."""
from pyln.client import Plugin

plugin = Plugin()


@plugin.hook('openchannel2_changed', before=['funder'])
def on_changed(plugin, openchannel2_changed, **kwargs):
    if plugin.get_option('df-reject-hook') == 'openchannel2_changed':
        plugin.log('rejecting openchannel2_changed')
        return {'result': 'reject'}
    return {'result': 'continue', 'psbt': openchannel2_changed['psbt']}


@plugin.hook('openchannel2_sign', before=['funder'])
def on_sign(plugin, openchannel2_sign, **kwargs):
    if plugin.get_option('df-reject-hook') == 'openchannel2_sign':
        plugin.log('rejecting openchannel2_sign')
        return {'result': 'reject'}
    return {'result': 'continue', 'psbt': openchannel2_sign['psbt']}


plugin.add_option('df-reject-hook', '', 'openchannel2_changed or openchannel2_sign')
plugin.run()
