#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Starts anvil and deploys UtxoChain to it (SPEC 6), for tests and by hand.
#
#   ./run-anvil.sh [--port 8545] [--interval 2] [--block-time S] [--token T] [-- anvil args...]
#
# Prints the contract's address on stdout (one line; with --token, a second
# line: the token's address), then keeps anvil in the foreground until this
# script is stopped (Ctrl-C, SIGTERM, SIGHUP), which kills anvil too.
# Everything else goes to stderr.
#
#   --port        anvil's port (default 8545)
#   --interval    the contract's INTERVAL, seconds per virtual block (default 2)
#   --block-time  mine a block every S seconds (default: only on transactions,
#                 evm_mine and anvil_mine; tests move time themselves with
#                 evm_setNextBlockTimestamp + evm_mine)
#   --token       token mode (SPEC 4.6): UtxoChainToken instead, for T, an
#                 ERC-20's address (with anvil --fork-url), or mock:D, which
#                 first deploys contracts/evm/test/Tokens.sol's MockERC20 of D
#                 decimals (anyone can mint it)
#   --            the rest goes to anvil as is (e.g. --timestamp, --hardfork)
#
# anvil runs with --slots-in-an-epoch 1, so `safe` trails `latest` by one
# block. The deployer is anvil's first default account; its key is public.
set -euo pipefail

port=8545
interval=2
block_time=()
token=""
extra=()
while [ $# -gt 0 ]; do
    case "$1" in
        --port) port="$2"; shift 2 ;;
        --interval) interval="$2"; shift 2 ;;
        --block-time) block_time=(--block-time "$2"); shift 2 ;;
        --token) token="$2"; shift 2 ;;
        --) shift; extra=("$@"); break ;;
        -h|--help) sed -n '3,26p' "$0"; exit 0 ;;
        *) echo "run-anvil.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
case "$port" in ''|*[!0-9]*) echo "run-anvil.sh: bad --port" >&2; exit 2 ;; esac

here="$(builtin cd "$(dirname "$0")" && pwd)"
contracts="$here/../../contracts/evm"
export PATH="$HOME/.foundry/bin:$PATH"
command -v anvil >/dev/null || { echo "run-anvil.sh: anvil not found (install Foundry)" >&2; exit 1; }
rpc="http://127.0.0.1:$port"

anvil --port "$port" --slots-in-an-epoch 1 --silent ${block_time[@]+"${block_time[@]}"} \
    ${extra[@]+"${extra[@]}"} >&2 &
anvil_pid=$!
cleanup() {
    trap - EXIT INT TERM HUP
    kill "$anvil_pid" 2>/dev/null || true
    wait "$anvil_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

up=""
for _ in $(seq 1 100); do
    kill -0 "$anvil_pid" 2>/dev/null || { echo "run-anvil.sh: anvil exited" >&2; exit 1; }
    if curl -s -m 1 -H 'Content-Type: application/json' \
        --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' "$rpc" | grep -q result; then
        up=1
        break
    fi
    sleep 0.1
done
[ -n "$up" ] || { echo "run-anvil.sh: anvil did not start on port $port" >&2; exit 1; }

# anvil's account 0 (the well-known test mnemonic).
key=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
token_args=()
case "$token" in
    '') ;;
    mock:*)
        decimals="${token#mock:}"
        case "$decimals" in ''|*[!0-9]*) echo "run-anvil.sh: bad --token $token" >&2; exit 2 ;; esac
        out=$(builtin cd "$contracts" && forge create test/Tokens.sol:MockERC20 --rpc-url "$rpc" \
            --private-key "$key" --broadcast --json --constructor-args "$decimals")
        token=$(printf '%s' "$out" | sed -n 's/.*"deployedTo": *"\(0x[0-9a-fA-F]\{40\}\)".*/\1/p')
        [ -n "$token" ] || { echo "run-anvil.sh: no token address in forge's output" >&2; exit 1; }
        token_args=(--token "$token") ;;
    *) token_args=(--token "$token") ;;
esac
address=$(DEPLOYER_PRIVATE_KEY=$key \
    "$contracts/deploy.sh" --rpc-url "$rpc" --interval "$interval" ${token_args[@]+"${token_args[@]}"})
echo "$address"
if [ -n "$token" ]; then
    echo "$token"
    echo "run-anvil.sh: anvil on $rpc (pid $anvil_pid), UtxoChainToken at $address for token $token," \
        "INTERVAL $interval" >&2
else
    echo "run-anvil.sh: anvil on $rpc (pid $anvil_pid), UtxoChain at $address, INTERVAL $interval" >&2
fi

# `wait` returns early when a trapped signal arrives, then the trap exits.
wait "$anvil_pid"
