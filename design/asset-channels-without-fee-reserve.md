# Asset channels without the fee reserve

Status: proposal, 2026-11-17. Nothing here is implemented.

## 1. Summary

Today an asset channel locks a second output of L-BTC, the fee reserve, next
to the asset funding output, and every commitment and closing transaction
spends both. The reserve exists for one reason: Elements 23 cannot relay a
commitment that pays no fee, so the fee has to be inside the commitment.

Elements 29 can. A version-3 commitment that pays no fee and holds no L-BTC is
accepted and relayed together with a child transaction that pays the fee.

This design drops the reserve. A commitment then has one input (the asset
funding output), as on Bitcoin, and whoever broadcasts it pays the fee from
their own wallet through an anchor spend.

It only works on a network whose nodes run Elements 29 or later. Section 9
lists what that means.

## 2. What was tested

On 2026-11-17, with two connected regtest nodes running Elements 29.4.1rc1
built from the tagged source:

| Case | Result |
|---|---|
| Version-3 parent, zero fee, asset only, one output of 1 unit of the asset as anchor. Version-3 child spends the anchor and a wallet L-BTC coin and pays the fee | Accepted by `submitpackage`, relayed to the second node, mined |
| Same with version 2 | Refused: `min relay fee not met, 0 < 22` |
| Version-3 parent with a 0-value L-BTC anchor | Refused: `bad-txns-in-ne-out` (cause not investigated) |

The parent alone is refused with `min relay fee not met`.

Not tested: real commitment scripts (the test used single-signature outputs;
script type does not enter relay policy), anything in CLN, and any public
Liquid network.

## 3. The new commitment transaction

Changes against today's asset commitment:

| | Today | Proposed |
|---|---|---|
| Version | 2 | 3 |
| Inputs | asset funding output, L-BTC fee reserve | asset funding output |
| Signatures per commitment | two from each side (the second in TLV `fee_reserve_sig`) | one from each side, as upstream |
| Anchors | 330 sat of L-BTC each, from the reserve | 1 unit of the asset each, from the opener's balance |
| Fee output | reserve minus anchors | none |
| `to_local`, `to_remote`, HTLC outputs | in the asset | unchanged |

- **Anchors keep their script.** They stay the BOLT 3 keyed anchors
  (`<funding_pubkey> OP_CHECKSIG OP_IFDUP OP_NOTIF 16 OP_CSV OP_ENDIF`), so
  onchaind and `anchorspend.c` recognise and sign them as today. Only the
  asset tag and the amount change.
- **The opener pays the two anchor units.** Upstream subtracts 660 sat from
  the funder for anchors; asset channels skip that today because the reserve
  pays. The deduction comes back, as 2 units of the asset.
- **HTLC transactions stay version 2.** They spend a confirmed commitment
  (their input has a 1-block CSV), so the version-3 rules do not reach them.
  They remain zero-fee with wallet L-BTC added by the broadcaster.

## 4. Getting a commitment mined

A commitment can no longer confirm by itself, so every unilateral close needs
an anchor spend.

- **Child transaction.** Version 3. Inputs: our anchor and wallet L-BTC.
  Outputs: the anchor's 1 unit of the asset to our close script, L-BTC change
  to the wallet, and the fee. It must stay at or under 1,000 vB, so coin
  selection has to limit the number of wallet inputs.
- **Always built.** `anchorspend.c` today decides whether a bump is worth it.
  For asset channels it must always produce the child when it broadcasts a
  commitment.
- **Submitted as a package.** Our own node refuses the parent alone, so
  lightningd must hand both transactions to the backend together. Core has
  no such call today (only `sendrawtransaction`). This needs a new backend
  method, `submitpackage`, in `plugins/bcli.c` and `lightningd/bitcoind.c`,
  and the commitment broadcast path must use it. This part is generic: the
  same call is what zero-fee commitments on Bitcoin need.
- **Bumping.** The child is replaced with a higher-fee one, as anchor spends
  are today. Version 3 allows one unconfirmed child, so only one side's
  anchor spend is in the mempool at a time.

## 5. Funding

The funding transaction has one channel output, the asset. The second output
and its checks go away:

- `fundchannel_start` loses `fee_reserve`.
- `fundchannel_complete` checks only output n (asset, amount, explicit).
- The plugin's `assetchan-open` no longer needs an L-BTC coin for the reserve
  or the rule that the reserve directly follows the funding output.

## 6. Mutual close

There is no L-BTC in the channel any more, so a closing transaction needs an
L-BTC input from one of the two wallets. This is the one place where the new
design adds protocol.

Proposed: the side that sends the closing offer pays, with its own input.

- The closer adds its wallet L-BTC input, an L-BTC change output and the fee
  to the closing transaction, and names them in a new TLV on its offer.
