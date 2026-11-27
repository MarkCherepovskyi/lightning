#ifndef LIGHTNING_LIGHTNINGD_ONCHAIN_CONTROL_H
#define LIGHTNING_LIGHTNINGD_ONCHAIN_CONTROL_H
#include "config.h"
#include <lightningd/lightningd.h>

struct channel;
struct bitcoin_tx;
struct block;

enum watch_result onchaind_funding_spent(struct channel *channel,
					 const struct bitcoin_tx *tx,
					 u32 blockheight);

void onchaind_replay_channels(struct lightningd *ld);

/* Asset channels: the most L-BTC we pay in fees for one transaction of
 * ours (an asset sweep, the fee inputs of an HTLC tx, an anchor spend).
 * One fee reserve normally; more for a @critical one (the peer can take
 * the output if ours confirms after @deadline) close to @deadline. */
struct amount_sat asset_fee_cap(const struct channel *channel,
				u32 deadline, bool critical);

#endif /* LIGHTNING_LIGHTNINGD_ONCHAIN_CONTROL_H */
