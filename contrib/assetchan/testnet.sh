#!/bin/sh
# Two lightningd nodes from this tree, A and B, on the real Liquid testnet
# (test coins only), with the assetchan plugin.  elementsd is reached over
# RPC at $ELEMENTS_RPC; its wallet funds the channels and takes the assets
# back at close.  The nodes talk to each other over localhost.
#
#   contrib/assetchan/testnet.sh start A [lightningd options...]
#   contrib/assetchan/testnet.sh stop A
#   contrib/assetchan/testnet.sh cli A getinfo        # lightning-cli
#   contrib/assetchan/testnet.sh ecli getblockcount   # elements-cli, via docker
#   contrib/assetchan/testnet.sh log A                # tail the log
#
# Environment: ASSETCHAN_TESTNET_DIR (node data; default ~/.assetchan-testnet,
# never inside the source tree), ELEMENTS_RPC (default
# http://poc:poc@127.0.0.1:7039: the liquid-ln-testnet docker stack's
# elementsd, exposed with `docker run -d --name assetchan-elements-proxy
# --network liquid-ln-testnet_default -p 127.0.0.1:7039:7039 alpine/socat
# tcp-listen:7039,fork,reuseaddr tcp:elementsd:7039`), ELEMENTS_WALLET (poc),
# ELEMENTS_CLI (how to run elements-cli; default: in that docker container;
# lightningd's bcli plugin gets a wrapper script for it),
# CHANNEL_ASSET (the asset id the nodes take channels in; see `testnet.sh
# asset`).  python3 with pyln-client must be on PATH for the plugin.
set -e

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
dir=${ASSETCHAN_TESTNET_DIR:-$HOME/.assetchan-testnet}
rpc=${ELEMENTS_RPC:-http://poc:poc@127.0.0.1:7039}
wallet=${ELEMENTS_WALLET:-poc}
ecli=${ELEMENTS_CLI:-docker exec liquid-ln-testnet-elementsd-1 elements-cli -chain=liquidtestnet -rpcport=7039 -rpcuser=poc -rpcpassword=poc}

# user:pass@host:port out of the RPC url
userpass=$(printf '%s' "$rpc" | sed -E 's|^[a-z]+://([^@]*)@.*|\1|')
hostport=$(printf '%s' "$rpc" | sed -E 's|^[a-z]+://([^@]*@)?([^/]*).*|\2|')

port_of() {
	case "$1" in
	A) echo 20741 ;;
	B) echo 20742 ;;
	*) echo "no such node: $1" >&2; exit 1 ;;
	esac
}

cmd=$1
shift || true
case "$cmd" in
start)
	node=$1
	shift
	port=$(port_of "$node")
	mkdir -p "$dir/$node"
	[ -n "$CHANNEL_ASSET" ] || CHANNEL_ASSET=$(cat "$dir/asset" 2> /dev/null || true)
	[ -n "$CHANNEL_ASSET" ] || { echo "set CHANNEL_ASSET (or $dir/asset)" >&2; exit 1; }
	# bcli runs elements-cli: a wrapper, as ours may be in a container
	printf '#!/bin/sh\nexec %s "$@"\n' "$(printf '%s' "$ecli" | sed 's/docker exec /docker exec -i /')" \
		> "$dir/elements-cli"
	chmod +x "$dir/elements-cli"
	# (--daemon: the daemon keeps our stdout, so give it its own)
	exec "$root/lightningd/lightningd" \
		--network=liquid-testnet \
		--lightning-dir="$dir/$node" \
		--alias="assetchan-$node" \
		--bind-addr=127.0.0.1:"$port" \
		--bitcoin-rpcconnect="${hostport%:*}" \
		--bitcoin-rpcport="${hostport#*:}" \
		--bitcoin-rpcuser="${userpass%:*}" \
		--bitcoin-rpcpassword="${userpass#*:}" \
		--bitcoin-cli="$dir/elements-cli" \
		--plugin="$here/assetchan.py" \
		--channel-asset="$CHANNEL_ASSET" \
		--assetchan-elements-rpc="$rpc" \
		--assetchan-elements-wallet="$wallet" \
		--watchtime-blocks=6 \
		--funding-confirms=2 \
		--log-level=debug \
		--log-file="$dir/$node/log" \
		--developer \
		--daemon \
		"$@" > "$dir/$node/stdout" 2>&1
	;;
stop)
	"$root/cli/lightning-cli" --network=liquid-testnet --lightning-dir="$dir/$1" stop
	;;
cli)
	node=$1
	shift
	exec "$root/cli/lightning-cli" --network=liquid-testnet --lightning-dir="$dir/$node" "$@"
	;;
ecli)
	exec $ecli -rpcwallet="$wallet" "$@"
	;;
log)
	exec tail -n "${2:-30}" -f "$dir/$1/log"
	;;
*)
	sed -n '2,/^set -e/p' "$0" | grep '^#' | cut -c3-
	exit 1
	;;
esac
