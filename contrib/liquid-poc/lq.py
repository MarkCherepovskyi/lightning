#!/usr/bin/env python3
"""lq: a small CLI for the Liquid asset-channel nodes (alice, bob, carol, dave).

Talks to the docker stack of docker-compose.testnet.yml (real Liquid testnet)
by default; LNPOC_NET=regtest uses the regtest stack instead.  Amounts are in
base units (asset units, or sats for L-BTC).

    lq status                              block height, wallets, channels
    lq address NODE|elements               a new address of a node (or of the elementsd wallet)
    lq faucet                              testnet faucet L-BTC to the elementsd wallet
    lq issue AMOUNT                        issue a new asset (whole units) and make it the default
    lq fund NODE SATS                      send L-BTC to a node's wallet (it pays on-chain fees)
    lq open FROM TO UNITS [--lbtc]         open a channel (asset by default; waits for it to be ready)
    lq channels                            every channel
    lq invoice NODE UNITS                  create an invoice
    lq pay FROM TO UNITS                   pay along our channels
    lq close FROM TO [--force]             cooperative close; --force: peer offline, unilateral
    lq send NODE ADDRESS UNITS [--lbtc]    spend a node's coins on-chain (asset: withdrawasset)

Opening a channel or closing one waits for blocks: about a minute each on
the testnet, so an open takes around ten minutes.
"""
import argparse
import os
import re
import sys

os.environ.setdefault('LNPOC_NET', 'testnet')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lnpoc as L  # noqa: E402

NODES = L.NODES
ns = argparse.Namespace


def asset_of(args):
    """The asset an operation is in: None means L-BTC."""
    return None if getattr(args, 'lbtc', False) else (getattr(args, 'asset', None) or L.default_asset(args))


def node_funds(node):
    """(L-BTC sats in the wallet, {asset: units in the wallet})."""
    lbtc = sum(o['amount_msat'] for o in L.ln(node, 'listfunds')['outputs']
               if o['status'] == 'confirmed') // 1000
    assets = {}
    res = L.ln(node, 'listassetfunds', check=False)
    lbtc_id = L.lbtc_asset()
    for o in res['outputs'] if isinstance(res, dict) else []:
        # (skip L-BTC that an old build recorded as an asset)
        if o['status'] == 'confirmed' and o['asset'] != lbtc_id:
            assets[o['asset']] = assets.get(o['asset'], 0) + o['amount']
    return lbtc, assets


def cmd_status(args):
    info = L.ecli('getblockchaininfo', wallet=False)
    print(f"chain {info['chain']}, block {info['blocks']} of {info['headers']}"
          f"{' (syncing)' if info.get('initialblockdownload') else ''}")
    try:
        bal = L.ecli('getbalances')['mine']['trusted']
        print("elementsd wallet:", ', '.join(
            f"{L.asset_label(None if k == 'bitcoin' else k)} {int(v * L.COIN)}" for k, v in bal.items()) or '-')
    except L.Fail:
        print("elementsd wallet: (not loaded; run `lq faucet` or the setup first)")
    for node in NODES:
        try:
            lbtc, assets = node_funds(node)
        except L.Fail as e:
            print(f"{node:6} not reachable")
            continue
        extra = ''.join(f", {L.asset_label(a)} {u}" for a, u in assets.items())
        print(f"{node:6} wallet: L-BTC {lbtc}{extra}")
    print()
    L.cmd_channels(args)


def cmd_address(args):
    if args.node == 'elements':
        print(L.unconfidential_address())
    else:
        print(L.ln(args.node, 'newaddr', addresstype='bech32')['bech32'])


def cmd_faucet(args):
    state = L.load_state()
    addr = state.get('address') or L.unconfidential_address()
    state['address'] = addr
    L.save_state(state)
    text = L.faucet(addr, 'lbtc')
    m = re.search(r'Sent \d+ sats.*?transaction [0-9a-f]{64}', text)
    print(m.group(0) if m else text[:300])


def cmd_issue(args):
    issued = L.ecli('issueasset', args.amount, 0, False)
    state = L.load_state()
    state['asset'] = issued['asset']
    L.save_state(state)
    print(f"issued {args.amount} of asset {issued['asset']} (now the default asset)")


def cmd_fund(args):
    addr = L.ln(args.node, 'newaddr', addresstype='bech32')['bech32']
    print(f"sent {args.sats} sat to {args.node}: {L.ecli('sendtoaddress', addr, L.coins(args.sats))}")


def cmd_open(args):
    if args.lbtc:
        L.cmd_open_lbtc(ns(frm=args.frm, to=args.to, amount=args.units, announce=args.announce))
    else:
        L.cmd_open_asset(ns(frm=args.frm, to=args.to, units=args.units, fee_reserve=args.fee_reserve,
                            change=args.change, asset=args.asset, lbtc=False, announce=args.announce))


def cmd_invoice(args):
    L.cmd_invoice(ns(node=args.node, amount_msat=args.units * 1000, label=None,
                     asset=args.asset, lbtc=args.lbtc))


def cmd_pay(args):
    L.cmd_pay(ns(frm=args.frm, to=args.to, amount_msat=args.units * 1000, asset=args.asset, lbtc=args.lbtc))


def cmd_close(args):
    if args.force:
        L.cmd_force_close(ns(frm=args.frm, to=args.to, asset=args.asset, lbtc=args.lbtc, to_elements=False))
    else:
        L.cmd_close(ns(frm=args.frm, to=args.to, asset=args.asset, lbtc=args.lbtc))


def cmd_send(args):
    if args.lbtc:
        res = L.ln(args.node, 'withdraw', destination=args.address, satoshi=args.units)
    else:
        res = L.ln(args.node, 'withdrawasset', destination=args.address,
                   asset=asset_of(args), amount=args.units)
    print(f"sent {args.units} from {args.node} to {args.address}: txid {res['txid']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)

    def add(name, fn, assetopts=False):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        if assetopts:
            sp.add_argument('--asset', help='asset id (default: the one `lq issue` / setup made)')
            sp.add_argument('--lbtc', action='store_true', help='use L-BTC instead of the asset')
        return sp

    add('status', cmd_status)
    sp = add('address', cmd_address)
    sp.add_argument('node', choices=NODES + ['elements'])
    add('faucet', cmd_faucet)
    sp = add('issue', cmd_issue)
    sp.add_argument('amount', type=int, help='whole units')
    sp = add('fund', cmd_fund)
    sp.add_argument('node', choices=NODES)
    sp.add_argument('sats', type=int)
    sp = add('open', cmd_open, True)
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('units', type=int)
    sp.add_argument('--announce', action='store_true')
    sp.add_argument('--fee-reserve', type=int, default=6000, help='L-BTC sats kept for fees (asset channels)')
    sp.add_argument('--change', type=int, default=2000, help='L-BTC sats of the funding input back to elementsd')
    add('channels', L.cmd_channels)
    sp = add('invoice', cmd_invoice, True)
    sp.add_argument('node', choices=NODES)
    sp.add_argument('units', type=int)
    sp = add('pay', cmd_pay, True)
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('units', type=int)
    sp = add('close', cmd_close, True)
    sp.add_argument('frm', choices=NODES)
    sp.add_argument('to', choices=NODES)
    sp.add_argument('--force', action='store_true', help='stop the peer and close unilaterally')
    sp = add('send', cmd_send, True)
    sp.add_argument('node', choices=NODES)
    sp.add_argument('address')
    sp.add_argument('units', type=int)

    args = p.parse_args()
    try:
        args.fn(args)
    except L.Fail as e:
        sys.exit(f"error: {e}")


if __name__ == '__main__':
    main()
