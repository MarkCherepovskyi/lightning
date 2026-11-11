# SPDX-License-Identifier: MIT
"""Helpers for tests/test_evmchan.py: Core Lightning on the EVM UtxoChain
contract (contrib/evmchan, SPEC 5-7) instead of bitcoind.

- EvmChain: one anvil per test, on a port reserved through pyln-testing,
  with UtxoChain deployed from its Foundry artifact. It stands in for
  pyln-testing's `bitcoind` (NodeFactory hands it to every node), and it
  "mines" by advancing anvil's time: one virtual block is INTERVAL seconds
  (SPEC 3.1). A background miner keeps the chain moving (one block a
  second), so CLN's rebroadcasts happen and the plugin's tag and lag alarms
  stay quiet; tests pause it where they need the chain still.
- EvmNode: a LightningNode run with --disable-plugin=bcli and
  contrib/evmchan/evmbackend.py, without bcli's --bitcoin-* options, with
  the SPEC 5.6 anvil options, and with its own ETH key (written to
  <lightning-dir>/regtest/evm-key and funded before it starts).
- On-chain facts come from the contract itself: its TxApplied events,
  isUnspent, withdrawable and ETH balances.
- Token mode (SPEC 4.6): EvmChain(token_decimals=D) deploys a mock ERC-20
  of D decimals (contracts/evm/test/Tokens.sol) and a UtxoChainToken on it
  instead; every node key gets NODE_TOKENS of it too. Amounts on chain are
  then token units, `units` per sat.
"""
from fixtures import LightningNode as TestsLightningNode
from pyln.testing.utils import (LightningD, TIMEOUT, drop_unused_port, reserve_unused_port,
                                wait_for)

import atexit
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVMCHAN = os.path.join(ROOT, 'contrib', 'evmchan')
sys.path.insert(0, EVMCHAN)

import btc        # noqa: E402
import evm        # noqa: E402
import evmindex   # noqa: E402

PLUGIN = os.path.join(EVMCHAN, 'evmbackend.py')
HOLD = os.path.join(EVMCHAN, 'hold.py')
ARTIFACT = os.path.join(ROOT, 'contracts', 'evm', 'out', 'UtxoChain.sol', 'UtxoChain.json')
TOKEN_ARTIFACT = os.path.join(ROOT, 'contracts', 'evm', 'out', 'UtxoChainToken.sol', 'UtxoChainToken.json')
MOCK_ARTIFACT = os.path.join(ROOT, 'contracts', 'evm', 'out', 'Tokens.sol', 'MockERC20.json')
FOUNDRY_BIN = os.path.expanduser('~/.foundry/bin')

INTERVAL = 2                      # SPEC 2: seconds per virtual block on anvil
SAT = 10**10                      # SPEC 2: wei per sat
# anvil's default account 0 (its key is public): the deployer.
DEPLOYER_KEY = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80'
NODE_ETH = 100 * 10**18           # what each node key gets before it starts
NODE_TOKENS = 10**8               # and, in token mode, sats' worth of the token


def units_per_sat(decimals):
    """SPEC 2/4.6: 10^(decimals - 8) token units per sat, or 1 below 8 decimals."""
    return 10**(decimals - 8) if decimals >= 8 else 1


def _code(artifact):
    with open(artifact) as f:
        return evm.unhex(json.load(f)['bytecode']['object'])


# SPEC 5.6, the anvil column: 80 s at INTERVAL 2.
OPTIONS_56 = {
    'watchtime-blocks': 40,
    'cltv-delta': 40,
    'cltv-final': 40,
    'funding-confirms': 1,
    'max-concurrent-htlcs': 30,
    'dev-force-features': '-63',
    'dev-bitcoind-poll': 1,
}
# pyln-testing's own defaults (LIGHTNINGD_CONFIG) that 5.6 replaces; a node
# started with evm_options=False gets lightningd's regtest defaults instead.
PYLN_DELAYS = ('watchtime-blocks', 'cltv-delta', 'cltv-final')

# BROKEN lines every node may log (each one is upstream CLN's, not ours):
# gossipd's when a closed channel's last update arrives late.
EXPECTED_BROKEN = [r'gossipd gave channel_update in CGOSSIP_CHANNEL_DEAD\?']
# The plugin's alarms (SPEC 5.5), for the tests that stop the chain on purpose.
ALARM_TAG = r'evmbackend.py: alarm: the safe block has not advanced'
ALARM_LAG = r'evmbackend.py: alarm: lag L = '
ALARM_SYNC = r'evmbackend.py: alarm: index sync failing'

