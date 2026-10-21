#include "config.h"
#include <assert.h>
#include <stdint.h>
#include <tests/fuzz/libfuzz.h>
#include <tests/fuzz/wire.h>
#include <wire/peer_wire.h>

struct funding_signed {
	struct channel_id channel_id;
	secp256k1_ecdsa_signature signature;
	struct tlv_funding_signed_tlvs *tlvs;
};

static void *encode(const tal_t *ctx, const struct funding_signed *s)
{
	return towire_funding_signed(ctx, &s->channel_id, &s->signature,
				     s->tlvs);
}

static struct funding_signed *decode(const tal_t *ctx, const void *p)
{
	struct funding_signed *s = tal(ctx, struct funding_signed);

	if (fromwire_funding_signed(s, p, &s->channel_id, &s->signature,
				    &s->tlvs))
		return s;
	return tal_free(s);
}

static bool equal(const struct funding_signed *x,
		  const struct funding_signed *y)
{
	size_t upto_tlvs = (uintptr_t)&x->tlvs - (uintptr_t)x;
	if (memcmp(x, y, upto_tlvs) != 0)
		return false;

	assert(x->tlvs && y->tlvs);
	if (!x->tlvs->fee_reserve_sig || !y->tlvs->fee_reserve_sig)
		return x->tlvs->fee_reserve_sig == y->tlvs->fee_reserve_sig;
	return memcmp(x->tlvs->fee_reserve_sig, y->tlvs->fee_reserve_sig,
		      sizeof(*x->tlvs->fee_reserve_sig)) == 0;
}

void run(const u8 *data, size_t size)
{
	test_decode_encode(data, size, WIRE_FUNDING_SIGNED,
			   struct funding_signed);
}
