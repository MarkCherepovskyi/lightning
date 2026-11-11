#!/usr/bin/env bash
# The test environment behind contrib/nodeui, in one command: lightningd nodes on
# Base Sepolia (EVM UtxoChain, contrib/evmchan), on the real Liquid testnet
# (contrib/assetchan) and on a local Bitcoin regtest, and the page that manages them.
#
#   contrib/nodeui/testnet.sh start  [all|evm|liquid|btc|ui]   (default all)
#   contrib/nodeui/testnet.sh stop   [all|evm|liquid|btc|ui]
#   contrib/nodeui/testnet.sh status
#   contrib/nodeui/testnet.sh cli NODE ARGS...   lightning-cli of P, LA, X, ...
#
# start skips whatever already runs, so it is safe to repeat. EVM nodes start one
# at a time, 10 s apart, and a node with an empty index starts from the snapshot:
# fresh nodes catching up at once trip Infura's per-second limit (HTTP 429).
#
# Needs: this tree built (lightningd, cli), its .venv (pyln-client, coincurve);
# INFURA_API_KEY in the environment for the EVM nodes (it is read from there by the
# plugin and never written anywhere; `. contrib/evmchan/testnet-env.sh` loads it from
# the macOS Keychain); for Liquid the liquid-ln-testnet docker stack
# (elementsd) with the socat proxy container on 127.0.0.1:7039 (see
# contrib/assetchan/nodes.sh); bitcoind/bitcoin-cli on PATH for Bitcoin regtest.
#
# A new EVM node needs ETH for gas (at least EVM_MIN_BALANCE) at the address that
# `status` prints, from a faucet or another key, and a Deposit before it can open
# channels. A new Liquid node gets L-BTC with the page's "Fund node".
#
# Environment (default):
#   EVM_NODES (P Q R S T U), EVM_DIR (~/.evmchan-testnet), EVM_PORT_BASE (20780: P is 20781)
#   EVM_CONTRACT (the ETH UtxoChain on Base Sepolia), EVM_POLL (180 s), EVM_MIN_BALANCE (0.003)
#   EVM_SNAPSHOT ($EVM_DIR/snap/eth-index.jsonl.gz; export one with
#                 `python3 contrib/evmchan/evmindex.py <a node's evmchan.sqlite3> <out>`)
#   LIQUID_NODES (A B C D E F, shown as LA..LF), ASSETCHAN_PORT_BASE (20760), ASSETCHAN_DIR,
#   ELEMENTS_RPC, ELEMENTS_WALLET, ELEMENTS_PROXY (assetchan-elements-proxy)
#   BTC_NODES (X Y), BTC_DIR (~/.btc-regtest), BTC_PORT_BASE (20790), BTC_RPC_PORT (18443)
#   UI_PORT (8780), NODES_JSON ($EVM_DIR/nodes.json, written by start)
set -euo pipefail
# Nothing here needs a private key: no child (lightningd, plugins, the page) gets one.
unset DEPLOYER_PRIVATE_KEY

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
venv=${VENV:-$root/.venv}
export PATH="$venv/bin:$PATH"
export PYTHONPATH="$root/contrib/pyln-client:$root/contrib/pyln-proto${PYTHONPATH:+:$PYTHONPATH}"
py="$venv/bin/python3"

