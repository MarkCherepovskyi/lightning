/* PoC: catch up on Liquid asset gossip networks, see liquid_sync.h */
#include "config.h"
#include <ccan/asort/asort.h>
#include <common/gossmap.h>
#include <common/status.h>
#include <common/timeout.h>
#include <common/utils.h>
#include <gossipd/gossipd.h>
#include <gossipd/gossmap_manage.h>
#include <gossipd/liquid_sync.h>
#include <gossipd/queries.h>

/* Keep each query well inside one message. */
#define MAX_SCIDS_PER_QUERY 1000

struct liquid_sync {
	struct peer *peer;
	/* Which of peer->liquid_networks we're catching up on */
	size_t network_idx;
	/* NULL until we know which channels it has; then the ones we still
	 * have to ask about (ascending) */
	struct short_channel_id *wanted;
	/* The peer takes one query of each kind at a time, and the seeker
	 * uses it too: if it's busy, we try again later. */
	struct oneshot *retry_timer;
};

static void next_step(struct liquid_sync *ls);

static const struct bitcoin_blkid *network(const struct liquid_sync *ls)
{
	return &ls->peer->liquid_networks[ls->network_idx];
}

static void retry_later(struct liquid_sync *ls)
{
	ls->retry_timer = new_reltimer(&ls->peer->daemon->timers, ls,
				       time_from_sec(1), next_step, ls);
}

static int cmp_scid(const struct short_channel_id *a,
		    const struct short_channel_id *b,
		    void *unused)
{
	if (a->u64 > b->u64)
		return 1;
	else if (a->u64 < b->u64)
		return -1;
	return 0;
}

static void range_done(struct peer *peer,
		       u32 first_blocknum, u32 number_of_blocks,
		       const struct range_query_reply *replies)
{
	struct liquid_sync *ls = peer->liquid_sync;
	struct gossmap *gossmap = gossmap_manage_get_gossmap(peer->daemon->gm);
	size_t n = 0;

	ls->wanted = tal_arr(ls, struct short_channel_id, 0);
	for (size_t i = 0; i < tal_count(replies); i++) {
		if (!gossmap_find_chan(gossmap, &replies[i].scid))
			tal_arr_expand(&ls->wanted, replies[i].scid);
	}

	/* Queries need ascending, unique scids */
	asort(ls->wanted, tal_count(ls->wanted), cmp_scid, NULL);
	for (size_t i = 0; i < tal_count(ls->wanted); i++) {
		if (n == 0 || ls->wanted[i].u64 != ls->wanted[n - 1].u64)
			ls->wanted[n++] = ls->wanted[i];
	}
	tal_resize(&ls->wanted, n);

	status_peer_debug(&peer->id,
			  "liquid_sync: network %s has %zu channels, %zu new to us",
			  fmt_bitcoin_blkid(tmpctx, network(ls)),
			  tal_count(replies), n);
	next_step(ls);
}

static void scids_done(struct peer *peer, bool complete)
{
	next_step(peer->liquid_sync);
}

static void next_step(struct liquid_sync *ls)
{
	struct peer *peer = ls->peer;
	struct daemon *daemon = peer->daemon;

	ls->retry_timer = tal_free(ls->retry_timer);

	/* Start on the next network: which channels does it have? */
	if (!ls->wanted) {
		if (ls->network_idx == tal_count(peer->liquid_networks)) {
			peer->liquid_sync = NULL;
			tal_free(ls);
			return;
		}
		if (peer->range_replies) {
			retry_later(ls);
			return;
		}
		query_channel_range(daemon, peer, network(ls), 0, UINT32_MAX, 0,
				    range_done);
		return;
	}

	/* Done with this network? */
	if (tal_count(ls->wanted) == 0) {
		ls->wanted = tal_free(ls->wanted);
		ls->network_idx++;
		next_step(ls);
		return;
	}

	/* Ask about (the next lot of) the channels we don't know */
	if (peer->scid_query_outstanding) {
		retry_later(ls);
		return;
	}
	size_t n = tal_count(ls->wanted);
	if (n > MAX_SCIDS_PER_QUERY)
		n = MAX_SCIDS_PER_QUERY;
	if (!query_short_channel_ids(daemon, peer, network(ls),
				     tal_dup_arr(tmpctx, struct short_channel_id,
						 ls->wanted, n, 0),
				     NULL, scids_done)) {
		/* Can't happen (the peer offers gossip queries): give up */
		status_peer_broken(&peer->id, "liquid_sync: can't query %zu scids", n);
		peer->liquid_sync = NULL;
		tal_free(ls);
		return;
	}
	memmove(ls->wanted, ls->wanted + n,
		(tal_count(ls->wanted) - n) * sizeof(*ls->wanted));
	tal_resize(&ls->wanted, tal_count(ls->wanted) - n);
}

void liquid_sync_peer(struct daemon *daemon, struct peer *peer)
{
	struct liquid_sync *ls;

	if (!peer->gossip_queries_feature || tal_count(peer->liquid_networks) == 0)
		return;

	ls = tal(peer, struct liquid_sync);
	ls->peer = peer;
	ls->network_idx = 0;
	ls->wanted = NULL;
	ls->retry_timer = NULL;
	peer->liquid_sync = ls;
	next_step(ls);
}
