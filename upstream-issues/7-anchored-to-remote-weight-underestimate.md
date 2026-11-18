# wallet: fee estimate for spending an anchor channel's `to_remote` output leaves out its witness script

**Version:** master at `v26.06-636-g41362d38a`

## What happens

When a peer closes an anchor channel unilaterally, our balance is the
`to_remote` output: P2WSH, `<remotepubkey> OP_CHECKSIGVERIFY 1
OP_CHECKSEQUENCEVERIFY`. The wallet keeps it as a coin of type
`UTXO_P2WSH_FROM_CLOSE` and spends it like any other coin (`withdraw`,
`fundchannel`, `fundpsbt`...).

`bitcoin_tx_input_witness_weight()` in `bitcoin/tx.c` sizes that coin's
witness as a signature only:

```c
	case UTXO_P2WSH_FROM_CLOSE:
		...
		/* In practice, these predate anchors, so: */
		return 1 + 1 + bitcoin_tx_input_sig_weight();
```

The comment has it backwards: `wallet/wallet.c` sets this type only when
`close_info->option_anchors` is set (a pre-anchor `to_remote` is plain P2WPKH
and gets `UTXO_P2WPKH`). Being P2WSH, the witness carries the 37-byte script
too, which `hsmd` adds when it signs. So every such input is estimated 36
weight units too light, and the transaction pays less than the feerate asked
for.

At ordinary feerates that is a slightly low fee. At the floor (253 perkw, one
sat per vbyte) it puts the transaction under bitcoind's minimum relay fee, and
the broadcast is refused.

## How it showed up

On regtest, where fee estimates sit at the floor: a node that had won a
penalty close held four coins, one of them such a `to_remote` output.
`fundchannel` picked all four and failed with

```
Error broadcasting transaction: error code: -26
error message:
min relay fee not met, 332 < 338
```

The channel had already been negotiated, so it was left waiting for a funding
transaction that never reached the chain, and had to be removed with
`dev-forget-channel` on both sides. Spending the coin alone with
`withdraw ... 253perkw` was refused the same way.

## Reproduce

`tests/test_wallet.py::test_withdraw_anchored_to_remote_feerate` (in the
patch): `l1` pays `l2`, `l1` drops to chain, and `l2` withdraws its `to_remote`
coin at `253perkw`. The transaction weighs 613 (witness: a 71-byte signature
and the 37-byte script). Before the fix it pays 145 sat, the fee for a weight
of 576; with the fix it pays 155 sat, the fee for 613.

## Fix

Count the script:

```c
-		/* In practice, these predate anchors, so: */
-		return 1 + 1 + bitcoin_tx_input_sig_weight();
+		/* We only use this type for the anchor variant, and being
+		 * P2WSH its witness also carries the script itself:
+		 * a 33-byte key push, OP_CHECKSIGVERIFY, OP_1 and
+		 * OP_CHECKSEQUENCEVERIFY. */
+		return bitcoin_tx_input_sig_weight() + 1 + (1 + 33 + 1 + 1 + 1);
```

Not covered by the patch: with `option_will_fund` the CSV value in a leased
channel's `to_remote` script can take up to two more bytes
(`is_to_remote_anchored_witness_script` allows for them). The function only
gets the coin's type, not its CSV, so such a coin would still be estimated up
to two weight units too light.

Patch: commit "wallet: count the witness script when sizing an anchored
to_remote input" (`999b0635d` on branch `generic-fixes`).
