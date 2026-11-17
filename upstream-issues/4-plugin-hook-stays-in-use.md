# A plugin restarted after a hook chain was ended early is never called for that hook again

**Version:** master at `v26.06-636-g41362d38a`

## What happens

Some hook answers end the hook chain early: `htlc_accepted` with `fail` or
`resolve`, `peer_connected` with `disconnect`. After such an answer the hook
stays counted as "in use" forever. A plugin that registers for that hook
later (for example after `plugin stop` and `plugin start`) is never called.
The log says:

```
Deferring registration of hook htlc_accepted until it's not in use.
```

For `htlc_accepted` this means HTLCs silently bypass the restarted plugin.

## How to reproduce

1. Start a node with `tests/plugins/fail_htlcs.py`.
2. Pay an invoice of that node: the plugin fails the HTLC.
3. `plugin stop` and `plugin start` the plugin.
4. Pay another invoice: the new plugin instance is not called.

The same with `tests/plugins/reject.py` and `peer_connected`.

Tests: `test_htlc_accepted_hook_replaced_after_fail` and
`test_peer_connected_hook_replaced_after_disconnect` (`tests/test_plugin.py`).
Both fail on master and pass with the fix.

## Cause

`plugin_hook_callback` (`lightningd/plugin_hook.c`) frees the request and
returns when `deserialize_cb` returns false, without the `num_users`
decrement that `hook_done` does. Hooks registered while `num_users > 0` are
parked in `new_hooks` and only applied when it reaches zero, which now never
happens.

## Fix

Split the decrement out of `hook_done` into `hook_release` and call it on the
early-return path too.

Patch: commit "lightningd: release a hook when a plugin's answer ends the
chain" (`647568700` on branch `generic-fixes`).
