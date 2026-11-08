#!/bin/sh
# lightningd nodes from this tree (A, B, C, D) with the assetchan plugin, on
# the real Liquid testnet (test coins only) by default, or on a Liquid regtest.
# elementsd is reached over RPC at $ELEMENTS_RPC; its wallet funds the
# channels and takes the assets back at close.  The nodes talk to each other
# over localhost.
#
#   contrib/assetchan/nodes.sh start A [lightningd options...]
#   contrib/assetchan/nodes.sh stop A
#   contrib/assetchan/nodes.sh cli A getinfo        # lightning-cli
#   contrib/assetchan/nodes.sh ecli getblockcount   # elements-cli
#   contrib/assetchan/nodes.sh mine [n]             # regtest: mine n (1) blocks
#   contrib/assetchan/nodes.sh log A                # tail the log
#
# Environment: ASSETCHAN_NETWORK (liquid-testnet, or liquid-regtest),
# ASSETCHAN_PORT_BASE (node ports: base + 1..4, default 20740),
# ASSETCHAN_DIR (node data; default ~/.assetchan-testnet or ~/.assetchan-regtest,
# never inside the source tree), ELEMENTS_RPC (default
# http://poc:poc@127.0.0.1:7039 on testnet: the liquid-ln-testnet docker
# stack's elementsd, exposed with `docker run -d --name assetchan-elements-proxy
# --network liquid-ln-testnet_default -p 127.0.0.1:7039:7039 alpine/socat
# tcp-listen:7039,fork,reuseaddr tcp:elementsd:7039`; on regtest
# http://poc:poc@127.0.0.1:18884, the liquid-ln-poc stack's, exposed the same
# way on network liquid-ln-poc_default), ELEMENTS_WALLET (poc), ELEMENTS_CLI
# (how to run elements-cli; default: in that docker container; lightningd's
# bcli plugin gets a wrapper script for it), CHANNEL_ASSET (the asset id the
# nodes take channels in; default: $ASSETCHAN_DIR/asset).  python3 with
# pyln-client must be on PATH for the plugin.
set -e

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
network=${ASSETCHAN_NETWORK:-liquid-testnet}
case "$network" in
liquid-testnet)
	dir=${ASSETCHAN_DIR:-$HOME/.assetchan-testnet}
	rpc=${ELEMENTS_RPC:-http://poc:poc@127.0.0.1:7039}
	ecli=${ELEMENTS_CLI:-docker exec liquid-ln-testnet-elementsd-1 elements-cli -chain=liquidtestnet -rpcport=7039 -rpcuser=poc -rpcpassword=poc}
	;;
liquid-regtest)
	dir=${ASSETCHAN_DIR:-$HOME/.assetchan-regtest}
	rpc=${ELEMENTS_RPC:-http://poc:poc@127.0.0.1:18884}
	ecli=${ELEMENTS_CLI:-docker exec liquid-ln-poc-elementsd-1 elements-cli -chain=liquid-regtest -rpcport=18884 -rpcuser=poc -rpcpassword=poc}
	;;
*)
	echo "ASSETCHAN_NETWORK must be liquid-testnet or liquid-regtest" >&2
	exit 1
	;;
esac
wallet=${ELEMENTS_WALLET:-poc}

# user:pass@host:port out of the RPC url
userpass=$(printf '%s' "$rpc" | sed -E 's|^[a-z]+://([^@]*)@.*|\1|')
hostport=$(printf '%s' "$rpc" | sed -E 's|^[a-z]+://([^@]*@)?([^/]*).*|\2|')

# Node ports: ASSETCHAN_PORT_BASE + 1..4 (default 20740), so nodes of two
# networks can run side by side (e.g. ASSETCHAN_PORT_BASE=20760 for testnet).
port_of() {
	base=${ASSETCHAN_PORT_BASE:-20740}
	case "$1" in
	A) echo $((base + 1)) ;;
	B) echo $((base + 2)) ;;
	C) echo $((base + 3)) ;;
	D) echo $((base + 4)) ;;
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
		--network="$network" \
		--lightning-dir="$dir/$node" \
		--alias="assetchan-$node" \
		--bind-addr=127.0.0.1:"$port" \
		--bitcoin-rpcconnect="${hostport%:*}" \
		--bitcoin-rpcport="${hostport#*:}" \
		--bitcoin-rpcuser="${userpass%:*}" \
		--bitcoin-rpcpassword="${userpass#*:}" \
		--bitcoin-cli="$dir/elements-cli" \
		--plugin="$here/assetchan.py" \
		--assetchan-asset="$CHANNEL_ASSET" \
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
	"$root/cli/lightning-cli" --network="$network" --lightning-dir="$dir/$1" stop
	;;
cli)
	node=$1
	shift
	exec "$root/cli/lightning-cli" --network="$network" --lightning-dir="$dir/$node" "$@"
	;;
ecli)
	exec $ecli -rpcwallet="$wallet" "$@"
	;;
mine)
	[ "$network" = liquid-regtest ] || { echo "nobody mines on $network" >&2; exit 1; }
	$ecli -rpcwallet="$wallet" generatetoaddress "${1:-1}" "$($ecli -rpcwallet="$wallet" getnewaddress)"
	;;
log)
	exec tail -n "${2:-30}" -f "$dir/$1/log"
	;;
*)
	sed -n '2,/^set -e/p' "$0" | grep '^#' | cut -c3-
	exit 1
	;;
esac
