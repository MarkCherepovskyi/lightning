# lightningd: `fundchannel_start` crashes if the peer disconnected while it waited for sync

**Version:** master at `v26.06-636-g41362d38a`

## What happens

`fundchannel_start` on a node that has not caught up with the chain is parked
until it has ("Waiting to sync with bitcoind network"), then resumed by
`fundchannel_start_after_sync()` in `lightningd/opening_control.c`. That
function looks the peer up again, and if the peer is known but no longer
connected it fails the command, but does not return:

```c
	if (peer->connected != PEER_CONNECTED)
		was_pending(command_fail(info->cmd, FUNDING_PEER_NOT_CONNECTED,
					 "Peer %s",
					 peer->connected == PEER_DISCONNECTED
					 ? "not connected" : "still connecting"));
	fundchannel_start(info->cmd, peer, info->fc,
			  &info->tmp_channel_id, info->mindepth, info->reserve);
```

`command_fail()` frees the command, and with it `info->fc` and
`info->reserve`. `fundchannel_start()` then steals the freed `fc`, and
`lightningd` dies:

```
lightningd: FATAL SIGNAL 6
backtrace: ccan/ccan/tal/tal.c:95 (call_error)
backtrace: ccan/ccan/tal/tal.c:169 (check_bounds)
backtrace: ccan/ccan/tal/tal.c:180 (to_tal_hdr)
backtrace: ccan/ccan/tal/tal.c:571 (tal_steal_)
backtrace: lightningd/opening_control.c:1330 (fundchannel_start)
backtrace: lightningd/opening_control.c:1382 (fundchannel_start_after_sync)
backtrace: lightningd/chaintopology.c:918 (updates_complete)
```

(Line numbers are from our branch; SIGSEGV instead of SIGABRT in the test
below.)

The peer has to be one we keep while it is disconnected, i.e. one we have a
channel with; an unknown peer takes the branch above, which does return.

## How it showed up

A node was restarted and `fundchannel` was called at once, towards a peer it
already had (closed) channels with. The node was still syncing, the peer had
not reconnected when the sync finished, and the node went down. The coins the
open had picked stayed reserved for 2016 blocks.

## Reproduce

`tests/test_misc.py::test_fundchannel_start_peer_gone_while_syncing` (in the
patch): two nodes with a channel; `l1` restarts with its last block held
back, `fundchannel_start` towards `l2` parks, `l2` stops, the block is
released. Before the fix `l1` exits on a fatal signal; with it the command
fails with "Peer not connected" and `l1` keeps running.

## Fix

```c
-	if (peer->connected != PEER_CONNECTED)
+	if (peer->connected != PEER_CONNECTED) {
 		was_pending(command_fail(info->cmd, FUNDING_PEER_NOT_CONNECTED,
 					 "Peer %s",
 					 peer->connected == PEER_DISCONNECTED
 					 ? "not connected" : "still connecting"));
+		return;
+	}
```

Patch: commit `19c3a24ca` on `generic-fixes` (merged into `feat/clean-up`).
