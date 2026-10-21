#!/bin/sh
# Run CLN's pytest suite on Liquid regtest (TEST_NETWORK=liquid-regtest) in
# Docker, against the source tree this script is in.  Works on macOS too: the
# tree is built for Linux in a Docker volume, one per checkout, kept between
# runs so rebuilds are incremental (the first build takes a while).
#
#   contrib/liquid-poc/run-tests.sh                   # the Liquid asset tests
#   contrib/liquid-poc/run-tests.sh -k penalty        # ... some of them
#   contrib/liquid-poc/run-tests.sh tests/test_closing.py -n 8
#   contrib/liquid-poc/run-tests.sh logs DIR          # last run's test dirs to DIR
#   contrib/liquid-poc/run-tests.sh clean             # remove the build volume
#
# Arguments go to pytest.  Without a test file it runs
# tests/test_liquid_asset_poc.py and tests/test_liquid_asset_gossip.py, and
# without -n it uses -n 4.  The tree is copied in as git sees it: tracked and
# untracked files, but not ignored ones.
#
# Environment: TEST_DEBUG=1 prints the nodes' logs of failed tests;
# LNPOC_TEST_KEEP_ALL=1 keeps the directories of passed tests too (else only
# failed ones are kept for `logs`); LNPOC_TEST_REBUILD=1 rebuilds the image;
# LNPOC_TEST_IMAGE and LNPOC_TEST_VOLUME override their names.
set -e

here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)
image=${LNPOC_TEST_IMAGE:-liquid-ln-poc/pytest}
volume=${LNPOC_TEST_VOLUME:-lnpoc-pytest-$(printf '%s' "$root" | cksum | cut -d' ' -f1)}

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

if [ -n "$LNPOC_TEST_REBUILD" ] || ! docker image inspect "$image" > /dev/null 2>&1; then
	docker build --target pytest -t "$image" -f "$here/Dockerfile" "$here"
fi

# Default test files and parallelism.
has_tests=no
has_n=no
for arg in "$@"; do
	case "$arg" in
	tests/*|*.py|*.py::*) has_tests=yes ;;
	-n|-n*|--numprocesses*) has_n=yes ;;
	esac
done
[ $has_n = yes ] || set -- -n 4 "$@"
[ $has_tests = yes ] || set -- "$@" tests/test_liquid_asset_poc.py tests/test_liquid_asset_gossip.py

list=$(mktemp "${TMPDIR:-/tmp}/lnpoc-files.XXXXXX")
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
	| docker run --rm -i --name "lnpoc-pytest-$$" -v "$volume":/build \
		-e TEST_DEBUG="${TEST_DEBUG:-}" -e KEEP_ALL="${LNPOC_TEST_KEEP_ALL:-}" \
		"$image" sh -c '
set -e
tar -xf - -C /build 2> /dev/null
cd /build
if [ ! -f config.vars ]; then
	echo "configuring ..."
	PYTHON=python3 ./configure --disable-rust --disable-valgrind > /tmp/configure.log 2>&1 \
		|| { cat /tmp/configure.log; exit 1; }
fi
echo "building ..."
make -j"$(nproc)" VERSION=v26.09-liquid all-programs default-targets > /tmp/make.log 2>&1 \
	|| { grep -E "error|Error" /tmp/make.log | head -40; tail -20 /tmp/make.log; exit 1; }

export PYTHONPATH=/build/contrib/pyln-testing:/build/contrib/pyln-client:/build/contrib/pyln-proto:/build/contrib/pyln-spec/bolt7:/build/contrib/pyln-grpc-proto
export TEST_NETWORK=liquid-regtest TEST_DIR=/tmp/ltests SLOW_MACHINE=1
mkdir -p /tmp/ltests
plugins=
if [ -n "$KEEP_ALL" ]; then
	# pyln-testing removes the directories of passed tests.
	echo "import shutil; shutil.rmtree = lambda *a, **k: None" > /tmp/keepdirs.py
	export PYTHONPATH=/tmp:$PYTHONPATH
	plugins="-p keepdirs"
fi
rc=0
pytest -p no:cacheprovider $plugins --timeout=900 "$@" || rc=$?
rm -rf /build/test-logs
mkdir -p /build/test-logs
cp -r /tmp/ltests/. /build/test-logs/
exit $rc
' sh "$@"
