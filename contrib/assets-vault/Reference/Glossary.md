---
tags: [reference]
aliases: [Terms]
---
# Glossary

The terms the other notes use, with a pointer to where each is defined or implemented.

| Term | Meaning | Where |
|---|---|---|
| **Asset channel** | A Liquid channel whose balance is an issued asset rather than L-BTC. Each channel has exactly one asset. | [[Liquid Asset Channels]] |
| **Asset tag** | The 33-byte explicit Elements asset field: `0x01` followed by the asset id, byte-reversed from how `elements-cli` prints it. | `common/channel_asset.h` |
| **Fee asset / L-BTC** | Liquid's policy asset. It is the only asset that can pay transaction fees. | `bitcoin/chainparams.c` (`fee_asset_tag`) |
| **Fee reserve** | L-BTC that the opener puts in the 2-of-2 at funding output `n+1`. Every commitment and mutual close spends it as input 1, and it pays both anchors plus the fee. | [[Asset Transactions]] |
| **Funding sibling input** | Input 1 of a commitment or close tx, which spends the fee reserve. hsmd signs it with `hsmd_sign_funding_sibling_input`. | [[Core - hsmd]] |
| **Explicit output** | An Elements output whose asset and value are not blinded. Every output of an asset channel is explicit. | `elements_output_is_explicit` |
| **Anchors** | Two 330-sat L-BTC outputs (BOLT 3 `option_anchors_zero_fee_htlc_tx`). Asset channels require anchors, but core never builds anchor spends on Elements. | [[Asset Transactions]] |
| **HTLC cap** | The most HTLC outputs a commitment can carry while the fixed fee still meets the 0.1 sat/vB relay floor. Each side accepts at most half. | `asset_commit_max_htlcs` |
| **Asset sweep** | A spend of an asset output of ours after a unilateral close. Core funds it with L-BTC from CLN's own wallet and signs it `SIGHASH_ALL`. | [[Core - lightningd]] |
| **Stub channel** | A channel recreated from a static channel backup (SCB) so that the peer force-closes it and we sweep our side. | [[Core - lightningd]] |
| **Core forward rule** | lightningd refuses any forward that would leave in a different asset than it arrived in, whatever a plugin says. | `lightningd/peer_htlcs.c` |
| **assetchan** | The Liquid asset-channel plugin. It must run as `important-plugin`. | [[assetchan Plugin]] |
| **Virtual chain** | The Bitcoin-format chain that the EVM contract emulates and evmbackend serves to CLN as blocks. | [[EVM Channels]] |
| **UtxoChain** | The Solidity contract holding one coin (ETH, or one ERC-20) as a UTXO set. It validates the transactions CLN produces. | [[UtxoChain Contract]] |
| **INTERVAL** | Seconds per virtual block. Heights follow the wall clock. | [[EVM Channels]] |
| **evmbackend** | The chain-backend plugin that replaces bcli on EVM nodes. | [[evmbackend Plugin]] |
| **Peer identity** | The `(chain id, contract, token, units per sat)` that EVM nodes exchange over custom message `0xFFE9`, so they refuse peers on other contracts. | [[evmbackend Plugin]] |
| **Leg 1 / leg 2** | In a swap: leg 1 is the payer's payment to the provider, in the payer's asset; leg 2 is the provider's payment to the payee, in the payee's asset. Both use the same payment hash. | [[Cross-Asset Swaps]] |
| **Provider** | The node, or pair of nodes, that swaps leg 1 into leg 2. It must run assetswap as `important-plugin`. | [[assetswap Plugin]] |
| **Quote record** | The swap state in the datastore: `claimed`, then `paying`, then `complete` or `failed`. | [[assetswap Plugin]] |
| **important-plugin** | A CLN option: if such a plugin dies, lightningd exits. | [[Operations]] |

## See also
- [[Index]]
- [[Safety Invariants]]
