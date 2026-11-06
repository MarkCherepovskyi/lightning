#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Run the EVM channel tests on Linux in Docker, against the source tree this
# script is in: the plugin's unit tests (contrib/evmchan/tests) and CLN's
# integration tests on anvil (tests/test_evmchan.py). Works on macOS too: the
# tree is built for Linux (CLN, and the contract with forge) in a Docker
# volume, one per checkout, kept between runs so rebuilds are incremental
# (the first build takes a while).
#
#   contrib/evmchan/run-tests.sh                    # unit tests, then integration tests
#   contrib/evmchan/run-tests.sh -k htlc            # ... some integration tests
#   contrib/evmchan/run-tests.sh tests/test_evmchan.py::test_penalty -n 1
#   contrib/evmchan/run-tests.sh logs DIR           # last run's test dirs to DIR
#   contrib/evmchan/run-tests.sh clean              # remove the build volume
#
# Arguments go to the integration run's pytest. Without a test file it runs
# tests/test_evmchan.py, and without -n it uses -n 4. The tree is copied in
# without what .gitignore files exclude (build output, generated files) and
# without Mach-O binaries; it does not need git.
#
# Environment: EVMCHAN_TEST_UNIT=0 skips the unit tests; TEST_DEBUG=1 prints
# the nodes' logs of failed tests; EVMCHAN_TEST_KEEP_ALL=1 keeps the
# directories of passed tests too (else only failed ones are kept for
# `logs`); EVMCHAN_TEST_REBUILD=1 rebuilds the image; EVMCHAN_TEST_IMAGE and
# EVMCHAN_TEST_VOLUME override their names (evmchan/pytest, and
# evmchan-pytest-<checksum of the tree's path>).
set -e

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
image=${EVMCHAN_TEST_IMAGE:-evmchan/pytest}
volume=${EVMCHAN_TEST_VOLUME:-evmchan-pytest-$(printf '%s' "$root" | cksum | cut -d' ' -f1)}

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

if [ -n "$EVMCHAN_TEST_REBUILD" ] || ! docker image inspect "$image" > /dev/null 2>&1; then
	docker build --target pytest -t "$image" -f "$here/Dockerfile" "$here"
fi

# Default test file and parallelism. Options and their values (as in
# `--deselect tests/x.py::t`) are not test paths.
has_tests=no
has_n=no
value=no
for arg in "$@"; do
	if [ $value = yes ]; then
		value=no
		continue
	fi
	case "$arg" in
	-n|--numprocesses) has_n=yes value=yes ;;
	-n*|--numprocesses=*) has_n=yes ;;
	--deselect|--ignore|--ignore-glob|-k|-m|-p|-o|-c|-W|--timeout|--maxfail|--durations|--rootdir|\
	--basetemp|--confcutdir|--junitxml|--junit-xml|--log-file|--log-level|--override-ini|--tb|-r) value=yes ;;
	-*) ;;
	tests/*|*.py|*.py::*) has_tests=yes ;;
	esac
done
[ $has_n = yes ] || set -- -n 4 "$@"
[ $has_tests = yes ] || set -- "$@" tests/test_evmchan.py

list=$(mktemp "${TMPDIR:-/tmp}/evmchan-files.XXXXXX")
trap 'rm -f "$list"' EXIT

# The files to copy: every file under the tree but .git, what the
# .gitignore files (at any depth, git's rules: last match wins, `!`
# negates, a pattern with a slash is anchored, an ignored directory is not
# entered, a submodule has only its own) exclude, and Mach-O binaries (a
# macOS build in the tree).
python3 - "$root" > "$list" << 'EOF'
import os
import re
import sys

root = sys.argv[1]


def glob_re(pat):
    out, i = '', 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith('**/', i):
            out, i = out + '(?:.*/)?', i + 3
        elif pat.startswith('/**', i) and i + 3 == len(pat):
            out, i = out + '/.*', i + 3
        elif pat.startswith('**', i):
            out, i = out + '.*', i + 2
        elif c == '*':
            out, i = out + '[^/]*', i + 1
        elif c == '?':
            out, i = out + '[^/]', i + 1
        elif c == '[':
            j = pat.find(']', i + 1)
            if j < 0:
                out, i = out + re.escape(c), i + 1
            else:
                body = pat[i + 1:j].replace('\\', '\\\\')
                if body.startswith('!'):
                    body = '^' + body[1:]
                out, i = out + '[' + body + ']', j + 1
        elif c == '\\' and i + 1 < len(pat):
            out, i = out + re.escape(pat[i + 1]), i + 2
        else:
            out, i = out + re.escape(c), i + 1
    return re.compile(out + r'\Z')


