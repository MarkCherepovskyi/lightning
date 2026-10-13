#ifndef LIGHTNING_COMMON_LIQUID_ASSET_H
#define LIGHTNING_COMMON_LIQUID_ASSET_H
#include "config.h"
#include <bitcoin/tx.h>

/* PROOF OF CONCEPT: Liquid issued-asset channels.
 *
 * If CLN_DEV_LIQUID_ASSET (the asset id as printed by elements-cli) is set in
 * the environment on an Elements network, *every* channel is an asset
 * channel.  The funding transaction pays the asset to the 2-of-2 at output n,
 * and CLN_DEV_LIQUID_FEE_RESERVE_SAT of the policy asset (L-BTC) to the same
 * 2-of-2 at output n+1.  Commitment and closing transactions spend both, and
 * on-chain fees come out of that L-BTC "fee reserve" instead of the opener's
 * balance.  Subdaemons inherit the environment from lightningd. */

/* The 33-byte explicit asset tag of the channel asset, or NULL if we're not
 * running asset channels. */
const u8 *liquid_channel_asset(void);

/* L-BTC locked in the fee reserve output (only meaningful in asset mode). */
struct amount_sat liquid_fee_reserve(void);

/* The fee reserve always directly follows the funding output. */
struct bitcoin_outpoint liquid_fee_reserve_outpoint(const struct bitcoin_outpoint *funding);

/* Add the fee reserve input (same 2-of-2 script as the funding output). */
void liquid_add_fee_reserve_input(struct bitcoin_tx *tx,
				  const struct bitcoin_outpoint *funding,
				  u32 sequence,
				  const u8 *funding_wscript);

/* Finish an asset channel transaction before it gets sorted:
 *  - asset outputs must add up to the asset funding amount, so whatever
 *    isn't paid out (trimmed HTLCs, rounding) tops up the opener's output;
 *  - the L-BTC left in the reserve after `fee` goes back to the opener, to
 *    the same script as the opener's asset output.
 * Returns the index of the L-BTC change output, or -1 if there is none. */
int liquid_finish_channel_tx(struct bitcoin_tx *tx,
			     struct amount_sat funding_sats,
			     int opener_outnum,
			     struct amount_sat fee);

#endif /* LIGHTNING_COMMON_LIQUID_ASSET_H */