TX_APPLIED_TYPES = ['bytes']

# Every anvil started, so none outlives the test process (even when a test
# or an xdist worker dies without its fixture's teardown).
_live = set()


@atexit.register
def _kill_anvils():
    for p in list(_live):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


IS_UNSPENT = 'isUnspent(bytes32,uint32,uint64,bytes,uint32)'


def anvil_bin():
    found = [os.path.join(d, 'anvil') for d in [FOUNDRY_BIN] + os.environ.get('PATH', '').split(os.pathsep)]
    for f in found:
        if os.access(f, os.X_OK):
            return f
    raise RuntimeError('anvil not found (install Foundry)')


class Event:
    """One TxApplied event of the contract (SPEC 4.2)."""

    def __init__(self, log):
        self.block = int(log['blockNumber'], 16)
        self.block_hash = log['blockHash']
        self.log_index = int(log['logIndex'], 16)
        self.evm_tx = log['transactionHash']
        self.height = int(log['topics'][1], 16)
        self.txid = btc.display(evm.unhex(log['topics'][2]))
        self.raw = evm.abi_decode(TX_APPLIED_TYPES, evm.unhex(log['data']))[0]
        self.tx = btc.Tx.parse(self.raw)
        assert self.tx.txid_hex() == self.txid, 'event txid does not match its raw tx'

    @property
    def is_deposit(self):
        return len(self.tx.inputs) == 1 and not self.tx.has_witness() and not self.tx.inputs[0].script_sig \
            and self.tx.version == 2 and self.tx.locktime == 0

    def spends(self):
        return [(btc.display(i.prev_txid), i.vout) for i in self.tx.inputs]

    def __repr__(self):
        return 'Event({} at height {}, EVM block {})'.format(self.txid, self.height, self.block)