- The other side checks that the asset outputs are right and that it
  contributes nothing, and signs the funding input over the whole
  transaction.
- The closer signs the funding input and its own wallet input, and
  broadcasts. The transaction pays its own fee, so it is an ordinary
  version-2 transaction.

This fits `option_simple_close`, where each side already makes its own
closing transaction and pays its own fee. Asset channels skip simple close
today; they would use it instead of the legacy negotiation.

A side with no wallet L-BTC cannot offer a close; the other side can.

## 7. What changes in the code

Removed (about 570 lines by a rough per-function count):

- `add_fee_reserve_input`, `check_fee_reserve_sig`, `fee_reserve_outpoint`,
  and `fee_reserve` in `struct channel_asset`.
- TLVs `fee_reserve` (4245) and `fee_reserve_sig` (4241), and their handling
  in openingd, channeld and closingd.
- hsmd's `handle_sign_funding_sibling_input` and
  `spends_funding_and_sibling`. hsmd then signs asset commitments with
  upstream's messages only.
- `fee_reserve_output_ok` and the second-output check in the funding watch.
- The reserve part of `sign_last_tx` and of the closing checks.
- `asset_commit_max_htlcs` as a calculation (see below).

Added (about 380 to 400 lines, an estimate made before writing any of it):

| Piece | Lines | Asset-specific? |
|---|---|---|
| `submitpackage` in the backend interface, and broadcasting a commitment with its child | ~160 | No, generic |
| Mutual close with the closer's L-BTC input | ~120 to 150 | Yes |
| Version 3 and asset anchors on commitments, anchor spend always built and returning its asset unit | ~80 | Yes |
| A fee budget option to replace "one fee reserve per transaction" in `asset_fee_cap` | ~15 | Yes |

Net effect on the asset delta: about 2,440 lines today, about 2,100 after,
plus about 160 generic lines that can go upstream. That is less than the
first estimate of about 1,800, because package submission and the new
mutual close are needed in exchange.

The larger gain is in shape, not size: an asset commitment becomes an
upstream anchor commitment with asset tags. One input, upstream's signing
messages, no extra signature in every `commitment_signed`, and a plain
funding transaction.

Other effects:

- **No fee is wasted on close.** Today every close pays the whole reserve
  minus the anchors as fee, whatever the feerate.
- **The HTLC cap becomes a constant.** It no longer depends on the reserve.
  A version-3 transaction may be at most 10,000 vB; at about 78 vB per HTLC
  output that allows roughly 120 HTLCs in total, 60 per side. The exact
  number has to be computed during implementation.
- **`asset_fee_cap` needs a new unit.** It is defined in fee reserves today.
  A node-wide option (for example 2,000 sat per transaction, ten times that
  near a deadline) replaces it.

## 8. What gets worse

- **A node with no wallet L-BTC cannot force-close.** Today the reserve lets
  the commitment itself relay. In the new design nothing confirms without a
  child. The plugin should refuse to open or accept an asset channel unless
  the CLN wallet holds a minimum of L-BTC, in the way upstream keeps
  `min-emergency-msat` for anchor channels.
- **Two units of the asset per channel sit in anchors.** They come from the
  opener. Ours is returned by our anchor spend; an unspent anchor can be
  swept by anyone after 16 blocks. For an asset whose smallest unit is worth
  something (an asset issued with no decimal places), that is a real cost.
- **Mutual close is new code** on a path that is simple today.

## 9. Preconditions and open questions

- **Deployment.** Released Elements is 23.3.4; 29.4.1 is a release
  candidate. The design needs 29 or later on our own nodes, on the nodes
  that relay, and on the block signers. A 23.x node refuses both
  transactions. What Liquid testnet and mainnet run, and when they move, is
  a question for the Elements team.
- **Version-3 rules.** I relied on Bitcoin Core's rules (one unconfirmed
  child, child at most 1,000 vB, replacement of the child). Only acceptance,
  relay and mining of one package were tested on Elements.
- **The 0-value anchor.** Refused in the test, reason unknown. Not needed by
  this design.
- **Compatibility.** The commitment format changes, so existing asset
  channels cannot be converted. They have to be closed with the old binary
  first. An open that still carries `fee_reserve` should be refused with a
  clear message.
- **External signers.** hsmd is simpler in this design, but a validating
  signer must accept a version-3 commitment with asset anchors.

## 10. Order of work

1. Add Elements 29 to the dev edition (built from source with two compile
   jobs; the Docker VM has 8 GB).
2. `submitpackage` in the backend interface, with a test on Bitcoin regtest.
3. Version-3 commitment with asset anchors and no reserve; unilateral close,
   HTLC and penalty tests from `test_assetchan.py`.
4. Mutual close with the closer's input.
5. Remove the reserve code and the two TLVs; refuse old-format opens.
6. Plugin: minimum wallet L-BTC, simpler `assetchan-open`.
