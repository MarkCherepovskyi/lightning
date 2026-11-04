#ifndef LIGHTNING_COMMON_CHANNEL_ASSET_H
#define LIGHTNING_COMMON_CHANNEL_ASSET_H
#include "config.h"
#include <bitcoin/pubkey.h>
#include <bitcoin/signature.h>
#include <bitcoin/tx.h>

/* Asset channels: channels whose balance is in an issued asset of a
 * multi-asset chain (Liquid), rather than the chain's own coin.
 *
 * The chain's coin (L-BTC) is the only thing that can pay on-chain fees, so
 * an asset channel's funding transaction pays the asset to the 2-of-2 at
 * output n, and a "fee reserve" of L-BTC (put up by the opener) to the same
 * 2-of-2 at output n+1.  Every commitment and closing transaction spends
 * both: the asset is the channel balance, the reserve pays the anchors and
 * the fee.  Nothing is ever taken from the asset balances for fees, so
 * `update_fee` changes nothing, and HTLC transactions are the zero-fee
 * `option_anchors` ones (the broadcaster adds L-BTC inputs).
 *
 * One asset per channel: an asset channel's HTLCs, and the invoices paid
 * through it, are in its asset.  Channels without an asset are ordinary
 * channels.
 *
 * Assets are 33-byte explicit asset tags (0x01 followed by the asset id in
 * internal byte order).  In messages to subdaemons they are a byte array
 * which is empty for ordinary channels. */

/* Subdaemons each serve one channel: they set its asset (NULL for an
 * ordinary channel) once they know it, and the transaction builders use it. */
void channel_asset_set(const u8 *asset, struct amount_sat fee_reserve);

/* The 33-byte asset tag of this subdaemon's channel, or NULL. */
const u8 *channel_asset(void);

/* The chain's coin locked in the fee reserve output (asset channels only). */
struct amount_sat channel_fee_reserve(void);

/* Same asset (NULL meaning the chain's own coin)? */
bool channel_asset_eq(const u8 *a, const u8 *b);

/* Byte array from a wire message (empty means none) to an asset or NULL. */
const u8 *channel_asset_or_null(const u8 *arr);

/* tal copy of @asset for wire messages (NULL stays NULL, i.e. empty). */
u8 *channel_asset_dup(const tal_t *ctx, const u8 *asset);

/* Asset tag from an asset id as printed by elements-cli (byte-reversed hex),
 * or NULL if it's not one. */
u8 *channel_asset_from_hex(const tal_t *ctx, const char *hex, size_t hexlen);

/* Asset id as printed by elements-cli. */
char *fmt_channel_asset(const tal_t *ctx, const u8 *asset);

/* Is @asset one of the (tal array of) @assets? */
bool channel_asset_in(const u8 *asset, u8 *const *assets);

/* Are this output's asset and value both explicit (always, off Elements)?
 * Only those can fund a channel. */
bool elements_output_is_explicit(const struct wally_tx_output *output);

/* The fee reserve always directly follows the funding output. */
struct bitcoin_outpoint fee_reserve_outpoint(const struct bitcoin_outpoint *funding);

/* Add the fee reserve input (same 2-of-2 script as the funding output). */
void add_fee_reserve_input(struct bitcoin_tx *tx,
			   const struct bitcoin_outpoint *funding,
			   u32 sequence,
			   const u8 *funding_wscript);

/* Asset channel transactions spend the fee reserve as input 1, with the
 * same 2-of-2 as the funding input.  Check the peer's signature @sig (NULL
 * if they didn't send one) for it, and keep it in the PSBT as we do theirs
 * for input 0: that's how lightningd completes input 1 when it broadcasts. */
bool check_fee_reserve_sig(struct bitcoin_tx *tx,
			   const u8 *funding_wscript,
			   const struct pubkey *their_funding_pubkey,
			   const secp256k1_ecdsa_signature *sig);

/* Finish an asset channel transaction's outputs (before they get sorted):
 * the asset can't pay fees, so the asset outputs must add up to the asset
 * funding amount.  Whatever isn't paid out (trimmed HTLCs, msat rounding)
 * tops up the opener's direct output at @opener_outnum, or the other side's
 * at @other_outnum if the opener has none (-1).  The L-BTC that the outputs
 * don't use (the reserve minus the anchors) is the fee. */
void channel_asset_finish_tx(struct bitcoin_tx *tx,
			     struct amount_sat funding_sats,
			     int opener_outnum,
			     int other_outnum);

#endif /* LIGHTNING_COMMON_CHANNEL_ASSET_H */