class EvmChain:
    """anvil and a fresh UtxoChain for one test. Passed to the nodes as their
    `bitcoind`, so it provides what NodeFactory and LightningNode use of one
    (get_proxy().rpcport, mock_rpc, canned_blocks)."""

    canned_blocks = None

    def __init__(self, directory, interval=INTERVAL, keep_state=False, wallclock=False, token_decimals=None):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.interval_arg = interval
        self.port = reserve_unused_port()
        self.rpcport = self.port              # LightningD.start reads it (and we drop it again)
        self.url = 'http://127.0.0.1:{}'.format(self.port)
        self.state_file = os.path.join(directory, 'anvil-state.json') if keep_state else None
        self.log_file = os.path.join(directory, 'anvil.log')
        self.rpc = evm.EvmRpc(self.url, timeout=30)
        self.nodes = []
        self.proc = None
        self._lock = threading.RLock()
        self._pause = 0
        self._stop = threading.Event()
        self._miner = None
        self.wallclock = wallclock
        self.mined = 0
        self._send_lock = threading.Lock()    # the deployer's sends (nodes are built in parallel)
        self.token = None                     # token mode: the mock ERC-20's address
        self.units = SAT                      # wei, or token units, per sat
        try:
            self.start_anvil()
            self.deployer = evm.Account(DEPLOYER_KEY)
            if token_decimals is None:
                data = _code(ARTIFACT) + evm.abi_encode(['uint256'], [interval])
            else:
                rc = self.rpc.send(self.deployer, None, gas=3_000_000,
                                   data=_code(MOCK_ARTIFACT) + evm.abi_encode(['uint8'], [token_decimals]))
                self.token = evm.checksum_address(rc['contractAddress'])
                self.units = units_per_sat(token_decimals)
                data = _code(TOKEN_ARTIFACT) + evm.abi_encode(['uint256', 'address', 'uint256'],
                                                              [interval, self.token, self.units])
            rc = self.rpc.send(self.deployer, None, data=data, gas=8_000_000)
            self.contract = evm.checksum_address(rc['contractAddress'])
            self.deploy_block = int(rc['blockNumber'], 16)
            self.G = self.const('GENESIS_TIME')
            self.I = self.const('INTERVAL')   # noqa: E741
            assert self.I == interval and self.const('DEPLOY_BLOCK') == self.deploy_block
            self.chain_id = self.rpc.chain_id()
            for _ in range(4):
                self.mine1()
        except BaseException:
            self.stop()          # no anvil left behind
            raise

    # ---------------------------------------------------- what NodeFactory uses

    def get_proxy(self):
        return self

    def mock_rpc(self, method, fn=None):
        """set_feerates() mocks bitcoind's estimatesmartfee; the plugin
        answers estimatefees itself (SPEC 5.3)."""

    # ---------------------------------------------------- anvil

    def start_anvil(self):
        args = [anvil_bin(), '--port', str(self.port), '--slots-in-an-epoch', '1', '--silent']
        if self.state_file:
            # --preserve-historical-states: eth_call pinned to an older block
            # (the plugin's proof at `safe`) still works after a restart.
            args += ['--state', self.state_file, '--preserve-historical-states']
        with open(self.log_file, 'a') as log:     # anvil has its own copy of the fd
            self.proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        _live.add(self.proc)
        end = time.time() + 30
        while True:
            try:
                self.rpc.call('eth_chainId')
                return
            except evm.EvmError:
                if self.proc.poll() is not None or time.time() > end:
                    raise RuntimeError('anvil did not start on port {}'.format(self.port))
                time.sleep(0.1)

    def stop_anvil(self):
        """SIGTERM (anvil dumps its state to --state on the way out)."""
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(10)
        _live.discard(self.proc)
        self.proc = None

    def stop(self):
        self._stop.set()
        if self._miner is not None:
            self._miner.join(10)
        self.stop_anvil()
        drop_unused_port(self.port)

    # ---------------------------------------------------- the contract

    def const(self, name):
        return evm.abi_decode(['uint256'], self.rpc.eth_call(self.contract, evm.selector(name + '()')))[0]

    def height_of(self, ts):
        return (ts - self.G) // self.I

    def latest(self):
        return self.rpc.block('latest')

    def height(self):
        """H(latest): the contract's current height (SPEC 3.1)."""
        return self.height_of(int(self.latest()['timestamp'], 16))

    def expected_tip(self):
        """The tip the plugin reports once synced: H(safe) - 1 (SPEC 3.4)."""
        return max(self.height_of(int(self.rpc.block('safe')['timestamp'], 16)) - 1, 0)

    def events(self):
        logs = self.rpc.get_logs(self.contract, [evmindex.TOPIC0], self.deploy_block, 'latest')
        return [Event(x) for x in logs]

    def event(self, txid):
        for e in self.events():
            if e.txid == txid:
                return e
        return None

    def reference_blocks(self, upto):
        """{height: block hash (display hex)} of virtual blocks 0..upto,
        built here from the contract's events (SPEC 3.2), independently of
        the plugin's index."""
        by_height = {}
        for e in sorted(self.events(), key=lambda e: (e.block, e.log_index)):
            by_height.setdefault(e.height, []).append(e.raw)
        prev, out = bytes(32), {}
        for h in range(upto + 1):
            _, prev = btc.build_block(h, prev, by_height.get(h, []), self.G, self.I)
            out[h] = btc.display(prev)
        return out

    def wait_event(self, txid, timeout=TIMEOUT):
        """The TxApplied event of txid, once the contract has applied it."""
        wait_for(lambda: self.event(txid) is not None, timeout=timeout)
        return self.event(txid)

    def spender(self, txid, vout):
        """The event of the tx that spent (txid, vout), or None."""
        for e in self.events():
            if (txid, vout) in e.spends():
                return e
        return None

    def output(self, txid, vout):
        """(amount, scriptPubKey, height) of an output the contract created."""
        e = self.event(txid)
        assert e is not None, 'no TxApplied for {}'.format(txid)
        amount, spk = e.tx.outputs[vout]
        return amount, spk, e.height

    def is_unspent(self, txid, vout):
        """The contract's own isUnspent for an output it created (SPEC 4.2)."""
        amount, spk, h = self.output(txid, vout)
        data = evm.calldata(IS_UNSPENT, ['bytes32', 'uint32', 'uint64', 'bytes', 'uint32'],
                            [btc.internal(txid), vout, amount, spk, h])
        return evm.abi_decode(['bool'], self.rpc.eth_call(self.contract, data))[0]

    def withdrawable(self, address):
        data = evm.calldata('withdrawable(address)', ['address'], [address])
        return evm.abi_decode(['uint256'], self.rpc.eth_call(self.contract, data))[0]

    def balance(self, address):
        return int(self.rpc.request('eth_getBalance', address, 'latest'), 16)

    def token_balance(self, address):
        """Token mode: the mock token's balanceOf, in units."""
        data = evm.calldata('balanceOf(address)', ['address'], [address])
        return evm.abi_decode(['uint256'], self.rpc.eth_call(self.token, data))[0]

    def fund_token(self, address, units):
        """Token mode: mints `units` of the mock token to address."""
        with self._send_lock:
            self.rpc.send(self.deployer, self.token, gas=200_000, data=evm.calldata(
                'mint(address,uint256)', ['address', 'uint256'], [address, units]))

    def fund_eth(self, address, wei=NODE_ETH):
        self.rpc.call('anvil_setBalance', address, evm.qty(wei))

    def fee_of(self, txid):
        """The in-tx fee of an applied tx, in sats: its inputs, as the
        contract recorded them, minus its outputs."""
        e = self.event(txid)
        ins = sum(self.output(t, v)[0] for t, v in e.spends())
        return ins - sum(a for a, _ in e.tx.outputs)

    # ---------------------------------------------------- mining

    def mine1(self):
        """One EVM block INTERVAL seconds after the latest: the next virtual
        block (a submit anvil automines in between just takes its own)."""
        with self._lock:
            for _ in range(20):
                ts = int(self.latest()['timestamp'], 16) + self.I
                try:
                    self.rpc.call('evm_setNextBlockTimestamp', evm.qty(ts))
                    self.rpc.call('evm_mine')
                except evm.EvmError:
                    time.sleep(0.05)     # an automined submit got there first
                    continue
                # (A submit automined between the two calls takes ts, and our
                # block follows it: the height has advanced either way.)
                self.mined += 1
                return
            raise RuntimeError('cannot mine')

    def wait_synced(self, nodes=None, timeout=TIMEOUT):
        """Every running node's lightningd at the tip its plugin can report."""
        tip = self.expected_tip()
        for n in (self.nodes if nodes is None else nodes):
            if n.running():
                wait_for(lambda: n.rpc.getinfo()['blockheight'] >= tip, timeout=timeout)
        return tip

    def mine(self, n=1, sync=True, nodes=None):
        """n virtual blocks (plus one, so `safe` covers the last), then waits
        for the nodes to reach the new tip. Returns the tip."""
        with self._lock:
            for _ in range(n + 1):
                self.mine1()
        return self.wait_synced(nodes) if sync else self.expected_tip()

    def mine_wallclock(self):
        """One EVM block at the wall clock (or a second after the latest,
        if the chain is ahead): the safe block advances, heights follow the
        wall clock (SPEC 3.1)."""
        with self._lock:
            for _ in range(20):
                ts = max(int(self.latest()['timestamp'], 16) + 1, int(time.time()))
                try:
                    self.rpc.call('evm_setNextBlockTimestamp', evm.qty(ts))
                    self.rpc.call('evm_mine')
                    return
                except evm.EvmError:
                    time.sleep(0.05)
            raise RuntimeError('cannot mine')

    def _mine_loop(self, period, wallclock):
        while not self._stop.wait(period):
            with self._lock:     # paused() takes it too: no block after it returns
                if self._pause or self.proc is None:
                    continue
                try:
                    self.mine_wallclock() if wallclock else self.mine1()
                except Exception as e:     # anvil going away under a test: it says why
                    logging.info('background miner: {}'.format(e))

    def keep_mining(self, period=1.0, wallclock=False):
        """Mines a block every `period` seconds until stop() (SPEC 5.5's
        alarms watch the tag advance; CLN rebroadcasts on new blocks): the
        next virtual block each time, or with wallclock=True at the wall
        clock, so heights keep real time."""
        assert self._miner is None
        self._miner = threading.Thread(target=self._mine_loop, args=(period, wallclock), daemon=True)
        self._miner.start()

    @contextlib.contextmanager
    def paused(self):
        """The background miner stops for the duration (anvil still
        automines every submit)."""
        with self._lock:
            self._pause += 1
        try:
            yield
        finally:
            with self._lock:
                self._pause -= 1

    def wait_mining(self, cond, blocks=400, timeout=TIMEOUT * 4, burst=1):
        """Mines `burst` blocks at a time until cond() holds: for waits that
        need the chain to move faster than the background miner."""
        end = time.time() + timeout
        for _ in range(blocks):
            if cond():
                return True
            if time.time() > end:
                break
            self.mine(burst, sync=False)
            time.sleep(0.3)
        assert cond(), 'condition not met after mining'
        return True


