#ifndef LIGHTNING_COMMON_LIQUID_ASSET_H
#define LIGHTNING_COMMON_LIQUID_ASSET_H
#include "config.h"
#include <bitcoin/block.h>
#include <bitcoin/tx.h>

/* PROOF OF CONCEPT: Liquid issued-asset channels.
 *
 * An asset channel's funding transaction pays the asset to the 2-of-2 at
 * output n, and an L-BTC "fee reserve" (put up by the opener) to the same
 * 2-of-2 at output n+1.  Commitment and closing transactions spend both: the
 * asset is the channel balance, and on-chain fees come out of the fee reserve
 * instead of the opener's balance; the rest of the reserve returns to the
 * opener.  Channels without an asset are ordinary L-BTC channels.
 *
 * Assets are 33-byte explicit asset tags (0x01 followed by the asset id in
 * internal byte order).  On the wire to subdaemons they are a byte array
 * which is empty for ordinary channels. */

/* Subdaemons each serve one channel: they set its asset (NULL for an
 * ordinary channel) once they know it, and the transaction builders use it. */
void liquid_set_channel_asset(const u8 *asset, struct amount_sat fee_reserve);

/* The 33-byte asset tag of this subdaemon's channel, or NULL. */
const u8 *liquid_channel_asset(void);

/* L-BTC locked in the fee reserve output (only valid for asset channels). */
struct amount_sat liquid_fee_reserve(void);

/* Same asset (NULL meaning L-BTC)? */
bool liquid_asset_eq(const u8 *a, const u8 *b);

/* Byte array from a wire message (empty means none) to an asset or NULL. */
const u8 *liquid_asset_or_null(const u8 *arr);

/* tal copy of @asset for wire messages (NULL stays NULL, i.e. empty). */
u8 *liquid_asset_dup(const tal_t *ctx, const u8 *asset);

/* Asset tag from an asset id as printed by elements-cli (byte-reversed hex),
 * or NULL if it's not one. */
u8 *liquid_asset_from_hex(const tal_t *ctx, const char *hex, size_t hexlen);

/* Asset id as printed by elements-cli. */
char *fmt_liquid_asset(const tal_t *ctx, const u8 *asset);

/* Announced asset channels are gossiped on their own network: the asset id
 * goes where gossip messages carry the chain_hash (so it prints as the asset
 * id).  Nodes that don't follow the asset ignore it as an unknown chain. */
void liquid_asset_network(const u8 *asset, struct bitcoin_blkid *network);

/* Is @asset one of the (tal array of) @assets? */
bool liquid_asset_in(const u8 *asset, u8 *const *assets);

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
