#!/usr/bin/env bash
# Loads assetswap-plugin and pricetiers-plugin on the nodes of contrib/nodeui/testnet.sh
# (started with BTC_NODES="X Y Z"), so its page's Swap tab can swap between all
# three chains. Rerun after any of these nodes restarts: `plugin start` options
# don't survive one.
#
#   contrib/liquid-poc/asset-swap/testnet-swaps.sh          # (re)load everywhere
#   contrib/liquid-poc/asset-swap/testnet-swaps.sh LD T     # (re)load only these
#   contrib/liquid-poc/asset-swap/testnet-swaps.sh stop     # unload everywhere
#
# Reloading needs lightningd with the htlc_accepted fix of this branch
# (lightningd/plugin_hook.c): an older one, once a hook answered "fail" or
# "resolve", defers the new plugin's htlc_accepted hook forever ("Deferring
# registration of hook htlc_accepted until it's not in use"), so a provider
# loses its hook. Restart such a node before reloading it.
#
# The provider runs one node per chain, each pricing its own coin against the
# others (1:1 reference rates) and paying leg 2 on the other chains through
# its sibling's RPC socket:
#
#   Liquid testnet  LD   asset ddc8da3f   leg 2 in eth -> T, in btc -> Y
#   Base Sepolia    T    ETH              leg 2 in the asset -> LD, in btc -> Y
#   Bitcoin regtest Y    BTC              leg 2 in eth -> T, in the asset -> LD
#
# Payers: LC (asset, via LD), S (ETH, via T), X (BTC, via Y). Every one of
# them, and the payees U, LE and Z, can make an assetinvoice.
set -euo pipefail

root=$(cd "$(dirname "$0")/../../.." && pwd)
cli=$root/cli/lightning-cli
bin=$root/target/debug
LIQUID_DIR=${ASSETCHAN_DIR:-$HOME/.assetchan-testnet}
EVM_DIR=${EVM_DIR:-$HOME/.evmchan-testnet}
BTC_DIR=${BTC_DIR:-$HOME/.btc-regtest}
ASSET=$(cat "$LIQUID_DIR/asset")
RATE=1000000:0

ln() {      # ln NODE ARGS...: lightning-cli of a page node by its page name
	case "$1" in
	L?) "$cli" --network=liquid-testnet --lightning-dir="$LIQUID_DIR/${1#L}" "${@:2}" ;;
	[P-U]) "$cli" --network=regtest --lightning-dir="$EVM_DIR/$1" "${@:2}" ;;
	*) "$cli" --network=regtest --lightning-dir="$BTC_DIR/$1" "${@:2}" ;;
	esac
}
id() { ln "$1" getinfo | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }
sock() {
	case "$1" in
	L?) echo "$LIQUID_DIR/${1#L}/liquid-testnet/lightning-rpc" ;;
	[P-U]) echo "$EVM_DIR/$1/regtest/lightning-rpc" ;;
	*) echo "$BTC_DIR/$1/regtest/lightning-rpc" ;;
	esac
}
unload() {
	local p
	for p in assetswap-plugin pricetiers-plugin; do
		ln "$1" -k plugin subcommand=stop plugin="$p" > /dev/null 2>&1 || true
	done
}
ONLY=" ${*:-} "
load() {    # load NODE [pricetiers-pair...] -- [assetswap options...]
	local node=$1 pairs=() opts=()
	shift
	case "$ONLY" in "  "|*" $node "*) ;; *) return 0 ;; esac
	while [ $# -gt 0 ] && [ "$1" != -- ]; do pairs+=("pricetiers-pair=$1"); shift; done
	[ $# -gt 0 ] && shift
	opts=("$@")
	unload "$node"
	if [ ${#pairs[@]} -gt 0 ]; then
		ln "$node" -k plugin subcommand=start plugin="$bin/pricetiers-plugin" "${pairs[@]}" > /dev/null
	fi
	# (${a[@]+"${a[@]}"}: an empty array is unbound under set -u in macOS's bash 3.2)
	ln "$node" -k plugin subcommand=start plugin="$bin/assetswap-plugin" ${opts[@]+"${opts[@]}"} > /dev/null
	echo "$node: assetswap${pairs[*]+, ${pairs[*]}}${opts[*]+, ${opts[*]}}"
}

if [ "${1:-}" = stop ]; then
	for n in LC LD LE S T U X Y Z; do unload "$n"; echo "$n: unloaded"; done
	exit 0
fi

LD=$(id LD) T=$(id T) Y=$(id Y)
# Providers: price their own coin against the others, pay leg 2 through the sibling.
load LD "$ASSET:eth:$RATE" "$ASSET:btc:$RATE" -- "assetswap-remote=eth:$(sock T)" "assetswap-remote=btc:$(sock Y)"
load T "eth:$ASSET:$RATE" "eth:btc:$RATE" -- "assetswap-remote=$ASSET:$(sock LD)" "assetswap-remote=btc:$(sock Y)"
load Y "btc:eth:$RATE" "btc:$ASSET:$RATE" -- "assetswap-remote=eth:$(sock T)" "assetswap-remote=$ASSET:$(sock LD)"
# Payers: the same pairs, for payassetinvoice's local estimate.
load LC "$ASSET:eth:$RATE" "$ASSET:btc:$RATE" -- "assetswap-provider=$LD"
load S "eth:$ASSET:$RATE" "eth:btc:$RATE" -- "assetswap-provider=$T"
load X "btc:eth:$RATE" "btc:$ASSET:$RATE" -- "assetswap-provider=$Y"
# Payees: assetinvoice only.
for n in LE U Z; do load "$n"; done
echo "Restart the page (it detects plugins when it starts) to see them in its Swap tab."
