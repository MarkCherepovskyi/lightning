#!/bin/sh
# Run assetswap-plugin's Liquid <-> EVM swap tests in Docker, against the
# source tree this script is in: elementsd and anvil side by side, CLN,
# the UtxoChain contract and the two Rust plugins built in a Docker volume
# (one per checkout, kept between runs so rebuilds are incremental).
#
#   contrib/liquid-poc/asset-swap/run-tests.sh            # the swap tests
#   contrib/liquid-poc/asset-swap/run-tests.sh -k refund  # ... some of them
#   contrib/liquid-poc/asset-swap/run-tests.sh logs DIR   # last run's test dirs to DIR
#   contrib/liquid-poc/asset-swap/run-tests.sh clean      # remove the build volume
#
# Arguments go to pytest.  Without a test file it runs
# plugins/assetswap-plugin/tests/test_liquid_evm_swap.py, and without -n it
# uses -n 2.  The tree is copied in as git sees it: tracked and untracked
# files, but not ignored ones.
#
# Environment: TEST_DEBUG=1 prints the nodes' logs of failed tests;
# ASSETSWAP_TEST_KEEP_ALL=1 keeps the directories of passed tests too;
# ASSETSWAP_TEST_REBUILD=1 rebuilds the image (and the two it is made of).
set -e

here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)
image=${ASSETSWAP_TEST_IMAGE:-assetswap/pytest}
volume=${ASSETSWAP_TEST_VOLUME:-assetswap-pytest-$(printf '%s' "$root" | cksum | cut -d' ' -f1)}

case "$1" in
clean)
	docker volume rm "$volume"
	exit 0
	;;
logs)
	if [ -z "$2" ]; then
		echo "usage: $0 logs DIR" >&2
		exit 1
	fi
	mkdir -p "$2"
	docker run --rm -v "$volume":/build "$image" tar -C /build/test-logs -cf - . \
		| tar -C "$2" -xf -
	echo "test directories of the last run are in $2"
	exit 0
	;;
esac

if [ -n "$ASSETSWAP_TEST_REBUILD" ] || ! docker image inspect "$image" > /dev/null 2>&1; then
	for part in assetchan evmchan; do
		if [ -n "$ASSETSWAP_TEST_REBUILD" ] || ! docker image inspect "$part/pytest" > /dev/null 2>&1; then
			docker build --target pytest -t "$part/pytest" -f "$root/contrib/$part/Dockerfile" "$root/contrib/$part"
		fi
	done
	docker build --target pytest -t "$image" -f "$here/Dockerfile" "$here"
fi

has_tests=no
has_n=no
for arg in "$@"; do
	case "$arg" in
	tests/*|plugins/*|*.py|*.py::*) has_tests=yes ;;
	-n|-n*|--numprocesses*) has_n=yes ;;
	esac
done
[ $has_n = yes ] || set -- -n 2 "$@"
[ $has_tests = yes ] || set -- "$@" plugins/assetswap-plugin/tests/test_liquid_evm_swap.py

list=$(mktemp "${TMPDIR:-/tmp}/assetswap-files.XXXXXX")
trap 'rm -f "$list"' EXIT
cd "$root"
{ git ls-files --recurse-submodules; git ls-files --others --exclude-standard; } \
	| while IFS= read -r f; do [ -e "$f" ] && printf '%s\n' "$f"; done > "$list"

# macOS's tar would add extended attributes and ._ files.
tarflags=
if tar --version 2> /dev/null | grep -q bsdtar; then
	tarflags="--no-xattrs --no-mac-metadata"
fi

# shellcheck disable=SC2086
COPYFILE_DISABLE=1 tar $tarflags -cf - -T "$list" \
	| docker run --rm -i --name "assetswap-pytest-$$" -v "$volume":/build \
		-e TEST_DEBUG="${TEST_DEBUG:-}" -e KEEP_ALL="${ASSETSWAP_TEST_KEEP_ALL:-}" \
		-e TEST_NETWORK=liquid-regtest \
		"$image" sh -c '
set -e
tar -xf - -C /build 2> /dev/null
cd /build
if [ ! -f config.vars ]; then
	echo "configuring ..."
	PYTHON=python3 ./configure --disable-rust --disable-valgrind > /tmp/configure.log 2>&1 \
		|| { cat /tmp/configure.log; exit 1; }
fi
start=$(date +%s)
echo "building CLN ..."
make -j"$(nproc)" VERSION=v26.06-assetswap all-programs default-targets > /tmp/make.log 2>&1 \
	|| { grep -E "error|Error" /tmp/make.log | head -40; tail -20 /tmp/make.log; exit 1; }
echo "building the contract ..."
(cd contracts/evm && forge build --offline --skip test --skip script > /tmp/forge.log 2>&1) \
	|| { cat /tmp/forge.log; exit 1; }
echo "building the plugins ..."
# Crates are fetched once into the volume.
CARGO_HOME=/build/.cargo-home cargo build -q -p cln-assetswap -p cln-pricetiers > /tmp/cargo.log 2>&1 \
	|| { tail -40 /tmp/cargo.log; exit 1; }
echo "built in $(( $(date +%s) - start )) s"

export PYTHONPATH=/build/contrib/pyln-testing:/build/contrib/pyln-client:/build/contrib/pyln-proto:/build/contrib/pyln-spec/bolt7:/build/contrib/pyln-grpc-proto
export TEST_DIR=/tmp/ltests SLOW_MACHINE=1
mkdir -p /tmp/ltests
plugins=
if [ -n "$KEEP_ALL" ]; then
	# pyln-testing removes the directories of passed tests.
	echo "import shutil; shutil.rmtree = lambda *a, **k: None" > /tmp/keepdirs.py
	export PYTHONPATH=/tmp:$PYTHONPATH
	plugins="-p keepdirs"
fi
rc=0
# --confcutdir: not the repository root'"'"'s conftest.py (CI reporting, needs .git).
pytest -p no:cacheprovider -p no:trackflaky --confcutdir=plugins/assetswap-plugin $plugins \
	--timeout=900 -rfEX "$@" || rc=$?
rm -rf /build/test-logs
mkdir -p /build/test-logs
cp -r /tmp/ltests/. /build/test-logs/
exit $rc
' sh "$@"