EVM_NODES=${EVM_NODES:-P Q R S T U}
EVM_DIR=${EVM_DIR:-$HOME/.evmchan-testnet}
EVM_PORT_BASE=${EVM_PORT_BASE:-20780}
EVM_CONTRACT=${EVM_CONTRACT:-0xB8203517Ee8718d59b93d9E7c8F6Ee55BB3a940E}
EVM_POLL=${EVM_POLL:-180}
EVM_MIN_BALANCE=${EVM_MIN_BALANCE:-0.003}
EVM_SNAPSHOT=${EVM_SNAPSHOT:-$EVM_DIR/snap/eth-index.jsonl.gz}
EVM_STAGGER=${EVM_STAGGER:-10}
LIQUID_NODES=${LIQUID_NODES:-A B C D E F}
export ASSETCHAN_PORT_BASE=${ASSETCHAN_PORT_BASE:-20760}
export ASSETCHAN_NETWORK=liquid-testnet
LIQUID_DIR=${ASSETCHAN_DIR:-$HOME/.assetchan-testnet}
ELEMENTS_RPC=${ELEMENTS_RPC:-http://poc:poc@127.0.0.1:7039}
ELEMENTS_WALLET=${ELEMENTS_WALLET:-poc}
ELEMENTS_PROXY=${ELEMENTS_PROXY:-assetchan-elements-proxy}
BTC_NODES=${BTC_NODES:-X Y}
BTC_DIR=${BTC_DIR:-$HOME/.btc-regtest}
BTC_PORT_BASE=${BTC_PORT_BASE:-20790}
BTC_RPC_PORT=${BTC_RPC_PORT:-18443}
BTC_RPC_USER=${BTC_RPC_USER:-u}
BTC_RPC_PASS=${BTC_RPC_PASS:-p}
UI_PORT=${UI_PORT:-8780}
NODES_JSON=${NODES_JSON:-$EVM_DIR/nodes.json}
UI_PID=$EVM_DIR/ui.pid
UI_LOG=$EVM_DIR/ui.log
# The ERC-20 the token UtxoChain on Base Sepolia holds, named on the page.
TOKEN_LABELS=${TOKEN_LABELS:-'{"0x664dc332055b204ca046621ca643313386fbe04a": "tUSD"}'}

say() { printf '%s\n' "$*"; }
die() { printf 'testnet.sh: %s\n' "$*" >&2; exit 1; }
lncli() { "$root/cli/lightning-cli" "$@"; }
evm_cli() { lncli --network=regtest --lightning-dir="$EVM_DIR/$1" "${@:2}"; }
liquid_cli() { "$root/contrib/assetchan/nodes.sh" cli "$1" "${@:2}"; }
btc_cli() { lncli --network=regtest --lightning-dir="$BTC_DIR/$1" "${@:2}"; }
bcli() { bitcoin-cli -datadir="$BTC_DIR/bitcoind" -regtest -rpcport="$BTC_RPC_PORT" \
         -rpcuser="$BTC_RPC_USER" -rpcpassword="$BTC_RPC_PASS" "$@"; }
# json FIELD...: fields of the JSON on stdin, space-separated ('-' when missing).
json() { "$py" -c 'import json, sys
d = json.load(sys.stdin)
print(" ".join("-" if d.get(k) is None else str(d[k]) for k in sys.argv[1:]))' "$@"; }
listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t > /dev/null 2>&1; }

[ -x "$root/lightningd/lightningd" ] || die "build this tree first ($root/lightningd/lightningd is missing)"
[ -x "$py" ] || die "no venv at $venv (python3 -m venv .venv && .venv/bin/pip install coincurve ...)"

# ------------------------------------------------------------------ EVM
start_evm() {
	[ -n "${INFURA_API_KEY:-}" ] || die "set INFURA_API_KEY for the EVM nodes (it stays in the environment)"
	local i=0 first=1 n d port note
	for n in $EVM_NODES; do
		i=$((i + 1)); d=$EVM_DIR/$n; port=$((EVM_PORT_BASE + i))
		if evm_cli "$n" getinfo > /dev/null 2>&1; then say "EVM $n: already running"; continue; fi
		listening "$port" && die "EVM $n: port $port is taken by another process"
		[ "$first" = 1 ] || sleep "$EVM_STAGGER"
		first=0
		mkdir -p "$d"
		# An empty index starts from the snapshot (a few eth_getLogs instead of a full catch-up).
		set -- --evm-confirm=safe
		note=
		if [ ! -e "$d/regtest/evmchan.sqlite3" ] && [ -e "$EVM_SNAPSHOT" ]; then
			set -- "$@" --evm-snapshot="$EVM_SNAPSHOT"
			note=" from the snapshot"
		fi
		# SPEC 5.6's options for public chains: 24 h to_self_delay, 2.4 h CLTV, at INTERVAL 60.
		# (--daemon keeps our stdout open: it gets its own file.)
		"$root/lightningd/lightningd" --daemon --network=regtest --lightning-dir="$d" \
			--log-file="$d/log" --log-level=debug --alias="$n" --bind-addr=127.0.0.1:"$port" \
			--disable-plugin=bcli --plugin="$root/contrib/evmchan/evmbackend.py" \
			--evm-rpc='https://base-sepolia.infura.io/v3/${INFURA_API_KEY}' \
			--evm-contract="$EVM_CONTRACT" --evm-poll="$EVM_POLL" \
			--evm-min-balance="$EVM_MIN_BALANCE" "$@" \
			--watchtime-blocks=1440 --cltv-delta=144 --cltv-final=144 --funding-confirms=3 \
			--max-concurrent-htlcs=30 --developer --dev-force-features=-63 \
			> "$d/stdout" 2>&1 || { tail -5 "$d/log" >&2 || true; die "EVM $n did not start (log: $d/log)"; }
		say "EVM $n: started on port $port$note"
	done
}

stop_evm() {
	local n
	for n in $EVM_NODES; do
		if evm_cli "$n" stop > /dev/null 2>&1; then say "EVM $n: stopped"; fi
	done
}

status_evm() {
	local n st
	for n in $EVM_NODES; do
		if ! st=$(evm_cli "$n" evm-status cached=true 2> /dev/null); then say "  $n  down"; continue; fi
		printf '%s' "$st" | "$py" -c 'import json, sys
s = json.load(sys.stdin)
gas = (s.get("balance_wei") or 0) / 1e18
line = "  {}  tip {}  lag {}  gas {:.6f} ETH  {}".format(sys.argv[1], s.get("tip"), s.get("lag"), gas, s.get("eth_address"))
if s.get("opens_refused"):
    line += "\n      opens refused: " + s["opens_refused"]
for k, v in sorted((s.get("alarms") or {}).items()):
    line += "\n      ALARM {}: {}".format(k, v)
print(line)' "$n"
	done
}

# ------------------------------------------------------------------ Liquid
start_liquid() {
	command -v docker > /dev/null || die "Liquid needs docker (the liquid-ln-testnet stack's elementsd)"
	if ! listening 7039; then
		if docker inspect "$ELEMENTS_PROXY" > /dev/null 2>&1; then
			docker start "$ELEMENTS_PROXY" > /dev/null
		else
			die "nothing on 127.0.0.1:7039: start the liquid-ln-testnet stack and the proxy (contrib/assetchan/nodes.sh's header)"
		fi
	fi
	if ! "$root/contrib/assetchan/nodes.sh" ecli getwalletinfo > /dev/null 2>&1; then
		"$root/contrib/assetchan/nodes.sh" ecli loadwallet "$ELEMENTS_WALLET" > /dev/null \
			|| die "elementsd wallet $ELEMENTS_WALLET is not loaded and could not be"
		say "elementsd: loaded wallet $ELEMENTS_WALLET"
	fi
	local n
	for n in $LIQUID_NODES; do
		if liquid_cli "$n" getinfo > /dev/null 2>&1; then say "Liquid L$n: already running"; continue; fi
		"$root/contrib/assetchan/nodes.sh" start "$n" || die "Liquid L$n did not start (log: $LIQUID_DIR/$n/log)"
		say "Liquid L$n: started"
	done
}

stop_liquid() {
	local n
	for n in $LIQUID_NODES; do
		if liquid_cli "$n" stop > /dev/null 2>&1; then say "Liquid L$n: stopped"; fi
	done
}

status_liquid() {
	local n
	for n in $LIQUID_NODES; do
		local info
		if ! info=$(liquid_cli "$n" getinfo 2> /dev/null); then say "  L$n  down"; continue; fi
		set -- $(printf '%s' "$info" | json blockheight num_active_channels)
		say "  L$n  height $1  channels $2"
	done
}

# ------------------------------------------------------------------ Bitcoin regtest
start_btc() {
	command -v bitcoind > /dev/null || die "Bitcoin regtest needs bitcoind on PATH"
	if ! bcli getblockchaininfo > /dev/null 2>&1; then
		mkdir -p "$BTC_DIR/bitcoind" && chmod 700 "$BTC_DIR"
		bitcoind -datadir="$BTC_DIR/bitcoind" -regtest -daemon -fallbackfee=0.0001 -txindex \
			-rpcport="$BTC_RPC_PORT" -port=$((BTC_RPC_PORT + 1)) \
			-rpcuser="$BTC_RPC_USER" -rpcpassword="$BTC_RPC_PASS" > /dev/null
		local i
		for i in $(seq 30); do bcli getblockchaininfo > /dev/null 2>&1 && break; sleep 1; done
		say "bitcoind regtest: started"
	fi
	if ! bcli listwallets | grep -q '"w"'; then
		bcli loadwallet w > /dev/null 2>&1 || bcli createwallet w > /dev/null
	fi
	if [ "$(bcli getblockcount)" -lt 101 ]; then
		bcli -generate 101 > /dev/null
		say "bitcoind regtest: mined 101 blocks (50 BTC spendable)"
	fi
	local i=0 n d port
	for n in $BTC_NODES; do
		i=$((i + 1)); d=$BTC_DIR/$n; port=$((BTC_PORT_BASE + i))
		if btc_cli "$n" getinfo > /dev/null 2>&1; then say "Bitcoin $n: already running"; continue; fi
		mkdir -p "$d"
		"$root/lightningd/lightningd" --daemon --network=regtest --lightning-dir="$d" --log-file="$d/log" \
			--alias="$n" --bind-addr=127.0.0.1:"$port" --bitcoin-datadir="$BTC_DIR/bitcoind" \
			--bitcoin-rpcuser="$BTC_RPC_USER" --bitcoin-rpcpassword="$BTC_RPC_PASS" \
			--bitcoin-rpcport="$BTC_RPC_PORT" > "$d/stdout" 2>&1 || die "Bitcoin $n did not start (log: $d/log)"
		say "Bitcoin $n: started on port $port"
	done
}

stop_btc() {
	local n
	for n in $BTC_NODES; do
		if btc_cli "$n" stop > /dev/null 2>&1; then say "Bitcoin $n: stopped"; fi
	done
	if bcli stop > /dev/null 2>&1; then say "bitcoind regtest: stopped"; fi
}

status_btc() {
	local n
	say "  bitcoind  $(bcli getblockcount 2> /dev/null || echo down) blocks"
	for n in $BTC_NODES; do
		if ! btc_cli "$n" getinfo > /dev/null 2>&1; then say "  $n  down"; continue; fi
		say "  $n  height $(btc_cli "$n" getinfo | json blockheight)"
	done
}

# ------------------------------------------------------------------ the page
ui_running() {
	[ -f "$UI_PID" ] && ps -o command= -p "$(cat "$UI_PID")" 2> /dev/null | grep -q "nodeui/ui.py"
}

write_nodes_json() {
	mkdir -p "$EVM_DIR"
	(umask 077
	 EVM_NODES="$EVM_NODES" LIQUID_NODES="$LIQUID_NODES" BTC_NODES="$BTC_NODES" EVM_DIR="$EVM_DIR" \
	 LIQUID_DIR="$LIQUID_DIR" BTC_DIR="$BTC_DIR" TOKEN_LABELS="$TOKEN_LABELS" ELEMENTS_RPC="$ELEMENTS_RPC" \
	 ELEMENTS_WALLET="$ELEMENTS_WALLET" BTC_RPC="http://$BTC_RPC_USER:$BTC_RPC_PASS@127.0.0.1:$BTC_RPC_PORT" \
	 "$py" -c 'import json, os
e = os.environ
nodes = [{"name": n, "dir": os.path.join(e["EVM_DIR"], n), "network": "regtest"} for n in e["EVM_NODES"].split()]
nodes += [{"name": "L" + n, "dir": os.path.join(e["LIQUID_DIR"], n), "network": "liquid-testnet"}
          for n in e["LIQUID_NODES"].split()]
nodes += [{"name": n, "dir": os.path.join(e["BTC_DIR"], n), "network": "regtest"} for n in e["BTC_NODES"].split()]
cfg = {"nodes": [x for x in nodes if os.path.isdir(x["dir"])], "labels": json.loads(e["TOKEN_LABELS"]),
       "bitcoin-rpc": e["BTC_RPC"], "elements-rpc": e["ELEMENTS_RPC"], "elements-wallet": e["ELEMENTS_WALLET"]}
print(json.dumps(cfg, indent=1))' > "$NODES_JSON")
}

start_ui() {
	if ui_running; then say "page: already running on http://127.0.0.1:$UI_PORT"; return; fi
	listening "$UI_PORT" && die "port $UI_PORT is taken by another process (set UI_PORT)"
	write_nodes_json
	# The page never needs the Infura key or a private key.
	env -u INFURA_API_KEY -u DEPLOYER_PRIVATE_KEY nohup "$py" "$here/ui.py" --config "$NODES_JSON" \
		--port "$UI_PORT" > "$UI_LOG" 2>&1 &
	echo $! > "$UI_PID"
	local i
	for i in $(seq 30); do listening "$UI_PORT" && break; sleep 1; done
	listening "$UI_PORT" || die "the page did not start (log: $UI_LOG)"
	say "page: http://127.0.0.1:$UI_PORT (config $NODES_JSON)"
}

stop_ui() {
	if ui_running; then kill "$(cat "$UI_PID")"; say "page: stopped"; fi
	rm -f "$UI_PID"
}

# ------------------------------------------------------------------ commands
what=${2:-all}
case "${1:-}" in
start)
	case "$what" in
	all) start_btc; start_liquid; start_evm; start_ui ;;
	evm|liquid|btc|ui) "start_$what" ;;
	*) die "start what? all, evm, liquid, btc or ui" ;;
	esac ;;
