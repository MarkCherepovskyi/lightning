#include "config.h"
#include <assert.h>
#include <bitcoin/chainparams.h>
#include <ccan/build_assert/build_assert.h>
#include <ccan/str/hex/hex.h>
#include <common/liquid_asset.h>
#include <common/utils.h>
#include <stdlib.h>
#include <string.h>

static bool asset_mode;
static u8 asset_tag[ELEMENTS_ASSET_LEN];
static struct amount_sat fee_reserve;

void liquid_set_channel_asset(const u8 *asset, struct amount_sat reserve)
{
	asset_mode = (asset != NULL);
	if (asset_mode)
		memcpy(asset_tag, asset, sizeof(asset_tag));
	fee_reserve = reserve;
}

const u8 *liquid_channel_asset(void)
{
	return asset_mode ? asset_tag : NULL;
}

struct amount_sat liquid_fee_reserve(void)
{
	assert(asset_mode);
	return fee_reserve;
}

bool liquid_asset_eq(const u8 *a, const u8 *b)
{
	if (!a || !b)
		return a == b;
	return memcmp(a, b, ELEMENTS_ASSET_LEN) == 0;
}

const u8 *liquid_asset_or_null(const u8 *arr)
{
	if (!arr || tal_bytelen(arr) != ELEMENTS_ASSET_LEN)
		return NULL;
	return arr;
}

u8 *liquid_asset_dup(const tal_t *ctx, const u8 *asset)
{
	if (!asset)
		return NULL;
	return tal_dup_arr(ctx, u8, asset, ELEMENTS_ASSET_LEN, 0);
}

u8 *liquid_asset_from_hex(const tal_t *ctx, const char *hex, size_t hexlen)
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

void liquid_asset_network(const u8 *asset, struct bitcoin_blkid *network)
{
	BUILD_ASSERT(sizeof(*network) == ELEMENTS_ASSET_LEN - 1);
	memcpy(network, asset + 1, sizeof(*network));
}

bool liquid_output_is_explicit(const struct wally_tx_output *output)
{
	if (!chainparams->is_elements)
		return true;
	return output->asset_len == ELEMENTS_ASSET_LEN && output->asset[0] == 0x01
		&& output->value_len == 9 && output->value[0] == 0x01;
}

bool liquid_asset_in(const u8 *asset, u8 *const *assets)
{
	for (size_t i = 0; i < tal_count(assets); i++)
		if (liquid_asset_eq(asset, assets[i]))
			return true;
	return false;
}

char *fmt_liquid_asset(const tal_t *ctx, const u8 *asset)
{
	u8 id[ELEMENTS_ASSET_LEN - 1];

	for (size_t i = 0; i < sizeof(id); i++)
		id[i] = asset[ELEMENTS_ASSET_LEN - 1 - i];
	return tal_hexstr(ctx, id, sizeof(id));
}

struct bitcoin_outpoint liquid_fee_reserve_outpoint(const struct bitcoin_outpoint *funding)
{
	struct bitcoin_outpoint reserve = *funding;
	reserve.n++;
	return reserve;
}

void liquid_add_fee_reserve_input(struct bitcoin_tx *tx,
				  const struct bitcoin_outpoint *funding,
				  u32 sequence,
				  const u8 *funding_wscript)
{
	struct bitcoin_outpoint reserve = liquid_fee_reserve_outpoint(funding);

	bitcoin_tx_add_input(tx, &reserve, sequence, NULL,
			     liquid_fee_reserve(), NULL, funding_wscript);
}

int liquid_finish_channel_tx(struct bitcoin_tx *tx,
			     struct amount_sat funding_sats,
			     int opener_outnum,
			     struct amount_sat fee)
{
	const u8 *asset = liquid_channel_asset();
	struct amount_sat paid = AMOUNT_SAT(0), leftover, change;
	struct amount_asset amt;
	u8 *script;

	assert(asset);
	for (size_t i = 0; i < tx->wtx->num_outputs; i++) {
		amt = bitcoin_tx_output_get_amount(tx, i);
		if (memcmp(amt.asset, asset, sizeof(amt.asset)) != 0)
			continue;
		if (!amount_sat_add(&paid, paid, amount_sat(amt.value)))
			abort();
	}

	/* No opener output (it's below dust): nowhere to send leftovers. */
	if (opener_outnum < 0)
		return -1;

	/* The asset can't go to miners like sats do, so whatever isn't paid
	 * out (trimmed HTLCs, rounding) goes to the opener. */
	if (amount_sat_sub(&leftover, funding_sats, paid)
	    && !amount_sat_is_zero(leftover)) {
		struct amount_sat topped_up;
		amt = bitcoin_tx_output_get_amount(tx, opener_outnum);
		if (!amount_sat_add(&topped_up, amount_sat(amt.value), leftover))
			abort();
		bitcoin_tx_output_set_amount(tx, opener_outnum, topped_up);
	}

	if (!amount_sat_sub(&change, liquid_fee_reserve(), fee)
	    || amount_sat_is_zero(change))
		return -1;

	script = tal_dup_arr(tmpctx, u8,
			     tx->wtx->outputs[opener_outnum].script,
			     tx->wtx->outputs[opener_outnum].script_len, 0);
	return bitcoin_tx_add_output(tx, script, NULL, change);
}
