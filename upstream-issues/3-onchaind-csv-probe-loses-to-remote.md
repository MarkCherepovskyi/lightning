# onchaind: a channel restored from a backup can miss its own `to_remote` output

**Version:** master at `v26.06-636-g41362d38a`

## What happens

When the peer's commitment transaction has an output that onchaind cannot
identify, and that output comes before our `to_remote` output, onchaind does
not recognise our `to_remote` either. The funds are ours but are never added
to the wallet.

This can always happen on a channel restored from a static channel backup
(`emergencyrecover`, `recoverchannel`): the stub does not know the peer's
funding key, so it cannot place their anchor output.

## How to reproduce

`test_emergencyrecoverpenaltytxn` (`tests/test_misc.py`) already sets this up.
It only checked that the penalty output is in `listfunds`. With one more
assertion, that our `to_remote` on the peer's commitment is there too, it
fails on master:

```
AssertionError: assert '82eacf91...d906' in ['57b88be4...84f8']
```

## Cause

In `handle_their_cheat` and `handle_their_unilateral` (`onchaind/onchaind.c`),
an unidentified output is probed against other CSV values (2 to
`LEASE_RATE_DURATION`) for a leased `to_remote` or `to_local`. Each probe is
written into `script[LOCAL]` or `script[REMOTE]`, the same variables that hold
the scripts onchaind expects for our real outputs. After the loop they hold
the last probed script, so a later output that is the ordinary (CSV 1) one no
longer matches. There are four such loops, two in each function.

## Fix

Keep each probed script in a local variable and leave `script[]` alone.

Patch: commit "onchaind: don't overwrite our expected scripts when probing CSV
values" (`787481da5` on branch `generic-fixes`), which also adds the assertion
to the test.
