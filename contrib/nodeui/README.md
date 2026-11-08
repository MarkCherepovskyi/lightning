# nodeui: one page for Bitcoin, Liquid and EVM Lightning nodes

`ui.py` serves a web page at http://127.0.0.1:8765 that manages Core Lightning
nodes on three kinds of network at once:

- **Bitcoin**: plain lightningd (bitcoin, testnet, testnet4, signet, regtest);
- **Liquid**: lightningd with the asset-channel plugin (`contrib/assetchan`);
- **EVM**: lightningd on the EVM backend plugin (`contrib/evmchan/evmbackend.py`).

It needs Python 3 and pyln-client only. The page is one static file with no build
step. It replaces `contrib/assetchan/ui.py`.

```sh
W=/path/to/lightning
export PYTHONPATH=$W/contrib/pyln-client:$W/contrib/pyln-proto
python3 $W/contrib/nodeui/ui.py --node alice=~/.lightning --node bob=/path/to/lightning-dir
python3 $W/contrib/nodeui/ui.py --config nodes.json
# the old Liquid command line still works: nodes.sh's A..D under ~/.assetchan-regtest
python3 $W/contrib/nodeui/ui.py --network liquid-regtest --elements-rpc http://poc:poc@127.0.0.1:18884
```

Open http://127.0.0.1:8765. To see the page with sample data and no nodes, open
`/?demo`.

`--node NAME=DIR` takes the lightning dir. The page finds `DIR/<network>/lightning-rpc`
itself; `--network` pins the subdirectory. `nodes.json` looks like this:

```json
{"nodes": [{"name": "alice", "dir": "~/.lightning", "network": "regtest"}],
 "labels": {"<asset id or token address>": "USDC"},
 "bitcoin-rpc": "http://user:pass@127.0.0.1:18443",
 "elements-rpc": "http://user:pass@127.0.0.1:18884", "elements-wallet": "poc"}
```

Other options:
- `--label ID=NAME` names a Liquid asset or an ERC-20 token.
- `--bitcoin-rpc` and `--elements-rpc` are only needed when the nodes' own options
  don't give the credentials.
- `--evm-status-every S` (default 300) is how often `evm-status` is read on chains
  other than anvil. It asks for `cached=true`, so the plugin reads no fresh
  balance: it reports the ETH balance its poll keeps, and `withdrawable` at most
  once per `evm-poll` (SPEC 5.5). A plugin without that parameter (the ERC-20
  branch's) gets a plain call instead. On anvil the page reads it every 3 s.
- `--port`.

## Node kinds and network IDs

On first contact, and again whenever a node comes back after being down (or comes
back as another node or network), the server asks each node:

| kind | how it is detected | network ID |
|---|---|---|
| `evm` | `help` lists `evm-status` | `evm:<chain id>:<contract>`, shown as `evm:84532:0xB820…940E` |
| `liquid` | `assetchan-*` methods, or the network is `liquid*` | `liquid:<mainnet/testnet/regtest>` |
| `bitcoin` | anything else | `btc:<network>`; the tooltip shows the chain hash |

A channel's ID adds what it carries:
- `liquid:regtest/L-BTC`, or `liquid:regtest/<asset label or first 8 hex digits>`;
- `evm:…/ETH`, or `evm:…/<token label or address prefix>`.

A node that is down shows as down. The rest of the page keeps working.

The **Network** switcher lists All plus every network ID. It filters the nodes,
channels and maps.

**Safety:**
- Open and Pay only offer a peer with the same network ID as the paying or opening
  node. The server refuses anything else with HTTP 400, whatever the page sends.
- A pasted invoice must carry the network's prefix (`lnbcrt`, `lnert`, …). It must
  not belong to a page node on another network: EVM and Bitcoin regtest invoices
  look alike, so the server checks the payee too.
- A Liquid channel's asset must be in `assetchan-asset` of both nodes.
- A node that isn't fully detected yet (an EVM node whose `evm-status` hasn't
  answered) can't open or pay.

## What the page does

For every node it shows:
- info, peers, the wallet balance;
- every channel, with its split, its state and onchaind's latest status after a
  force close;
- a map per network ID, from `listchannels` (gossip) plus the local channels.

Actions:
- open, pay (between page nodes, or a pasted bolt11), invoice;
- close, force close;
- send on-chain, new address, fund;
- deposit, exit, withdraw (EVM only);
- mine (only on the networks listed under Mining below).

Actions run one at a time per network ID, and side by side on different networks.
A node has one network ID, so two networks' actions never touch the same node; an
action naming a node that another action uses waits too. A second action on a busy
network waits its turn, in order (at most 3 wait per network; more get HTTP 409).
The Activity panel shows every running and waiting action, each with its own Cancel,
and the finished ones with their logs. Cancel drops a waiting action and stops a
running one at its next wait, not in the middle of an RPC.

