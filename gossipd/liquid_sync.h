#ifndef LIGHTNING_GOSSIPD_LIQUID_SYNC_H
#define LIGHTNING_GOSSIPD_LIQUID_SYNC_H
#include "config.h"

struct daemon;
struct peer;

/* PoC: catch up on the Liquid asset gossip networks (see
 * common/liquid_asset.h) we and this peer both follow.  For each, ask for
 * every channel in it (query_channel_range), then for the ones we don't
 * know yet (query_short_channel_ids), using that network's chain_hash.  The
 * seeker does the same, more elaborately, for the chain's own network. */
void liquid_sync_peer(struct daemon *daemon, struct peer *peer);

#endif /* LIGHTNING_GOSSIPD_LIQUID_SYNC_H */
