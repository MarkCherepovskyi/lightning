#ifndef LIGHTNING_COMMON_PER_PEER_STATE_H
#define LIGHTNING_COMMON_PER_PEER_STATE_H
#include "config.h"

#include <common/crypto_state.h>

/* Things we hand between daemons to talk to peers. */
struct per_peer_state {
	/* If not -1, closed on freeing */
	int peer_fd;
};

/* Allocate a new per-peer state and add destructor to close fds if set;
 * sets peer_fd to -1. */
struct per_peer_state *new_per_peer_state(const tal_t *ctx);

/* Initialize the fds (must be -1 previous) */
void per_peer_state_set_fd(struct per_peer_state *pps, int peer_fd);

void per_peer_state_fdpass_send(int fd, const struct per_peer_state *pps);

/* On macOS, a socket whose only reference is in flight over SCM_RIGHTS can
 * arrive unable to read, so after per_peer_state_fdpass_send() to lightningd
 * we must keep our copy (i.e. not exit) until it has the fd.  It frees us
 * once it does, so wait for it to close fd (or kill us), but not forever. */
void per_peer_state_fdpass_wait(int fd);

#endif /* LIGHTNING_COMMON_PER_PEER_STATE_H */
