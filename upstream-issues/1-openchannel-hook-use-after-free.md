# lightningd crashes in the `openchannel` / `openchannel2` hooks (use-after-free)

**Version:** master at `v26.06-636-g41362d38a`

## What happens

Two related crashes in the channel-open hooks.

1. **`openchannel` with two plugins, one setting `close_to`.** When one plugin
   answers the `openchannel` hook with a `close_to` address and another plugin
   registered for the same hook answers after it, lightningd aborts in
   `openchannel_hook_final`. Any peer that opens a channel to such a node
   crashes it.

2. **`openchannel2_changed` / `openchannel2_sign` rejected by a plugin.** When
   a plugin's answer to either hook is rejected (no usable PSBT), lightningd
   aborts when dualopend goes away.

## How to reproduce

1. Start a node with `tests/plugins/openchannel_hook_accepter.py` and
   `tests/plugins/openchannel_hook_accept.py`, then open a 100003 sat channel
   to it (that amount makes the first plugin answer with `close_to`).
2. Start a node with a plugin that answers `openchannel2_changed` without a
   valid PSBT, then open a v2 channel to it.

Tests that do this: `test_openchannel_hook_close_to_then_another_plugin`
(`tests/test_plugin.py`) and `test_openchannel2_psbt_hook_reject`
(`tests/test_opening.py`, with `tests/plugins/df_reject.py`). Both fail on
master and pass with the fix.

## Backtraces on master

```
ccan/ccan/tal/tal.c:95 (call_error)
ccan/ccan/tal/tal.c:737 (tal_bytelen)
lightningd/opening_control.c:753 (openchannel_hook_final)
lightningd/plugin_hook.c:243 (hook_done)
lightningd/plugin_hook.c:343 (plugin_hook_call_next)
lightningd/plugin_hook.c:299 (plugin_hook_callback)
```

```
lightningd/dual_open_control.c:858 (openchannel2_psbt_remove_dualopend)
ccan/ccan/tal/tal.c:243 (notify)
ccan/ccan/tal/tal.c:541 (tal_free)
lightningd/subd.c:588 (sd_msg_read)
```

## Cause

1. `openchannel_hook_deserialize` (`lightningd/opening_control.c`) parses
   `close_to` onto `tmpctx`. With another plugin in the chain, `tmpctx` is
   freed before `openchannel_hook_final` uses the script.
   `openchannel2_hook_deserialize` keeps its shutdown script on `tmpctx` the
   same way.
2. In `lightningd/dual_open_control.c`, when `openchannel2_changed_deserialize`
   or `openchannel2_signed_deserialize` returns false, the hook machinery
   frees the payload, but dualopend's destructor
   (`openchannel2_psbt_remove_dualopend`) still points at it. The same happens
   in `openchannel2_hook_cb` when an open is already in progress.

## Fix

Parse `close_to` onto the payload (and steal the v2 shutdown script onto it),
and remove the dualopend destructor before the payload is freed.

Patch: commit "lightningd: fix use-after-frees in the openchannel and
openchannel2 hooks" (`5a4938116` on branch `generic-fixes`).
