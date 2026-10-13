#include "config.h"
#include <assert.h>
#include <bitcoin/chainparams.h>
#include <ccan/str/hex/hex.h>
#include <common/liquid_asset.h>
#include <common/utils.h>
#include <stdlib.h>
#include <string.h>

static bool initialized;
static bool asset_mode;
static u8 asset_tag[ELEMENTS_ASSET_LEN];
static struct amount_sat fee_reserve;

static void liquid_asset_init(void)
{
	const char *hexid, *reserve;
	u8 id[ELEMENTS_ASSET_LEN - 1];

	/* Subdaemons only learn the chain from their init message. */
	if (initialized || !chainparams)
		return;
	initialized = true;

	if (!chainparams->is_elements)
		return;

	hexid = getenv("CLN_DEV_LIQUID_ASSET");
	if (!hexid || !hex_decode(hexid, strlen(hexid), id, sizeof(id)))
		return;

	/* elements-cli prints asset ids byte-reversed, like txids. */
	asset_tag[0] = 0x01;
	for (size_t i = 0; i < sizeof(id); i++)
		asset_tag[1 + i] = id[sizeof(id) - 1 - i];

	reserve = getenv("CLN_DEV_LIQUID_FEE_RESERVE_SAT");
	fee_reserve = amount_sat(reserve ? strtoull(reserve, NULL, 10) : 100000);
	asset_mode = true;
}

const u8 *liquid_channel_asset(void)
{
	liquid_asset_init();
	return asset_mode ? asset_tag : NULL;
}

struct amount_sat liquid_fee_reserve(void)
{
	liquid_asset_init();
	assert(asset_mode);
	return fee_reserve;
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
