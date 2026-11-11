#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Deploys UtxoChain (SPEC 4), or with --token UtxoChainToken (SPEC 4.6),
# with `forge create` and prints its address.
#
#   DEPLOYER_PRIVATE_KEY=0x... ./deploy.sh --rpc-url <url> --interval <seconds> \
#       [--token <address> [--units-per-sat N]]
#
#   --rpc-url        JSON-RPC endpoint (or $RPC_URL). A literal ${INFURA_API_KEY}
#                    in it is replaced from the environment; the URL is not printed.
#   --interval       seconds per virtual block: 2 on anvil, 60 on Base Sepolia.
#   --token          an ERC-20's address: deploys UtxoChainToken for it (one
#                    instance per token) instead of the ETH UtxoChain.
#   --units-per-sat  token units per sat. Default: from the token's decimals(),
#                    10^(decimals - 8), or 1 below 8 decimals; the constructor
#                    refuses any other value when the token has decimals().
#
# The libraries (BtcTx, Bip143, TaprootSighash, Ecdsa, Schnorr, Secp256k1,
# Templates, Validation) are all internal, so they are compiled into
# UtxoChain's own bytecode (under the 24,576-byte limit; `forge build --sizes`)
# and there is nothing to deploy or link separately.
set -euo pipefail

rpc_url="${RPC_URL:-}"
interval=""
token=""
units=""
while [ $# -gt 0 ]; do
    case "$1" in
        --rpc-url) rpc_url="$2"; shift 2 ;;
        --interval) interval="$2"; shift 2 ;;
        --token) token="$2"; shift 2 ;;
        --units-per-sat) units="$2"; shift 2 ;;
        -h|--help) sed -n '3,25p' "$0"; exit 0 ;;
        *) echo "deploy.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$rpc_url" ] || { echo "deploy.sh: --rpc-url (or RPC_URL) is required" >&2; exit 2; }
case "$interval" in
    ''|*[!0-9]*|0) echo "deploy.sh: --interval must be a positive integer" >&2; exit 2 ;;
esac
if [ -n "$token" ]; then
    [[ "$token" =~ ^0x[0-9a-fA-F]{40}$ ]] || { echo "deploy.sh: --token must be a 20-byte hex address" >&2; exit 2; }
fi
case "$units" in
    '') ;;
    *[!0-9]*|0) echo "deploy.sh: --units-per-sat must be a positive integer" >&2; exit 2 ;;
esac
[ -z "$units" ] || [ -n "$token" ] || { echo "deploy.sh: --units-per-sat needs --token" >&2; exit 2; }
[ -n "${DEPLOYER_PRIVATE_KEY:-}" ] || { echo "deploy.sh: DEPLOYER_PRIVATE_KEY is not set" >&2; exit 2; }
if [[ "$rpc_url" == *'${INFURA_API_KEY}'* ]]; then
    [ -n "${INFURA_API_KEY:-}" ] || { echo "deploy.sh: INFURA_API_KEY is not set" >&2; exit 2; }
    rpc_url="${rpc_url//'${INFURA_API_KEY}'/$INFURA_API_KEY}"
fi

builtin cd "$(dirname "$0")"
if [ -z "$token" ]; then
    contract=src/UtxoChain.sol:UtxoChain
    args=("$interval")
else
    if [ -z "$units" ]; then
        decimals=$(cast call "$token" 'decimals()(uint8)' --rpc-url "$rpc_url") ||
            { echo "deploy.sh: the token has no decimals(): give --units-per-sat" >&2; exit 1; }
        decimals="${decimals%% *}"
        case "$decimals" in ''|*[!0-9]*) echo "deploy.sh: bad decimals() from the token" >&2; exit 1 ;; esac
        [ "$decimals" -le 26 ] || { echo "deploy.sh: $decimals decimals: above 10^18 units per sat" >&2; exit 1; }
        units=1
        if [ "$decimals" -gt 8 ]; then units=$(( 10 ** (decimals - 8) )); fi
    fi
    contract=src/UtxoChainToken.sol:UtxoChainToken
    args=("$interval" "$token" "$units")
    echo "deploy.sh: UtxoChainToken for $token, $units units per sat" >&2
fi
out=$(forge create "$contract" \
    --rpc-url "$rpc_url" \
    --private-key "$DEPLOYER_PRIVATE_KEY" \
    --broadcast --json \
    --constructor-args "${args[@]}")
address=$(printf '%s' "$out" | sed -n 's/.*"deployedTo": *"\(0x[0-9a-fA-F]\{40\}\)".*/\1/p')
[ -n "$address" ] || { echo "deploy.sh: no address in forge's output" >&2; exit 1; }
echo "$address"
