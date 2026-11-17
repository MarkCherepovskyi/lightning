# bcli: fake fee estimates on `liquid-regtest`, as on `regtest`

**Version:** master at `v26.06-636-g41362d38a`

This is a small improvement, not a bug in production use.

## What happens

`plugins/bcli.c` fakes fee estimates when the network is exactly `regtest`,
because a fresh regtest chain has no fee history. `elementsd` on
`liquid-regtest` has none either, but the check does not match that name, so
nodes there get no estimates and tests have to set feerates by hand.

## Fix

Match any network whose name ends in `regtest`:

```c
-	if (streq(chainparams->network_name, "regtest"))
+	if (strends(chainparams->network_name, "regtest"))
```

Patch: commit "bcli: fake fee estimates on liquid-regtest as on regtest"
(`e300e9dee` on branch `generic-fixes`).
