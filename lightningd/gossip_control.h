#ifndef LIGHTNING_LIGHTNINGD_GOSSIP_CONTROL_H
#define LIGHTNING_LIGHTNINGD_GOSSIP_CONTROL_H
#include "config.h"
#include <bitcoin/block.h>
#include <bitcoin/short_channel_id.h>

struct channel;
struct lightningd;

void gossip_init(struct lightningd *ld, int connectd_fd);

/* PoC: the gossip networks of the Liquid assets we follow
 * (liquid-gossip-asset, see common/liquid_asset.h). */
struct bitcoin_blkid *liquid_gossip_networks(const tal_t *ctx,
					     const struct lightningd *ld);

void gossipd_notify_spends(struct lightningd *ld,
			   u32 blockheight,
			   const struct short_channel_id *scids);

void gossip_notify_new_block(struct lightningd *ld);

#endif /* LIGHTNING_LIGHTNINGD_GOSSIP_CONTROL_H */
