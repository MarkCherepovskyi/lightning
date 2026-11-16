#ifndef LIGHTNING_COMMON_CHANNEL_ASSET_H
#define LIGHTNING_COMMON_CHANNEL_ASSET_H
#include "config.h"
#include <bitcoin/pubkey.h>
#include <bitcoin/signature.h>
#include <bitcoin/tx.h>
#include <ccan/mem/mem.h>

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

struct channel_asset {
	/* The 33-byte explicit asset tag. */
	u8 tag[ELEMENTS_ASSET_LEN];
	/* The chain's coin locked next to the funding output to pay fees. */
	struct amount_sat fee_reserve;
};

struct channel_asset *channel_asset_new(const tal_t *ctx,
					const u8 *tag,
					struct amount_sat fee_reserve);

/* The asset tag, or NULL for an ordinary channel: what bitcoin/tx.h's
 * *_asset_* functions take. */
static inline const u8 *channel_asset_tag(const struct channel_asset *asset)
{
	return asset ? asset->tag : NULL;
}

/* Can a channel be in this asset: an explicit tag (0x01 prefix), not the
 * chain's fee asset (that's an ordinary channel)? */
bool channel_asset_tag_valid(const u8 *tag, size_t len);

/* Same asset (NULL meaning the chain's own coin)? */
bool channel_asset_eq(const struct channel_asset *a,
		      const struct channel_asset *b);

/* Tags are fixed-size byte arrays. */
static inline u8 *channel_asset_tag_dup(const tal_t *ctx, const u8 *tag)
{
	return tal_dup_arr(ctx, u8, tag, ELEMENTS_ASSET_LEN, 0);
}

static inline bool channel_asset_tag_eq(const u8 *a, const u8 *b)
{
	return memeq(a, ELEMENTS_ASSET_LEN, b, ELEMENTS_ASSET_LEN);
}

/* For wire messages (`?channel_asset` fields). */
void towire_channel_asset(u8 **pptr, const struct channel_asset *asset);
void fromwire_channel_asset(const u8 **cursor, size_t *max,
			    struct channel_asset *asset);

/* Asset tag from an asset id as printed by elements-cli (byte-reversed hex),
 * or NULL if it's not one. */
u8 *channel_asset_from_hex(const tal_t *ctx, const char *hex, size_t hexlen);

/* Asset id as printed by elements-cli. */
char *fmt_channel_asset(const tal_t *ctx, const u8 *asset);

/* Are this output's asset and value both explicit (always, off Elements)?
 * Only those can fund a channel. */
bool elements_output_is_explicit(const struct wally_tx_output *output);

/* The fee reserve always directly follows the funding output. */
struct bitcoin_outpoint fee_reserve_outpoint(const struct bitcoin_outpoint *funding);

/* Add the fee reserve input (same 2-of-2 script as the funding output). */
void add_fee_reserve_input(struct bitcoin_tx *tx,
			   const struct bitcoin_outpoint *funding,
			   u32 sequence,
			   const u8 *funding_wscript,
			   const struct channel_asset *asset);

/* Asset channel transactions spend the fee reserve as input 1, with the
 * same 2-of-2 as the funding input.  Check the peer's signature @sig (NULL
 * if they didn't send one) for it, and keep it in the PSBT as we do theirs
 * for input 0: that's how lightningd completes input 1 when it broadcasts. */
bool check_fee_reserve_sig(struct bitcoin_tx *tx,
			   const u8 *funding_wscript,
			   const struct pubkey *their_funding_pubkey,
			   const secp256k1_ecdsa_signature *sig);

/* A commitment's trimmed HTLCs, @burned in the asset, go to an OP_RETURN
 * output (nobody gets them), as trimmed HTLCs go to the miners on Bitcoin:
 * so no side profits from making the other's HTLCs dust.  Only when some
 * HTLC is trimmed, so the output takes the place of at least one HTLC
 * output and never adds to a commitment's worst-case weight (the HTLC cap,
 * asset_commit_max_htlcs).  Returns its output number, or -1 for none. */
int channel_asset_add_burn(struct bitcoin_tx *tx,
			   const struct channel_asset *asset,
			   struct amount_sat burned);

/* Is this an asset channel's burn output (an OP_RETURN, which no one can
 * spend)? */
bool channel_asset_is_burn(const struct wally_tx_output *out);

/* Finish an asset channel transaction's outputs (before they get sorted):
 * the asset can't pay fees, so the asset outputs must add up to the asset
 * funding amount.  Whatever isn't paid out (msat rounding; trimmed HTLCs
 * are burned first, channel_asset_add_burn) tops up the opener's direct
 * output at @opener_outnum, or the other side's at @other_outnum if the
 * opener has none (-1).  The L-BTC that the outputs don't use (the reserve
 * minus the anchors) is the fee. */
void channel_asset_finish_tx(struct bitcoin_tx *tx,
			     const struct channel_asset *asset,
			     struct amount_sat funding_sats,
			     int opener_outnum,
			     int other_outnum);

#endif /* LIGHTNING_COMMON_CHANNEL_ASSET_H */
