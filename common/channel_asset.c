#include "config.h"
#include <assert.h>
#include <bitcoin/chainparams.h>
#include <bitcoin/psbt.h>
#include <bitcoin/signature.h>
#include <ccan/str/hex/hex.h>
#include <common/channel_asset.h>
#include <common/utils.h>
#include <wire/wire.h>
#include <stdlib.h>
#include <string.h>

struct channel_asset *channel_asset_new(const tal_t *ctx,
					const u8 *tag,
					struct amount_sat fee_reserve)
{
	struct channel_asset *asset = tal(ctx, struct channel_asset);

	memcpy(asset->tag, tag, sizeof(asset->tag));
	asset->fee_reserve = fee_reserve;
	return asset;
}

bool channel_asset_eq(const struct channel_asset *a,
		      const struct channel_asset *b)
{
	if (!a || !b)
		return a == b;
	return memcmp(a->tag, b->tag, sizeof(a->tag)) == 0;
}

void towire_channel_asset(u8 **pptr, const struct channel_asset *asset)
{
	towire_u8_array(pptr, asset->tag, sizeof(asset->tag));
	towire_amount_sat(pptr, asset->fee_reserve);
}

void fromwire_channel_asset(const u8 **cursor, size_t *max,
			    struct channel_asset *asset)
{
	fromwire_u8_array(cursor, max, asset->tag, sizeof(asset->tag));
	asset->fee_reserve = fromwire_amount_sat(cursor, max);
}

u8 *channel_asset_from_hex(const tal_t *ctx, const char *hex, size_t hexlen)
{
	u8 *asset = tal_arr(ctx, u8, ELEMENTS_ASSET_LEN);

	/* elements-cli prints asset ids byte-reversed, like txids. */
	if (!hex_decode(hex, hexlen, asset + 1, ELEMENTS_ASSET_LEN - 1))
		return tal_free(asset);
	asset[0] = 0x01;
	reverse_bytes(asset + 1, ELEMENTS_ASSET_LEN - 1);
	return asset;
}

char *fmt_channel_asset(const tal_t *ctx, const u8 *asset)
{
	u8 id[ELEMENTS_ASSET_LEN - 1];

	memcpy(id, asset + 1, sizeof(id));
	reverse_bytes(id, sizeof(id));
	return tal_hexstr(ctx, id, sizeof(id));
}

bool elements_output_is_explicit(const struct wally_tx_output *output)
{
	if (!chainparams->is_elements)
		return true;
	return output->asset_len == ELEMENTS_ASSET_LEN && output->asset[0] == 0x01
		&& output->value_len == 9 && output->value[0] == 0x01;
}

struct bitcoin_outpoint fee_reserve_outpoint(const struct bitcoin_outpoint *funding)
{
	struct bitcoin_outpoint reserve = *funding;
	reserve.n++;
	return reserve;
}

void add_fee_reserve_input(struct bitcoin_tx *tx,
			   const struct bitcoin_outpoint *funding,
			   u32 sequence,
			   const u8 *funding_wscript,
			   const struct channel_asset *asset)
{
	struct bitcoin_outpoint reserve = fee_reserve_outpoint(funding);

	bitcoin_tx_add_input(tx, &reserve, sequence, NULL,
			     asset->fee_reserve, NULL, funding_wscript);
}

bool check_fee_reserve_sig(struct bitcoin_tx *tx,
			   const u8 *funding_wscript,
			   const struct pubkey *their_funding_pubkey,
			   const secp256k1_ecdsa_signature *sig)
{
	struct bitcoin_signature bsig;

	if (!sig || tx->wtx->num_inputs != 2)
		return false;
	bsig.s = *sig;
	bsig.sighash_type = SIGHASH_ALL;
	if (!check_tx_sig(tx, 1, NULL, funding_wscript,
			  their_funding_pubkey, &bsig))
		return false;
	return psbt_input_set_signature(tx->psbt, 1, their_funding_pubkey,
					&bsig);
}

void channel_asset_finish_tx(struct bitcoin_tx *tx,
			     const struct channel_asset *asset,
			     struct amount_sat funding_sats,
			     int opener_outnum,
			     int other_outnum)
{
	struct amount_sat paid = AMOUNT_SAT(0), leftover, topped_up;
	struct amount_asset amt;
	int outnum;

	for (size_t i = 0; i < tx->wtx->num_outputs; i++) {
		amt = bitcoin_tx_output_get_amount(tx, i);
		if (memcmp(amt.asset, asset->tag, sizeof(amt.asset)) != 0)
			continue;
		if (!amount_sat_add(&paid, paid, amount_sat(amt.value)))
			abort();
	}
	if (!amount_sat_sub(&leftover, funding_sats, paid))
		abort();
	if (amount_sat_is_zero(leftover))
		return;

	/* One direct output is always there: each side keeps its channel
	 * reserve (at least the dust limit) once it has reached it, the
	 * opener's balance is not cut for anchors (the fee reserve pays
	 * them), and at open the two balances add up to the funding. */
	outnum = opener_outnum >= 0 ? opener_outnum : other_outnum;
	assert(outnum >= 0);
	amt = bitcoin_tx_output_get_amount(tx, outnum);
	if (!amount_sat_add(&topped_up, amount_sat(amt.value), leftover))
		abort();
	bitcoin_tx_output_set_amount(tx, outnum, topped_up);
}
