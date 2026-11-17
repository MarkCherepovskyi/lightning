# After a restart, onchaind's replay can miss spends and never sweep an HTLC transaction's output

**Version:** master at `v26.06-636-g41362d38a`

## What happens

When lightningd restarts with a channel that is already closed on chain, it
replays the blocks since the close to a new onchaind. Two bugs in that replay
leave an output of ours unswept.

1. **A spend reaches onchaind before it knows the HTLCs and preimages.**
   onchaind gets those only after it replies to init. If the block that
   starts the replay also contains our own HTLC-success transaction, onchaind
   hears of that spend first and takes it for the peer's. The output our
   HTLC-success transaction creates is then never swept.

2. **A transaction that spends two watched outputs is dropped too early.**
   onchaind is asked about each watched output a transaction spends. Replay
   dropped its watch on the whole transaction as soon as onchaind said it was
   not interested in one of the inputs. If onchaind cared about the other
   input, it never saw the spend of that transaction's outputs, and never
   finished. This happens when our HTLC-timeout transaction takes the output
   of our own `to_local` sweep as its fee input.

## How to reproduce

1. `test_onchaind_rescan_htlc_success` (`tests/test_closing.py`): the close
   and our HTLC-success transaction are mined in one block, and the node is
   restarted with a rescan.
2. `test_onchaind_replay_tx_spending_two_watched` (`tests/test_closing.py`):
   an anchor channel is closed unilaterally with an HTLC in flight, and the
   only wallet coin is the `to_local` sweep's output, so the HTLC-timeout
   transaction spends it; the node is restarted after the second-stage sweep.

Both time out on master and pass with the fix.

## Cause

In `lightningd/onchain_control.c`:

1. `onchaind_restart` started the replay right after sending init, and
   `onchaind_funding_spent` only replayed after a restart. Spends could be
   sent before `handle_onchain_init_reply` had told onchaind about HTLCs and
   preimages.
2. `onchaind_spent_reply` called `replay_unwatch_txid` on the first "not
   interested" answer, without counting the other requests about the same
   transaction.

## Fix

- Always set up a replay from the block of the funding spend, and start it
  from `handle_onchain_init_reply`.
- Count the pending requests per replayed transaction, and drop it only when
  all are answered and none was wanted.
- Smaller points found on the way: record the height of a replayed
  transaction in the wallet (topology may have passed its block before anyone
  watched it, leaving its depth at 0), ignore a block reply for a height the
  replay no longer asks for, and tie the block request to the replay it
  belongs to.

Patch: commit "lightningd: fix onchaind replay missing spends after a restart"
(`e869f9a9e` on branch `generic-fixes`).