Where nobody mines (Base Sepolia, Liquid testnet), an open ends once `fundchannel`
returns and a deposit once `evm-deposit` reports the EVM tx mined. The page then shows
the progress: "confirming 1/3" on the channel (its funding depth against
`funding-confirms`), the deposit on the node card ("CLN at block X of H") until the
wallet lists it, and unconfirmed wallet balances. On regtest and anvil they still mine
the blocks they need.

Each EVM node has a **Refresh** button for one fresh `evm-status` read (an
`eth_getBalance` and an `eth_call`), at most once per 30 s per node. Each EVM node an
action used is also read afresh when the action ends.

ETH shows with at most 6 decimals, with `≈` when rounded; the tooltip has the exact
value in ETH and wei. A token whose `units_per_sat` is 1 shows once, as
"1,076,195 USDC units". After an exit or a send of "all", the log says what stayed in
the wallet: CLN keeps `min-emergency-msat` (default 25,000 sat) while it still knows
anchor channels, until about 100 blocks after a close. The node card says so too.

- **Bitcoin:** core RPCs. On regtest the page mines the blocks an action waits for,
  through bitcoind's RPC. The credentials come from `--bitcoin-rpc`, else from the
  node's `bitcoin-rpcuser/-rpcpassword/-rpcconnect/-rpcport`, else from bitcoind's
  cookie. lightningd conceals the password in `listconfigs`, so it is read back from
  the config file that set it. Fund sends from bitcoind's wallet.
- **Liquid:** everything the old page did: asset and L-BTC channels, payments
  (`assetchan-invoice`/`-pay`), closes, on-chain sends from elementsd's wallet, and
  funding a node with L-BTC. On liquid-regtest it mines through elementsd
  (`--elements-rpc`, else the node's `assetchan-elements-rpc`). The Asset sweeps
  table comes from `assetchan-sweeps`.
- **EVM:** a status panel per node shows:
  - the contract, chain id, and ETH or the token (with `units_per_sat`);
  - the gas balance, with a warning below `evm-min-balance`;
  - the token balance and what is withdrawable;
  - tip, current height, lag, and the `safe` tag's age;
  - pending sends;
  - credit use: `rpc_calls`, priced as in `contrib/evmchan/smoke_base_sepolia.py`,
    as credits/day and % of the 3M free tier;
  - alarms, option problems and refused opens, as a red banner.

  ETH shows as ETH and sat (1 sat = 1e10 wei). Tokens show as units and sat.
  The ERC-20 fields (`token`, `units_per_sat`, `token_balance_units`,
  `withdrawable_units`, `amount_units`) are optional. The EVM actions are:
  - Deposit: `evm-deposit` of N sat to a fresh p2tr address of the node.
  - Exit: `withdraw` of N sat (or all) to `evm-exitaddr` of the node's own
    `eth_address`.
  - Withdraw: `evm-withdraw`.

  Mining is offered only on anvil (chain id 31337): `evm_setNextBlockTimestamp` +
  `evm_mine` on the node's `evm-rpc`, INTERVAL seconds per block, plus one more
  block so `safe` covers them. Elsewhere the page waits.

The server polls the nodes in the background every 3 s while someone has the page
open, and stops 90 s after the last view. A page load never waits for a node. A
node that hangs keeps one request in flight and shows as "not answering".

## Security

- It listens on 127.0.0.1 only and refuses other `Host` and `Origin` headers, which
  blocks DNS rebinding and cross-site posts.
- `POST /api/job`, `/api/cancel`, `/api/refresh` and `GET /api/state` need the per-run token that the page carries
  in `X-NodeUI-Token`, and POST needs `application/json`.
- The token protects you from web pages, not from local users. Anyone who can reach
  127.0.0.1 on this machine can load the page and spend the nodes' funds. Don't run
  it on a shared machine.
- Every input is validated: node names, whole-number amounts, addresses per network,
  bolt11 shape and prefix, channel ids that the node has, and the network-ID checks
  above.
- No shell or subprocess is involved.
- RPC URLs and passwords are never sent to the page, logged or printed. That covers
  `evm-rpc` (its `${INFURA_API_KEY}` is read from the environment),
  `assetchan-elements-rpc` and bitcoind's credentials. Every error, log line and
  state field is scrubbed of known secrets, URLs, `user:pass@` and `/v3/<key>`
  paths.
- The page loads its fonts from Google Fonts, as the old page did.

## Tests

```sh
PYTHONPATH=$W/contrib/pyln-client:$W/contrib/pyln-proto python3 -m pytest contrib/nodeui/tests
```

They use fake RPC objects and start no daemons. They cover:
- detection and network IDs;
- cross-network refusals, including over HTTP;
- building every action and running most of them on fakes;
- secret scrubbing;
- token-mode `evm-status` and amount formatting;
- the job model: parallel jobs on two networks, waiting on one, cancel by id;
- `cached=true` polling, its fallback, Refresh and its rate limit;
- confirmation progress and the emergency-reserve note.

`ui/demo.json` is generated from the same fakes.
