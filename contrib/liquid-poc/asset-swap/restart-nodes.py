#!/usr/bin/env python3
"""Restart running lightningd nodes on this tree's lightningd, with the
options they run with now (plugins, ports and all), each detached in its own
session, one at a time.

    contrib/liquid-poc/asset-swap/restart-nodes.py ~/.assetchan-testnet/D
    contrib/liquid-poc/asset-swap/restart-nodes.py --stagger 10 ~/.evmchan-testnet/{P,Q,R,S,T,U}

Why: a node whose lightningd build was deleted from disk keeps running, but
can't start a channel daemon when a peer reconnects ("subd lightning_channeld
failed: No such file or directory"); and before this branch's fix in
lightningd/plugin_hook.c, a node can't take a new htlc_accepted plugin once a
hook has answered "fail" or "resolve". A restart on this build fixes both.
Restart a node's peers too if they share the problem: the reconnect needs
both sides. Each node's options are copied from its running command line,
so nothing secret in them is printed or stored. cln-grpc is disabled
(a Rust build runs it on one fixed port for every node).
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
LIGHTNINGD = os.path.join(ROOT, 'lightningd', 'lightningd')
CLI = os.path.join(ROOT, 'cli', 'lightning-cli')


def pids_of(ldir):
    out = subprocess.run(['pgrep', '-f', '--', '--lightning-dir={}( |$)'.format(ldir)],
                         capture_output=True, text=True).stdout.split()
    return [p for p in out if p and p != str(os.getpid())]


def argv_of(pid):
    # (ps -ww: the whole command line, never truncated)
    return subprocess.run(['ps', '-ww', '-o', 'command=', '-p', pid], capture_output=True, text=True).stdout.split()


def cli(args, *cmd):
    keep = [a for a in args if a.startswith(('--network=', '--lightning-dir='))]
    return subprocess.run([CLI] + keep + list(cmd), capture_output=True, text=True)


def restart(ldir, timeout):
    pids = pids_of(ldir)
    if len(pids) != 1:
        sys.exit('{}: expected one running lightningd, found {}'.format(ldir, pids or 'none'))
    argv = argv_of(pids[0])
    if not argv or not argv[0].endswith('lightningd'):
        sys.exit('{}: not a lightningd command line'.format(ldir))
    args = argv[1:]
    if '--disable-plugin=cln-grpc' not in args:
        args.append('--disable-plugin=cln-grpc')
    if '--daemon' not in args:
        args.append('--daemon')
    cli(args, 'stop')
    for _ in range(timeout):
        if not pids_of(ldir):
            break
        time.sleep(1)
    else:
        sys.exit('{}: did not stop'.format(ldir))
    env = dict(os.environ)
    env['PATH'] = os.path.join(ROOT, '.venv', 'bin') + os.pathsep + env.get('PATH', '')
    env['PYTHONPATH'] = os.pathsep.join(os.path.join(ROOT, 'contrib', p) for p in ('pyln-client', 'pyln-proto'))
    subprocess.Popen([LIGHTNINGD] + args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True).wait(timeout=timeout)
    for _ in range(timeout):
        r = cli(args, 'getinfo')
        if r.returncode == 0:
            info = json.loads(r.stdout)
            print('{}: up, {} ({})'.format(ldir, info.get('alias'), info.get('version')))
            return
        time.sleep(1)
    sys.exit('{}: did not come back (see its log)'.format(ldir))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('dirs', nargs='+', help='lightning-dir of each node, as it was started with')
    ap.add_argument('--stagger', type=int, default=0, help='seconds between nodes (EVM nodes: 10)')
    ap.add_argument('--timeout', type=int, default=120)
    a = ap.parse_args()
    for i, d in enumerate(a.dirs):
        if i and a.stagger:
            time.sleep(a.stagger)
        restart(os.path.abspath(os.path.expanduser(d)).rstrip('/'), a.timeout)


if __name__ == '__main__':
    main()
