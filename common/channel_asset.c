#include "config.h"
#include <assert.h>
#include <bitcoin/chainparams.h>
#include <bitcoin/psbt.h>
#include <bitcoin/signature.h>
#include <ccan/str/hex/hex.h>
#include <common/channel_asset.h>
#include <common/utils.h>
#include <stdlib.h>
#include <string.h>

static bool asset_mode;
static u8 asset_tag[ELEMENTS_ASSET_LEN];
static struct amount_sat fee_reserve;

void channel_asset_set(const u8 *asset, struct amount_sat reserve)
{
	asset_mode = (asset != NULL);
	if (asset_mode)
		memcpy(asset_tag, asset, sizeof(asset_tag));
	fee_reserve = reserve;
}

const u8 *channel_asset(void)
{
	return asset_mode ? asset_tag : NULL;
}

struct amount_sat channel_fee_reserve(void)
{
	assert(asset_mode);
	return fee_reserve;
}

bool channel_asset_eq(const u8 *a, const u8 *b)
{
	if (!a || !b)
		return a == b;
	return memcmp(a, b, ELEMENTS_ASSET_LEN) == 0;
}

const u8 *channel_asset_or_null(const u8 *arr)
{
	if (!arr || tal_bytelen(arr) != ELEMENTS_ASSET_LEN)
		return NULL;
	return arr;
}

u8 *channel_asset_dup(const tal_t *ctx, const u8 *asset)
{
	if (!asset)
		return NULL;
	return tal_dup_arr(ctx, u8, asset, ELEMENTS_ASSET_LEN, 0);
}

u8 *channel_asset_from_hex(const tal_t *ctx, const char *hex, size_t hexlen)
{
	u8 id[ELEMENTS_ASSET_LEN - 1];
	u8 *asset;

	if (!hex_decode(hex, hexlen, id, sizeof(id)))
		return NULL;

	/* elements-cli prints asset ids byte-reversed, like txids. */
	asset = tal_arr(ctx, u8, ELEMENTS_ASSET_LEN);
	asset[0] = 0x01;
	for (size_t i = 0; i < sizeof(id); i++)
		asset[1 + i] = id[sizeof(id) - 1 - i];
	return asset;
}

char *fmt_channel_asset(const tal_t *ctx, const u8 *asset)
{
	u8 id[ELEMENTS_ASSET_LEN - 1];

	for (size_t i = 0; i < sizeof(id); i++)
		id[i] = asset[ELEMENTS_ASSET_LEN - 1 - i];
	return tal_hexstr(ctx, id, sizeof(id));
}

bool channel_asset_in(const u8 *asset, u8 *const *assets)
{
	for (size_t i = 0; i < tal_count(assets); i++)
		if (channel_asset_eq(asset, assets[i]))
			return true;
	return false;
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
			   const u8 *funding_wscript)
{
	struct bitcoin_outpoint reserve = fee_reserve_outpoint(funding);

	bitcoin_tx_add_input(tx, &reserve, sequence, NULL,
			     channel_fee_reserve(), NULL, funding_wscript);
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
			     struct amount_sat funding_sats,
			     int opener_outnum,
			     int other_outnum)
{
	const u8 *asset = channel_asset();
	struct amount_sat paid = AMOUNT_SAT(0), leftover, topped_up;
	struct amount_asset amt;
	int outnum;

	assert(asset);
	for (size_t i = 0; i < tx->wtx->num_outputs; i++) {
		amt = bitcoin_tx_output_get_amount(tx, i);
		if (memcmp(amt.asset, asset, sizeof(amt.asset)) != 0)
			continue;
		if (!amount_sat_add(&paid, paid, amount_sat(amt.value)))
			abort();
	}
	if (!amount_sat_sub(&leftover, funding_sats, paid))
		abort();
	if (amount_sat_is_zero(leftover))
		return;

	/* The opener's balance never drops below the channel reserve, which
	 * is above the dust limit, so its output is there; the other side's
	 * may be trimmed. */
	outnum = opener_outnum >= 0 ? opener_outnum : other_outnum;
	assert(outnum >= 0);
	amt = bitcoin_tx_output_get_amount(tx, outnum);
	if (!amount_sat_add(&topped_up, amount_sat(amt.value), leftover))
		abort();
	bitcoin_tx_output_set_amount(tx, outnum, topped_up);
}