stop)
	case "$what" in
	all) stop_ui; stop_evm; stop_liquid; stop_btc ;;
	evm|liquid|btc|ui) "stop_$what" ;;
	*) die "stop what? all, evm, liquid, btc or ui" ;;
	esac ;;
status)
	say "EVM (Base Sepolia, $EVM_CONTRACT):"; status_evm
	say "Liquid testnet:"; status_liquid
	say "Bitcoin regtest:"; status_btc
	if ui_running; then say "page: http://127.0.0.1:$UI_PORT"; else say "page: down"; fi ;;
cli)
	node=${2:-}; [ -n "$node" ] || die "cli NODE ARGS..."
	shift 2
	case " $EVM_NODES " in *" $node "*) evm_cli "$node" "$@"; exit ;; esac
	case " $BTC_NODES " in *" $node "*) btc_cli "$node" "$@"; exit ;; esac
	case " $LIQUID_NODES " in *" ${node#L} "*) liquid_cli "${node#L}" "$@"; exit ;; esac
	die "no node $node (EVM: $EVM_NODES; Liquid: L+$LIQUID_NODES; Bitcoin: $BTC_NODES)" ;;
*)
	sed -n '2,/^set -euo/p' "$0" | grep '^#' | cut -c3-
	exit 1 ;;
esac
