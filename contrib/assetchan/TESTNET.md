# Asset channels on the real Liquid testnet

The cases of `tests/test_assetchan.py`, run by hand on Liquid testnet
(`liquidtestnet`, a block a minute, nobody can mine) with two nodes built from
this branch and the `contrib/assetchan` plugin. Explorer:
https://blockstream.info/liquidtestnet/tx/TXID

## Setup

- `elementsd` on `liquidtestnet` (synced, `-txindex=1`), its RPC on
  `127.0.0.1:7039`, wallet `poc`. Here it is the `liquid-ln-testnet` docker
  stack's elementsd, reached through a socat proxy container (see
  `nodes.sh`).
- Two lightningd nodes, A and B, started with `contrib/assetchan/nodes.sh
  start A|B`: this tree's build, `--network=liquid-testnet`, the plugin,
  `--assetchan-asset` = our asset, `--watchtime-blocks=6`,
  `--funding-confirms=2`, `--developer`; they talk over localhost. bcli runs
  `elements-cli` through a wrapper (`docker exec`), since there is none on the
  host.
- L-BTC: 100,000 sat from https://liquidtestnet.com/faucet to an
  unconfidential address of the `poc` wallet
  ([f3ab7791](https://blockstream.info/liquidtestnet/tx/f3ab7791ed6db369753674f3b36a5b98223173dcd6399d0ee42dcba26f7965d8)).
- Our asset: `issueasset 1000 0 false` →
  `ddc8da3f868bd193d25e3bbdd3e3c075d6629fa45dee1c3d829682d82094ba50`
  ([1be12f9a](https://blockstream.info/liquidtestnet/tx/1be12f9a894b143731b50f1d88157022817ebab253d09d241676d24f9476744c)).
  On testnet elementsd blinded both issuance outputs anyway; the plugin's
  funding flow pays itself an explicit output first when it has none
  (`explicit_utxo`), which is what happened at every open below.
- 20,000 sat of L-BTC to each node's own wallet, for the fees of their
  sweeps ([8177fce6](https://blockstream.info/liquidtestnet/tx/8177fce64dcc66bb5e7b451d36781ac04e540d3164694c09998726a5efaaa9bc),
  [5d804c45](https://blockstream.info/liquidtestnet/tx/5d804c45069caaa683e4748dc484010031536a4787af6f4a5ebb83aca3d583aa)).
- L-BTC on testnet is `144c654344aa716d6f3abcc1ca90e5641e4e2a7f633bc09fe3baf64585819a49`.

## Cases

| ID | Case | How | Result | Evidence |
| --- | --- | --- | --- | --- |
| T1 | Open an asset channel | `assetchan-open` A→B, 1,000,000 units, 2,000 sat reserve | PASS: `CHANNELD_NORMAL` on both after 2 confirmations; type `static_remotekey`+`anchors`+`scid_alias` | funding [a43d5711](https://blockstream.info/liquidtestnet/tx/a43d57113597e25b773c94de882c6d857bb8edb589689abd598b491f57db544d) (block 2,690,367): vout 0 = 1,000,000 units to the 2-of-2, vout 1 = 2,000 sat L-BTC to the same script, fee 1,000 sat; the plugin's explicit self-payments first ([f41a009c](https://blockstream.info/liquidtestnet/tx/f41a009c), [1b491eac](https://blockstream.info/liquidtestnet/tx/1b491eac)) |
| T2 | Payments both ways | `assetchan-invoice` + `assetchan-pay`: A→B 100,000 units, then B→A 30,000 | PASS: balances 930,000 / 70,000 | off-chain |
| T3 | Cooperative close | `assetchan-close` by A | PASS: `closing_signed` at once, fee = the whole reserve | close [73c70725](https://blockstream.info/liquidtestnet/tx/73c70725844f2a9e0cbb35b2f23977b45ee0cfb5facccda972208e5eefab5398): (block 2,690,370): inputs = both funding outputs (2-of-2 witnesses), 70,000 units to B's elementsd address, 930,000 to A's, fee 2,000 sat L-BTC; both outputs explicit and spendable in elementsd's wallet |
| T4 | Force close, no HTLC, opener closes | second channel [f0d00220](https://blockstream.info/liquidtestnet/tx/f0d002204fb73c4e6b13ceacb6b9d2b90799626fe4964c544a3caad0af74016f), A→B 100,000 units paid, B stopped, `close` on A with `unilateraltimeout=1` | PASS: the commitment spends both funding outputs and pays its fee from the reserve | commitment [b54c90fb](https://blockstream.info/liquidtestnet/tx/b54c90fbf10a34bba62066352686a21c6203619fe06274764f89e25cd92f9d09): inputs = funding vout 0 and 1 (2-of-2 witnesses), two 330 sat anchors, 100,000 units to_remote, 900,000 units to_local, fee 1,340 sat = reserve − anchors |
| T4 (cont.) | The opener sweeps its `to_local` | A, 6 blocks after the commitment (`--watchtime-blocks=6` on B) | PASS: swept at the first chance; it waited one extra block for L-BTC, see the notes | sweep [c028ff80](https://blockstream.info/liquidtestnet/tx/c028ff8018041e9a39333ec59a65367794d068f0c64c8f4e0d006280d3ee6079) (block 2,690,379): input 0 = the to_local (sequence 6, `<sig> <> <wscript>`), input 1 = A's wallet change from its other sweep; 900,000 units to A's elementsd address, 19,180 sat change, fee 415 sat |
| T5 | The peer sweeps its `to_remote` | B restarted after the commitment confirmed | PASS: swept one block later, fee from B's own L-BTC | sweep [d17ad268](https://blockstream.info/liquidtestnet/tx/d17ad2680d18913ba9e6a9592929406913a9b093bfbf2a4315d3861566906700): input 0 = the to_remote (sequence 1, `<sig> <wscript>`), input 1 = B's 20,000 sat wallet coin; 100,000 units to B's elementsd close address, 19,595 sat change back to B's wallet, fee 405 sat |
| T4b | Force close by the non-opener, opener offline | third channel [fb4880ee](https://blockstream.info/liquidtestnet/tx/fb4880ee4e66677e1520a5d3e70833cac6788a29c4144eacfafdcc8201e910fd), nothing paid, A stopped, `close` on B with `unilateraltimeout=1` (meant to be the HTLC case: the HTLC never left A, see the notes) | PASS: B's commitment has A's to_remote, one anchor (B has no output of its own) and the fee; A sweeps its to_remote on restart | commitment [362a2039](https://blockstream.info/liquidtestnet/tx/362a203920a3d0ba3f3cd80d97ba7a4f02ecca815fd8953500bfaebea8ee2d0c): 330 sat anchor, 1,000,000 units to_remote, fee 1,670 sat; A's sweep [1809044d](https://blockstream.info/liquidtestnet/tx/1809044d99f1c9ca4513f30ace8d1fe14acedfc5d5590ba86af163dbdec60d8a): to_remote (sequence 1) + A's wallet coin, 1,000,000 units to A's elementsd address, 19,595 sat change, fee 405 sat |
| T8 | HTLC in flight, the payee force-closes with the preimage | fourth channel [3d037dc7](https://blockstream.info/liquidtestnet/tx/3d037dc7f3bc3b85f3350e92d8f7a1038e877e5aef9d690072a36544f5d88731), A→B 100,000 units twice first (balance 800,000 / 200,000), then a third 100,000 payment with B's `--dev-disconnect` holding `update_fulfill_htlc`: B hangs up before sending the fulfill, A is stopped at once, `close` on B with `unilateraltimeout=1` | PASS: B's commitment carries the HTLC; B claims it with a zero-fee HTLC-success tx paid for by its wallet L-BTC, then sweeps that output and its to_local 6 blocks later; A, restarted, learns the preimage from B's HTLC-success tx (`listsendpays`: `complete`) and sweeps its to_remote | commitment [d263b432](https://blockstream.info/liquidtestnet/tx/d263b432e3635fd61db0100170c89676feaf0a70ba94c16fac67d4c3aa39cb31) (block 2,690,388): two 330 sat anchors, 100,000 units HTLC, 200,000 to_local, 700,000 to_remote, fee 1,340 sat. HTLC-success [79c95c52](https://blockstream.info/liquidtestnet/tx/79c95c529b6bf4679fd11323572b8b5a59d64948d5fcd70c24ffe24a76cc30e9) (block 2,690,390): input 0 = the HTLC (sequence 1, witness `<> <sig A> <sig B> <preimage> <wscript>`), input 1 = B's wallet coin; 100,000 units to the delayed HTLC-success script, 18,712 sat change, fee 883 sat. B's delayed sweeps, both in block 2,690,396: to_local [74570bfc](https://blockstream.info/liquidtestnet/tx/74570bfc5ab05914ddfbdc3ff13e3a60e293904494e0cadde8d4eeffb3b51366) (sequence 6, `<sig> <> <wscript>`, 200,000 units to B's elementsd address, fee 415 sat) and the HTLC-success output [f5dfba37](https://blockstream.info/liquidtestnet/tx/f5dfba37550661705d9d911f0d439c527f1b652ee583d0b234497cf7c4e0a66b) (sequence 6, 100,000 units, fee 415 sat). A's to_remote sweep [cb542c09](https://blockstream.info/liquidtestnet/tx/cb542c09ab4a16cfc27bfd01c4f5d7909479896e611a0cc4563c6ab28ab25b21) (block 2,690,397; sequence 1, `<sig> <wscript>`, 700,000 units to A's elementsd address, fee 405 sat); A's payment completed with preimage `32c22518…` taken from the chain |
| T9 | Penalty: the opener broadcasts a revoked commitment with an HTLC in it | fifth channel [c1955d5e](https://blockstream.info/liquidtestnet/tx/c1955d5eb8a976b509166132b48b11e3b46f94decc7bf085f17d65a8675e933f), A→B 100,000 units paid first (900,000 / 100,000); both nodes restarted with `--dev-disable-commit-after=1`; A pays a 50,000 unit invoice of B's and, once both sides hold the commitment with that HTLC, `dev-sign-last-tx` on A keeps a signed copy; commits re-enabled, the payment completes (850,000 / 150,000); A stopped; the old commitment sent with `sendrawtransaction` | PASS: B's onchaind recognises the revoked commitment and takes all three asset outputs in the next block, the two of A's with the revocation key; the cheater gets nothing | revoked commitment [95b1d417](https://blockstream.info/liquidtestnet/tx/95b1d417441b601525e677afaaa98e149f7b9170674cf8383681bda90ef38921) (block 2,690,403): two 330 sat anchors, 50,000 units HTLC, 100,000 to_remote (B), 850,000 to_local (A), fee 1,340 sat. B's three sweeps, all in block 2,690,404, each paid for with one of B's 3,000 sat coins: HTLC [697c8b1c](https://blockstream.info/liquidtestnet/tx/697c8b1ce513d3265ca2929f00594cef7f98480b404b6a78e8df527acf72025d) (`<sig> <revocationpubkey> <wscript>`, 50,000 units, fee 439 sat), to_remote [a808fcb0](https://blockstream.info/liquidtestnet/tx/a808fcb02785b87a778498357b54eba4ee1fcdb996a51ece5f585fdbfc6b93f2) (sequence 1, `<sig> <wscript>`, 100,000 units, fee 405 sat), to_local [a897cc69](https://blockstream.info/liquidtestnet/tx/a897cc69a780858d324b14dc70124fc46dc1e141d8552d213c6e5f005139f9eb) (`<sig> 01 <wscript>`, 850,000 units, fee 415 sat); all 1,000,000 units at B's elementsd address; B's log: `Resolved THEIR_REVOKED_UNILATERAL/DELAYED_CHEAT_OUTPUT_TO_THEM by our proposal OUR_PENALTY_TX` |

All cases passed. At the end the `poc` wallet holds the full 1000.0 units of
the asset again: every unit that went into a channel came back to a close
address, either side's, by a cooperative close, a sweep, an HTLC-success tx
or a penalty. Nothing in core or the plugin had to change for testnet; the
only differences from regtest were the pace (a block a minute, so each
sweep's wait is real) and the environment hiccups listed below.

## Notes

- **Fees and wallet coins.** Every sweep is a zero-fee asset spend plus one
  of the node's own L-BTC coins. A node with a single coin serialises its
  sweeps, one per block (`only 0sat L-BTC for a fee ... Will retry` in the
  log, as A did in T4). Give a node several small coins: B got four 3,000 sat
  coins before T8 and its two delayed sweeps landed in the same block.
- **Reproducing T8.** `--dev-disconnect` lines are consumed one at a time and
  `*N` means "on the N-th such message", not "N times"; after the hang-up the
  peers reconnect within about a second and B re-sends the fulfill. So stop A
  the moment B's log shows `dev_disconnect: -WIRE_UPDATE_FULFILL_HTLC` (six
  such lines in the file left enough time). Two earlier attempts on this
  channel settled normally for those two reasons.
- **Stale own-commitment rebroadcasts.** A node that decided to force-close
  after its peer's commitment had already won (A in T4b and T8, on restart)
  keeps re-broadcasting its own commitment: `Expected error broadcasting tx
  ... bad-txns-inputs-missingorspent` in the log. Upstream behaviour,
  harmless.
- **Cosmetic logs.** `UNUSUAL lightningd: Unexpected output number 1 paying
  to ...` at each funding confirmation: lightningd's wallet does not know the
  reserve output. Bookkeeper's `topic 'utxo_deposit' is not a known
  notification topic` at start is upstream and unrelated.
- **Plugin.** `assetchan-open` takes the bare node id of an already connected
  peer. Its explicit self-payment failed once with elementsd's `Insufficient
  funds` right after another spend from the same wallet (unconfirmed change);
  the retry worked.
- **Environment.** The elementsd container restarted twice around 09:07 UTC
  (exit 0, nothing in its log, mempool not saved). Both lightningd nodes just
  waited for the RPC to come back and resumed; B's not-yet-final sweeps were
  re-broadcast at the next block, so the T8 to_local sweep confirmed two
  blocks after it became valid. elementsd came back without the `poc`
  wallet loaded (`loadwallet poc` fixed the plugin's `Requested wallet does
  not exist or is not loaded`).
- `lightning-cli close` prints `#` comment lines before its JSON;
  `lightning-cli` does not mix positional and keyword arguments.