def read_rules(d):
    rules = []
    try:
        with open(os.path.join(d, '.gitignore'), errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return rules
    for line in lines:
        line = line.rstrip()
        if not line or line.startswith('#'):
            continue
        neg = line.startswith('!')
        if neg:
            line = line[1:]
        dir_only = line.endswith('/')
        line = line.rstrip('/')
        if not line:
            continue
        anchored = '/' in line
        rules.append((d, glob_re(line.lstrip('/')), neg, dir_only, anchored))
    return rules


def verdict(path, is_dir, rules):
    """None if no rule matches, else True (ignored) or False (re-included)."""
    v = None
    for base, rx, neg, dir_only, anchored in rules:
        if dir_only and not is_dir:
            continue
        rel = os.path.relpath(path, base)
        if rx.match(rel if anchored else os.path.basename(path)):
            v = not neg
    return v


def ignored(path, is_dir, rules):
    return verdict(path, is_dir, rules) is True


def macho(path):
    try:
        with open(path, 'rb') as f:
            magic = f.read(4)
    except OSError:
        return True
    return magic in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xfe\xed\xfa\xcf')


def walk(d, rules, excluded=False):
    """Inside an ignored directory (excluded) only files a `!` pattern names
    are taken: some repositories track such files (git add -f)."""
    if d != root and os.path.exists(os.path.join(d, '.git')):
        rules = []           # a submodule: the superproject's rules stop here
    rules = rules + read_rules(d)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return
    for name in names:
        if name == '.git':
            continue
        p = os.path.join(d, name)
        if os.path.isdir(p) and not os.path.islink(p):
            walk(p, rules, excluded or ignored(p, True, rules))
            continue
        v = verdict(p, False, rules)
        take = v is False if excluded else v is not True
        if take and (os.path.islink(p) or (os.path.isfile(p) and not macho(p))):
            print(os.path.relpath(p, root))


walk(root, [])
EOF

# macOS's tar would add extended attributes and ._ files.
tarflags=
if tar --version 2> /dev/null | grep -q bsdtar; then
	tarflags="--no-xattrs --no-mac-metadata"
fi

# shellcheck disable=SC2086
(cd "$root" && COPYFILE_DISABLE=1 tar $tarflags -cf - -T "$list") \
	| docker run --rm -i --name "evmchan-pytest-$$" -v "$volume":/build \
		-e TEST_DEBUG="${TEST_DEBUG:-}" -e KEEP_ALL="${EVMCHAN_TEST_KEEP_ALL:-}" \
		-e UNIT="${EVMCHAN_TEST_UNIT:-1}" \
		"$image" sh -c '
set -e
# The tree replaces the copy of the last run: files it no longer has are
# removed (build output, which it never has, stays).
tar -xf - -C /build --no-same-owner --quoting-style=literal --index-file=/tmp/files.new -v
sort -o /tmp/files.new /tmp/files.new
if [ -f /build/.evmchan-files ]; then
	comm -23 /build/.evmchan-files /tmp/files.new | while IFS= read -r f; do
		rm -f "/build/$f"
	done
fi
mv /tmp/files.new /build/.evmchan-files
cd /build
if [ ! -f config.vars ]; then
	echo "configuring ..."
	PYTHON=python3 ./configure --disable-rust --disable-valgrind > /tmp/configure.log 2>&1 \
		|| { cat /tmp/configure.log; exit 1; }
fi
echo "building CLN ..."
start=$(date +%s)
make -j"$(nproc)" VERSION=v26.06-evmchan all-programs default-targets > /tmp/make.log 2>&1 \
	|| { grep -E "error|Error" /tmp/make.log | head -40; tail -20 /tmp/make.log; exit 1; }
echo "building the contract ..."
(cd contrib/evmchan/contracts && forge build --offline --skip test --skip script > /tmp/forge.log 2>&1) \
	|| { cat /tmp/forge.log; exit 1; }
echo "built in $(( $(date +%s) - start )) s"

export PYTHONPATH=/build/contrib/pyln-testing:/build/contrib/pyln-client:/build/contrib/pyln-proto:/build/contrib/pyln-spec/bolt7:/build/contrib/pyln-grpc-proto
export TEST_DIR=/tmp/ltests
mkdir -p /tmp/ltests
rc=0
if [ "$UNIT" != 0 ]; then
	echo "unit tests (contrib/evmchan/tests) ..."
	(cd contrib/evmchan && pytest -q -p no:cacheprovider --timeout=600 tests) || rc=$?
fi
plugins=
if [ -n "$KEEP_ALL" ]; then
	# pyln-testing removes the directories of passed tests.
	echo "import shutil; shutil.rmtree = lambda *a, **k: None" > /tmp/keepdirs.py
	export PYTHONPATH=/tmp:$PYTHONPATH
	plugins="-p keepdirs"
fi
echo "integration tests ..."
pytest -p no:cacheprovider -p no:trackflaky $plugins --timeout=900 -rfEX --durations=0 "$@" || rc=$?
rm -rf /build/test-logs
mkdir -p /build/test-logs
cp -r /tmp/ltests/. /build/test-logs/
exit $rc
' sh "$@"
