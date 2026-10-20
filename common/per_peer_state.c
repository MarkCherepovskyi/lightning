#include "config.h"
#include <assert.h>
#include <ccan/fdpass/fdpass.h>
#include <ccan/time/time.h>
#include <common/per_peer_state.h>
#include <errno.h>
#include <poll.h>
#include <unistd.h>

bool dev_fast_gossip = false;

static void destroy_per_peer_state(struct per_peer_state *pps)
{
	if (pps->peer_fd != -1)
		close(pps->peer_fd);
}

struct per_peer_state *new_per_peer_state(const tal_t *ctx)
{
	struct per_peer_state *pps = tal(ctx, struct per_peer_state);

	pps->peer_fd = -1;
	tal_add_destructor(pps, destroy_per_peer_state);
	return pps;
}

void per_peer_state_set_fd(struct per_peer_state *pps, int peer_fd)
{
	assert(pps->peer_fd == -1);
	pps->peer_fd = peer_fd;
}

void per_peer_state_fdpass_send(int fd, const struct per_peer_state *pps)
{
	assert(pps->peer_fd != -1);
	fdpass_send(fd, pps->peer_fd);
}

void per_peer_state_fdpass_wait(int fd)
{
	struct timemono deadline = timemono_add(time_mono(), time_from_sec(30));

	while (timemono_before(time_mono(), deadline)) {
		struct pollfd pfd = { .fd = fd, .events = POLLIN };
		char buf[1024];
		ssize_t r;

		if (poll(&pfd, 1, 1000) < 0 && errno != EINTR)
			return;
		if (pfd.revents & POLLNVAL)
			return;
		if (!(pfd.revents & (POLLIN|POLLHUP|POLLERR)))
			continue;
		/* We're done, so whatever else it sends goes unanswered. */
		r = read(fd, buf, sizeof(buf));
		if (r == 0 || (r < 0 && errno != EINTR && errno != EAGAIN))
			return;
	}
}