class EvmLightningD(LightningD):
    """LightningD without bcli's --bitcoin-* options (lightningd would
    refuse them as unknown with bcli disabled)."""

    @property
    def cmd_line(self):
        return [a for a in LightningD.cmd_line.fget(self) if not a.startswith('--bitcoin-')]


def _listify(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


class EvmNode(TestsLightningNode):
    """A CLN node on evmbackend.py. Extra keyword arguments (through
    node_factory.get_node): evm_options=False starts it with lightningd's
    regtest defaults instead of SPEC 5.6's; fund_eth (wei) for its key."""

    def __init__(self, node_id, lightning_dir, bitcoind, executor, evm_options=True, fund_eth=NODE_ETH,
                 **kwargs):
        chain = bitcoind
        allowed = EXPECTED_BROKEN + _listify(kwargs.pop('broken_log', None))
        kwargs['broken_log'] = '|'.join('(?:{})'.format(p) for p in allowed)
        user = dict(kwargs.get('options') or {})
        TestsLightningNode.__init__(self, node_id, lightning_dir, chain, executor, **kwargs)
        self.chain = chain
        self.daemon.__class__ = EvmLightningD
        opts = self.daemon.opts
        for k in ('bitcoin-rpcuser', 'bitcoin-rpcpassword', 'bitcoin-datadir'):
            opts.pop(k, None)
        if evm_options:
            for k, v in OPTIONS_56.items():
                if k not in user:
                    opts[k] = v
        else:
            for k in PYLN_DELAYS:
                if k not in user:
                    opts.pop(k, None)
        opts['disable-plugin'] = ['bcli'] + [p for p in _listify(user.get('disable-plugin')) if p != 'bcli']
        opts['plugin'] = [PLUGIN] + _listify(user.get('plugin'))
        opts.setdefault('evm-rpc', chain.url)
        opts.setdefault('evm-contract', chain.contract)
        opts.setdefault('evm-poll', 1)
        # The plugins (#!/usr/bin/env python3) run on the tests' own Python,
        # which has coincurve and pyln.
        self.daemon.env['PATH'] = os.path.dirname(sys.executable) + os.pathsep + self.daemon.env.get('PATH', '')
        # The node's ETH key, before it starts (the plugin would make one,
        # but at balance 0 it alarms and refuses opens).
        key_file = os.path.join(lightning_dir, 'regtest', 'evm-key')
        if not os.path.exists(key_file):
            key = os.urandom(32)
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write('0x' + key.hex() + '\n')
        with open(key_file) as f:
            self.eth = evm.Account(evm.unhex(f.read().strip()))
        self.eth_address = self.eth.address
        if fund_eth:
            chain.fund_eth(self.eth_address, fund_eth)
        if chain.token is not None:
            chain.fund_token(self.eth_address, NODE_TOKENS * chain.units)
        chain.nodes.append(self)

    # ---------------------------------------------------- helpers

    def running(self):
        return self.daemon.proc is not None and self.daemon.proc.poll() is None

    def log_text(self):
        self.daemon.logs_catchup()
        return '\n'.join(self.daemon.logs)

    def evm_status(self):
        return self.rpc.call('evm-status')

    def deposit(self, sats, addrtype='p2tr'):
        """evm-deposit to a fresh address of ours; waits until the wallet
        has it confirmed. Returns (txid, address)."""
        addr = self.rpc.newaddr(addrtype)[addrtype]
        d = self.rpc.call('evm-deposit', {'amount': sats, 'address': addr})
        assert d['status'] == 'mined', d
        wait_for(lambda: any(o['txid'] == d['txid'] and o['status'] == 'confirmed'
                             for o in self.rpc.listfunds()['outputs']))
        return d['txid'], addr

    def channel(self, peer=None, funding_txid=None):
        cs = self.rpc.listpeerchannels(peer.info['id'] if peer else None)['channels']
        if funding_txid:
            cs = [c for c in cs if c['funding_txid'] == funding_txid]
        return cs[-1] if cs else None

    def state(self, peer=None, funding_txid=None):
        c = self.channel(peer, funding_txid)
        return c['state'] if c else None
