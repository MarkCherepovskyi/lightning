# Source this (bash or zsh) before the Base Sepolia scripts:
#
#   . contrib/evmchan/testnet-env.sh
#
# It exports, for this shell only and without printing them:
#   INFURA_API_KEY        from the macOS Keychain item "evmchan-infura", stored
#                         once with (it asks for the key; nothing is echoed):
#                           security add-generic-password -U -a "$USER" -s evmchan-infura -w
#   DEPLOYER_PRIVATE_KEY  from $EVM_DIR/deployer.key (default ~/.evmchan-testnet), mode 0600
#   PATH, PYTHONPATH      this tree's .venv and pyln-client/pyln-proto
# A variable that is already set is left alone. Nothing is written to disk.

if [ -n "${ZSH_VERSION:-}" ]; then
	eval '_evmenv_here=${(%):-%x}'
else
	_evmenv_here=${BASH_SOURCE[0]}
fi
_evmenv_root=$(cd "$(dirname "$_evmenv_here")/../.." && pwd)

if [ -z "${INFURA_API_KEY:-}" ]; then
	if INFURA_API_KEY=$(security find-generic-password -a "$USER" -s evmchan-infura -w 2>/dev/null) \
	   && [ -n "$INFURA_API_KEY" ]; then
		export INFURA_API_KEY
	else
		unset INFURA_API_KEY
		echo "testnet-env: no INFURA_API_KEY; store it once with:" >&2
		echo "  security add-generic-password -U -a \"\$USER\" -s evmchan-infura -w" >&2
	fi
fi

_evmenv_key=${EVM_DIR:-$HOME/.evmchan-testnet}/deployer.key
if [ -z "${DEPLOYER_PRIVATE_KEY:-}" ]; then
	if [ -r "$_evmenv_key" ]; then
		DEPLOYER_PRIVATE_KEY=$(tr -d ' \n' < "$_evmenv_key")
		export DEPLOYER_PRIVATE_KEY
	else
		echo "testnet-env: no $_evmenv_key, so no DEPLOYER_PRIVATE_KEY" >&2
	fi
fi

case ":$PATH:" in
*":$_evmenv_root/.venv/bin:"*) ;;
*) export PATH="$_evmenv_root/.venv/bin:$PATH" ;;
esac
case ":${PYTHONPATH:-}:" in
*":$_evmenv_root/contrib/pyln-client:"*) ;;
*) export PYTHONPATH="$_evmenv_root/contrib/pyln-client:$_evmenv_root/contrib/pyln-proto${PYTHONPATH:+:$PYTHONPATH}" ;;
esac

_evmenv_i=missing; [ -n "${INFURA_API_KEY:-}" ] && _evmenv_i=set
_evmenv_d=missing; [ -n "${DEPLOYER_PRIVATE_KEY:-}" ] && _evmenv_d=set
echo "testnet-env: INFURA_API_KEY $_evmenv_i, DEPLOYER_PRIVATE_KEY $_evmenv_d, venv $_evmenv_root/.venv" >&2
unset _evmenv_here _evmenv_root _evmenv_key _evmenv_i _evmenv_d
