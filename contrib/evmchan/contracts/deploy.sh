#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Deploys UtxoChain (SPEC 4) with `forge create` and prints its address.
#
#   DEPLOYER_PRIVATE_KEY=0x... ./deploy.sh --rpc-url <url> --interval <seconds>
#
#   --rpc-url    JSON-RPC endpoint (or $RPC_URL). A literal ${INFURA_API_KEY}
#                in it is replaced from the environment; the URL is not printed.
#   --interval   seconds per virtual block: 2 on anvil, 60 on Base Sepolia.
#
# The libraries (BtcTx, Bip143, TaprootSighash, Ecdsa, Schnorr, Secp256k1,
# Templates, Validation) are all internal, so they are compiled into
# UtxoChain's own bytecode (under the 24,576-byte limit; `forge build --sizes`)
# and there is nothing to deploy or link separately.
set -euo pipefail

rpc_url="${RPC_URL:-}"
interval=""
while [ $# -gt 0 ]; do
    case "$1" in
        --rpc-url) rpc_url="$2"; shift 2 ;;
        --interval) interval="$2"; shift 2 ;;
        -h|--help) sed -n '3,17p' "$0"; exit 0 ;;
        *) echo "deploy.sh: unknown argument $1" >&2; exit 2 ;;
    esac
done
[ -n "$rpc_url" ] || { echo "deploy.sh: --rpc-url (or RPC_URL) is required" >&2; exit 2; }
case "$interval" in
    ''|*[!0-9]*|0) echo "deploy.sh: --interval must be a positive integer" >&2; exit 2 ;;
esac
[ -n "${DEPLOYER_PRIVATE_KEY:-}" ] || { echo "deploy.sh: DEPLOYER_PRIVATE_KEY is not set" >&2; exit 2; }
if [[ "$rpc_url" == *'${INFURA_API_KEY}'* ]]; then
    [ -n "${INFURA_API_KEY:-}" ] || { echo "deploy.sh: INFURA_API_KEY is not set" >&2; exit 2; }
    rpc_url="${rpc_url//'${INFURA_API_KEY}'/$INFURA_API_KEY}"
fi

builtin cd "$(dirname "$0")"
out=$(forge create src/UtxoChain.sol:UtxoChain \
    --rpc-url "$rpc_url" \
    --private-key "$DEPLOYER_PRIVATE_KEY" \
    --broadcast --json \
    --constructor-args "$interval")
address=$(printf '%s' "$out" | sed -n 's/.*"deployedTo": *"\(0x[0-9a-fA-F]\{40\}\)".*/\1/p')
[ -n "$address" ] || { echo "deploy.sh: no address in forge's output" >&2; exit 1; }
echo "$address"
