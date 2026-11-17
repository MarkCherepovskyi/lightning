# Elements: an output with an explicit asset and a confidential value is read as an amount

**Version:** master at `v26.06-636-g41362d38a`

## What happens

`wally_tx_output_get_amount` (`bitcoin/tx.c`) decides whether an Elements
output is readable by looking only at the asset tag. If the asset is explicit
(`asset[0] == 0x01`), it copies 8 bytes from `value + 1` and returns them as
the amount.

An Elements output can have an explicit asset and a confidential value. The
value field is then a 33-byte commitment, not `0x01` followed by 8 bytes, and
the function returns part of the commitment as a satoshi amount. If the asset
is L-BTC, callers that check `amount_asset_is_main` treat that number as a
real L-BTC amount.

## How to reproduce

Found by code review; there is no test for it yet, and it was not reproduced
on a live chain. Any transaction with an output whose asset is explicit and
whose value is blinded reaches this path.

## Fix

Only read the amount when the value is explicit too (`value_len == 9` and
`value[0] == 0x01`). Otherwise treat the output as unknown, like a fully
blinded one: amount 0 and a zeroed asset, so it can never match L-BTC.

Patch: commit "bitcoin: don't read a confidential value as an amount on
Elements" (`98bcd6450` on branch `generic-fixes`).
