# lightningd: aborts when a reorg puts a funding tx back at an scid it had before

**Version:** master at `v26.06-636-g41362d38a`

## What happens

When a reorg moves the funding transaction of an announced channel,
`change_scid()` (`lightningd/channel_control.c`) sets the new scid and keeps
the previous one:

```c
	channel_set_scid(channel, &scid);
	/* In case we broadcast it before (e.g. splice!) */
	channel_add_old_scid(channel, old_scid);
```

`channel_add_old_scid()` appends it to `channel->old_scids` and also adds it to
`ld->channels_by_scid`, which is a `HTABLE_DEFINE_NODUPS_TYPE` map.

If a second reorg puts the funding transaction back at its first position, the
scid we are given is that old one. `channel_set_scid()` adds it to the map a
second time, and `lightningd` aborts:

```
INFO    ...-chan#4: Short channel id changed from 162187x10x0->162186x9x0
**BROKEN** lightningd: FATAL SIGNAL 6
backtrace: ./lightningd/channel.h:915 (channel_scid_map_add)
backtrace: lightningd/channel.c:298 (chanmap_add)
backtrace: lightningd/channel.c:315 (channel_set_scid)
backtrace: lightningd/channel_control.c:845 (change_scid)
backtrace: lightningd/channel_control.c:876 (channel_apply_scid)
backtrace: lightningd/channel_control.c:900 (depthcb_update_scid)
backtrace: lightningd/peer_control.c:2714 (channel_funding_found)
backtrace: lightningd/watch.c:528 (watch_check_tx_outputs)
backtrace: lightningd/chaintopology.c:92 (filter_block_txs)
backtrace: lightningd/chaintopology.c:1030 (add_tip)
```

(Line numbers are from our branch.)

The node then dies the same way on every start: the changed scid and
`old_scids` are in the database, and the replay of the chain finds the funding
transaction at the old scid again.

It does not need a live channel. A channel that is already closed and
`ONCHAIN` still has its funding watched until it is forgotten, 100 blocks
later, and its scid is updated the same way.

## How it showed up

Three nodes on testnet4, each with channels funded a few blocks earlier (one
closed cooperatively, one unilaterally). testnet4 reorganized six blocks, which
moved the funding transactions one block up (`162186x9x0` to `162187x10x0`),
and a little later reorganized again, which moved them back. All three nodes
aborted within two seconds of each other.

## Reproduce

`tests/test_misc.py::test_funding_reorg_back_to_old_scid` (in the patch): two
zeroconf nodes (so the channel survives its funding being reorged out) with an
announced channel at `106x1x0`; `bitcoind.simple_reorg(106, 2)` moves the
funding to block 108, `bitcoind.simple_reorg(106, 0)` moves it back. With the
fix both nodes log "Short channel id changed from 108x1x0->106x1x0", keep
running, and one of them restarts cleanly.

## Fix

In `channel_set_scid()`, before taking the new scid: if it is one of the
channel's `old_scids`, remove it from that array and from the map.

```c
static void channel_forget_old_scid(struct channel *channel,
				    struct short_channel_id scid)
{
	for (size_t i = 0; i < tal_count(channel->old_scids); i++) {
		if (!short_channel_id_eq(channel->old_scids[i], scid))
			continue;
		chanmap_remove(channel->peer->ld, channel, scid);
		tal_arr_remove(&channel->old_scids, i);
		return;
	}
}
```

Not covered by this: an scid that is in the map as an old scid of *another*
channel (a reorg moved that channel away and a different funding transaction
landed on its old position) would still hit the same abort.

Patch: in the `generic-fixes` worktree, not committed yet.
