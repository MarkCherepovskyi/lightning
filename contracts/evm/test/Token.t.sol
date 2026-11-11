// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChainBase} from "./ChainBase.sol";
import {UtxoChainTokenHarness} from "./Harness.sol";
import {UtxoChainToken} from "../src/UtxoChainToken.sol";
import {MockERC20, NoBoolToken, FalseToken, FeeToken, HookToken, NoDecimalsToken} from "./Tokens.sol";
import {RealVectors} from "./generated/RealVectors.sol";
import {Synthetic} from "./generated/Synthetic.sol";
import {Case} from "./Types.sol";
import {Prevout} from "../src/BtcTx.sol";
import "../src/Errors.sol" as E;

/// Takes a fee by submitting, then withdraws it; a hook token calls back
/// onTokenTransfer, from which it re-enters `withdraw` (or `deposit`).
contract TokenReenterer {
    UtxoChainToken internal immutable chain;
    bool internal immutable propagate; // let the inner revert through (else catch it)
    bool public viaDeposit; // re-enter with a deposit instead of a withdraw
    uint256 public calls;
    bytes public innerError;

    constructor(UtxoChainToken c, bool propagate_) {
        chain = c;
        propagate = propagate_;
    }

    function setViaDeposit(bool v) external {
        viaDeposit = v;
    }

    function approve(MockERC20 t, uint256 amount) external {
        t.approve(address(chain), amount);
    }

    function submit(bytes calldata raw, Prevout[] calldata pv) external {
        chain.submit(raw, pv);
    }

    function attack() external {
        chain.withdraw();
    }

    function depositFrom(uint256 amount, bytes calldata spk) external {
        chain.deposit(amount, spk);
    }

    function onTokenTransfer(address, address, uint256) external {
        calls += 1;
        if (calls > 1) return;
        bytes memory call_ = viaDeposit
            ? abi.encodeCall(chain.deposit, (1e10, hex"0014aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))
            : abi.encodeCall(chain.withdraw, ());
        (bool ok, bytes memory e) = address(chain).call(call_);
        if (!ok) {
            innerError = e;
            if (propagate) {
                assembly ("memory-safe") {
                    revert(add(e, 32), mload(e))
                }
            }
        }
    }
}

/// Several deposits and a withdraw in one EVM tx: the lock is released after
/// each token call.
contract Batch {
    function run(UtxoChainToken chain, MockERC20 t, bytes calldata spk) external {
        t.approve(address(chain), type(uint256).max);
        chain.deposit(chain.UNITS_PER_SAT(), spk);
        chain.deposit(2 * chain.UNITS_PER_SAT(), spk);
        chain.withdraw();
        chain.deposit(3 * chain.UNITS_PER_SAT(), spk);
    }

    function submit(UtxoChainToken chain, bytes calldata raw, Prevout[] calldata pv) external {
        chain.submit(raw, pv);
    }
}

/// UtxoChainToken (SPEC 4.6): deposits, exits and withdrawals in token
/// units, with the SPEC 4.1 invariant (on the token balance) after every
/// operation, for standard tokens of 6 and 18 decimals, a token without a
/// bool return, and the tokens it must refuse.
contract TokenTest is ChainBase {
    uint256 constant GENESIS = Synthetic.GENESIS_TIME;
    uint256 constant INTERVAL = Synthetic.INTERVAL;
    uint64 constant MAX_MONEY = 21_000_000 * 100_000_000;
    bytes constant SPK = hex"0014aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    /// 6 decimals: 1 sat = 1 unit (10^8 sat = 100 tokens). 18: 1 sat = 10^10.
    function _six() internal returns (MockERC20 t) {
        t = new MockERC20(6);
        _deployToken(GENESIS, INTERVAL, t, 1);
    }

    function _eighteen() internal returns (MockERC20 t) {
        t = new MockERC20(18);
        _deployToken(GENESIS, INTERVAL, t, 1e10);
    }

    function testConstructor() public {
        vm.roll(77);
        MockERC20 t = _six();
        assertTrue(tchain.TOKEN() == address(t), "TOKEN");
        assertEq(tchain.UNITS_PER_SAT(), 1, "UNITS_PER_SAT");
        assertEq(tchain.GENESIS_TIME(), GENESIS, "GENESIS_TIME");
        assertEq(tchain.INTERVAL(), INTERVAL, "INTERVAL");
        assertEq(tchain.DEPLOY_BLOCK(), 77, "DEPLOY_BLOCK");
        bytes memory badToken = abi.encodeWithSelector(E.BadToken.selector);
        bytes memory badUnits = abi.encodeWithSelector(E.BadUnitsPerSat.selector);
        vm.expectRevert(badToken);
        new UtxoChainToken(2, address(0), 1);
        vm.expectRevert(badToken);
        new UtxoChainToken(2, address(0xbeef), 1); // no code
        vm.expectRevert(badToken);
        new UtxoChainToken(2, address(this), 1); // code, but no balanceOf
        // units per sat: 10^(decimals - 8), or 1 below 8 decimals
        uint8[5] memory dec = [0, 6, 8, 18, 26];
        uint256[5] memory good = [uint256(1), 1, 1, 1e10, 1e18];
        for (uint256 i; i < dec.length; ++i) {
            MockERC20 m = new MockERC20(dec[i]);
            assertEq(new UtxoChainToken(2, address(m), good[i]).UNITS_PER_SAT(), good[i], "units");
            uint256[3] memory bad = [uint256(0), good[i] * 10, good[i] == 1 ? 2 : good[i] / 10];
            for (uint256 j; j < 3; ++j) {
                vm.expectRevert(badUnits);
                new UtxoChainToken(2, address(m), bad[j]);
            }
        }
        vm.expectRevert(badUnits);
        new UtxoChainToken(2, address(new MockERC20(27)), 1e19); // above 10^18
        // without decimals(): any power of ten up to 10^18
        address nd = address(new NoDecimalsToken());
        new UtxoChainToken(2, nd, 1);
        new UtxoChainToken(2, nd, 1000);
        new UtxoChainToken(2, nd, 1e18);
        uint256[4] memory ndBad = [uint256(0), 3, 1010, 1e19];
        for (uint256 j; j < 4; ++j) {
            vm.expectRevert(badUnits);
            new UtxoChainToken(2, nd, ndBad[j]);
        }
        vm.expectRevert(abi.encodeWithSelector(E.BadInterval.selector));
        new UtxoChainToken(0, address(t), 1);
    }

    /// Deposits take exactly `amount` units with transferFrom; the value rules
    /// of SPEC 2 hold in units; ETH is refused.
    function testDepositRules() public {
        for (uint256 k; k < 2; ++k) {
            MockERC20 t = k == 0 ? _six() : _eighteen();
            uint256 u = unit;
            t.mint(address(this), (uint256(MAX_MONEY) + 20_000) * u);
            vm.expectRevert(abi.encodeWithSelector(E.TooEarly.selector));
            tchain.deposit(u, SPK);
            _at(1);
            // not approved
            vm.expectRevert(abi.encodeWithSelector(E.TokenTransferFailed.selector));
            tchain.deposit(u, SPK);
            t.approve(address(tchain), type(uint256).max);
            bytes memory bad = abi.encodeWithSelector(E.BadDepositValue.selector);
            vm.expectRevert(bad);
            tchain.deposit(0, SPK);
            if (u > 1) {
                vm.expectRevert(bad);
                tchain.deposit(u - 1, SPK);
                vm.expectRevert(bad);
                tchain.deposit(u + 1, SPK);
            }
            vm.expectRevert(bad);
            tchain.deposit((uint256(MAX_MONEY) + 1) * u, SPK);
            vm.expectRevert(abi.encodeWithSelector(E.BadDepositScript.selector));
            tchain.deposit(u, hex"6a");
            // not payable, and no receive
            (bool ok,) = address(tchain).call{value: 1}(abi.encodeCall(tchain.deposit, (u, SPK)));
            assertTrue(!ok, "deposit with ETH");
            (ok,) = address(tchain).call{value: 1}("");
            assertTrue(!ok, "plain ETH");
            _checkInvariant();

            _deposit(u, SPK);
            _deposit(uint256(MAX_MONEY) * u, SPK);
            _deposit(12_345 * u, hex"5120aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
            assertEq(t.balanceOf(address(this)), 7_654 * u, "depositor paid exactly");
        }
    }

    function _deposit(uint256 amount, bytes memory spk) internal returns (bytes32 txid) {
        uint32 h = tchain.height();
        uint256 before = token.balanceOf(address(tchain));
        txid = tchain.deposit(amount, spk);
        assertEq(token.balanceOf(address(tchain)) - before, amount, "units received");
        uint64 sats = uint64(amount / unit);
        assertTrue(tchain.isUnspent(txid, 0, sats, spk, h), "deposit stored");
        outs.push(Out(txid, 0, sats, spk, h));
        _checkInvariant();
    }

    /// SPEC 3.3 holds in token mode, byte for byte: the same deposit txs and
    /// txids (amounts in sats) as the ETH contract at DEPOSIT_ADDRESS, and a
    /// spend of deposit 0 is valid.
    function testDepositTxsByteExact6() public {
        _byteExact(_six());
    }

    function testDepositTxsByteExact18() public {
        _byteExact(_eighteen());
    }

    function _byteExact(MockERC20 t) internal {
        vm.chainId(31337);
        vm.etch(Synthetic.DEPOSIT_ADDRESS, address(tchain).code);
        tchain = UtxoChainTokenHarness(Synthetic.DEPOSIT_ADDRESS);
        t.mint(address(this), 1e40);
        t.approve(address(tchain), type(uint256).max);
        _at(5);
        for (uint256 i; i < Synthetic.DEPOSIT_COUNT; ++i) {
            (uint64 amount, bytes memory spk, bytes memory raw, bytes32 txid) = Synthetic.depositTx(i);
            vm.expectEmit(true, true, false, true, address(tchain));
            emit TxApplied(5, txid, raw);
            assertEq(_deposit(uint256(amount) * unit, spk), txid, "deposit txid");
        }
        _submitValid(Synthetic.depositSpend());
        _drain();
    }

    /// Every real CLN tx, and every synthetic one, through the token chain:
    /// outputs, exits and fees in units, the invariant after each; then
    /// everything owed is withdrawn, exactly.
    function testReplayInTokenUnits() public {
        for (uint256 k; k < 2; ++k) {
            for (uint256 f; f < RealVectors.FILE_COUNT; ++f) {
                if (k == 0) {
                    _deployToken(RealVectors.GENESIS_TIME, RealVectors.INTERVAL, new MockERC20(6), 1);
                } else {
                    _deployToken(RealVectors.GENESIS_TIME, RealVectors.INTERVAL, new MockERC20(18), 1e10);
                }
                for (uint256 i; i < RealVectors.count(f); ++i) {
                    _submitValid(RealVectors.get(f, i));
                }
                _drain();
            }
            for (uint256 i; i < Synthetic.VALID_COUNT; ++i) {
                if (k == 0) _six();
                else _eighteen();
                _submitValid(Synthetic.valid(i));
                _drain();
            }
        }
    }

    /// Withdraws every payee's balance, checking each gets exactly what the
    /// ledger says, in units, and the invariant after each.
    function _drain() internal {
        for (uint256 k; k < payees.length; ++k) {
            address a = payees[k];
            uint256 w = owed[address(tchain)][a];
            if (w == 0) continue;
            uint256 before = token.balanceOf(a);
            vm.prank(a);
            tchain.withdraw();
            assertEq(token.balanceOf(a) - before, w, "withdrawn");
            owed[address(tchain)][a] = 0;
            _checkInvariant();
            vm.prank(a);
            vm.expectRevert(abi.encodeWithSelector(E.NothingToWithdraw.selector));
            tchain.withdraw();
        }
    }

    /// The exit case: the exit is credited in units and paid once by
    /// withdraw; the fee goes to the submitter.
    function testExitsFeesAndWithdraw() public {
        for (uint256 k; k < 2; ++k) {
            if (k == 0) _six();
            else _eighteen();
            Case memory c = Synthetic.valid(Synthetic.EXIT_CASE);
            _submitValid(c);
            for (uint256 j; j < c.outScripts.length; ++j) {
                if (_isExit(c.outScripts[j])) {
                    address to = _exitAddress(c.outScripts[j]);
                    assertEq(tchain.withdrawable(to), uint256(c.outAmounts[j]) * unit, "exit in units");
                }
            }
            assertEq(tchain.withdrawable(address(this)), uint256(c.fee) * unit, "fee in units");
            _drain();
        }
    }

    /// Deposits and a withdraw from one contract in one EVM tx all succeed:
    /// each token call releases the lock (transient storage lasts the whole
    /// tx, unlike between a test's calls).
    function testSeveralCallsInOneTx() public {
        MockERC20 t = _six();
        Batch bt = new Batch();
        Case memory c = Synthetic.valid(0);
        for (uint256 i; i < c.prevouts.length; ++i) {
            _seed(c.inTxids[i], c.inVouts[i], c.prevouts[i]);
        }
        _at(c.height);
        bt.submit(tchain, c.rawTx, c.prevouts); // its fee goes to bt
        uint256 fee = uint256(c.fee) * unit;
        _payee(address(bt));
        owed[address(tchain)][address(bt)] = fee;
        c.fee = 0;
        _applied(c);
        t.mint(address(bt), 6);
        uint256 n = tchain.depositNonce();
        uint256 held = t.balanceOf(address(tchain));
        bt.run(tchain, t, SPK);
        assertEq(t.balanceOf(address(tchain)), held + 6 - fee, "held: + 6 units deposited - the fee");
        assertEq(tchain.depositNonce(), n + 3, "three deposits");
        assertEq(t.balanceOf(address(bt)), fee, "the fee withdrawn, 6 units deposited");
        assertEq(tchain.withdrawable(address(bt)), 0, "withdrawn");
    }

    /// USDT-style: no return value is a success.
    function testNoBoolToken() public {
        NoBoolToken t = new NoBoolToken();
        _deployToken(GENESIS, INTERVAL, t, 1);
        t.mint(address(this), 1e12);
        t.approve(address(tchain), type(uint256).max);
        _at(1);
        _deposit(5_000, SPK);
        Case memory c = Synthetic.valid(Synthetic.EXIT_CASE);
        _submitValid(c);
        _drain();
    }

    /// A token returning false, or a malformed value, is a failed transfer;
    /// a failed withdraw keeps the balance owed.
    function testFalseToken() public {
        FalseToken t = new FalseToken();
        _deployToken(GENESIS, INTERVAL, t, 1);
        t.mint(address(this), 1e12);
        t.approve(address(tchain), type(uint256).max);
        _at(1);
        t.set(true, false);
        vm.expectRevert(abi.encodeWithSelector(E.TokenTransferFailed.selector));
        tchain.deposit(5_000, SPK);
        t.set(false, true);
        vm.expectRevert(abi.encodeWithSelector(E.TokenTransferFailed.selector));
        tchain.deposit(5_000, SPK);
        t.set(false, false);
        _deposit(5_000, SPK);
        _submitValid(Synthetic.valid(Synthetic.EXIT_CASE));
        uint256 w = tchain.withdrawable(address(this));
        assertTrue(w != 0, "fee owed");
        t.set(true, false);
        vm.expectRevert(abi.encodeWithSelector(E.TokenTransferFailed.selector));
        tchain.withdraw();
        t.set(false, true);
        vm.expectRevert(abi.encodeWithSelector(E.TokenTransferFailed.selector));
        tchain.withdraw();
        assertEq(tchain.withdrawable(address(this)), w, "still owed");
        _checkInvariant();
        t.set(false, false);
        _drain();
    }

    /// Fee-on-transfer: whatever the token does, the contract's balance must
    /// move by exactly the amount. A fee out of what the contract receives
    /// refuses the deposit; one charged to the contract on top refuses the
    /// withdraw, which stays owed. A fee the depositor pays on top, or one out
    /// of what a withdrawer receives, is theirs: the contract's books stay
    /// exact.
    function testFeeOnTransferRefused() public {
        FeeToken t = new FeeToken();
        _deployToken(GENESIS, INTERVAL, t, 1);
        t.mint(address(this), 1e12);
        t.approve(address(tchain), type(uint256).max);
        _at(1);
        t.setFee(10, false); // 0.1% + 1 unit, out of what the contract receives
        vm.expectRevert(abi.encodeWithSelector(E.TokenAmountMismatch.selector));
        tchain.deposit(100_000, SPK);
        assertEq(tchain.depositNonce(), 0, "nothing deposited");
        t.setFee(10, true); // charged to the depositor on top
        uint256 before = t.balanceOf(address(this));
        _deposit(100_000, SPK);
        assertEq(before - t.balanceOf(address(this)), 100_000 + 101, "the depositor paid the fee");
        t.setFee(0, false);
        _submitValid(Synthetic.valid(Synthetic.EXIT_CASE));
        uint256 w = tchain.withdrawable(address(this));
        assertTrue(w != 0, "fee owed");
        t.setFee(10, true); // charged to the contract on top
        vm.expectRevert(abi.encodeWithSelector(E.TokenAmountMismatch.selector));
        tchain.withdraw();
        assertEq(tchain.withdrawable(address(this)), w, "still owed");
        _checkInvariant();
        t.setFee(10, false); // out of what the withdrawer receives
        before = t.balanceOf(address(this));
        tchain.withdraw();
        assertEq(t.balanceOf(address(this)) - before, w - (w * 10 / 10_000 + 1), "the withdrawer paid the fee");
        owed[address(tchain)][address(this)] = 0;
        _checkInvariant();
        t.setFee(0, false);
        _drain();
    }

    /// A hook token's recipient re-entering during its withdraw: a second
    /// withdraw finds nothing (the balance is zeroed first), a deposit is
    /// refused (Reentrancy); it is paid once. Letting the inner revert
    /// through fails the token's transfer and so the whole withdraw, which
    /// stays owed.
    function testReentrantWithdrawHook() public {
        for (uint256 k; k < 4; ++k) {
            HookToken t = new HookToken();
            _deployToken(GENESIS, INTERVAL, t, 1e10);
            TokenReenterer r = new TokenReenterer(tchain, k >= 2);
            r.setViaDeposit(k % 2 == 1);
            bytes memory inner = k % 2 == 1
                ? abi.encodeWithSelector(E.Reentrancy.selector)
                : abi.encodeWithSelector(E.NothingToWithdraw.selector);
            uint256 fee = _feeTo(r);
            uint256 bal = t.balanceOf(address(tchain));
            if (k < 2) {
                r.attack();
                assertEq(r.calls(), 1, "one hook call");
                assertEq(r.innerError(), inner, "re-entry refused");
                assertEq(t.balanceOf(address(r)), fee, "paid once");
                assertEq(bal - t.balanceOf(address(tchain)), fee, "contract paid the fee once");
                assertEq(tchain.withdrawable(address(r)), 0, "zeroed");
                owed[address(tchain)][address(r)] = 0;
            } else {
                // the hook's revert fails the token's transfer
                vm.expectRevert(abi.encodeWithSelector(E.TokenTransferFailed.selector));
                r.attack();
                assertEq(tchain.withdrawable(address(r)), fee, "still owed");
            }
            _checkInvariant();
        }
    }

    /// A depositor whose transferFrom hook re-enters (with a deposit, or a
    /// withdraw of its fee) is refused; propagated, the token call fails and so
    /// does the outer deposit.
    function testReentrantDepositHook() public {
        for (uint256 k; k < 4; ++k) {
            HookToken t = new HookToken();
            _deployToken(GENESIS, INTERVAL, t, 1e10);
            TokenReenterer r = new TokenReenterer(tchain, k >= 2);
            r.setViaDeposit(k % 2 == 0);
            _feeTo(r);
            t.mint(address(r), 1e20);
            r.approve(t, type(uint256).max);
            uint256 nonce = tchain.depositNonce();
            if (k < 2) {
                r.depositFrom(1e10, SPK);
                assertEq(r.innerError(), abi.encodeWithSelector(E.Reentrancy.selector), "re-entry refused");
                assertEq(tchain.depositNonce(), nonce + 1, "one deposit");
                outs.push(Out(_lastDepositTxid(), 0, 1, SPK, tchain.height()));
            } else {
                // the hook's revert fails the token's transferFrom
                vm.expectRevert(abi.encodeWithSelector(E.TokenTransferFailed.selector));
                r.depositFrom(1e10, SPK);
                assertEq(tchain.depositNonce(), nonce, "no deposit");
            }
            _checkInvariant();
        }
    }

    function _lastDepositTxid() internal view returns (bytes32) {
        bytes32 prev = keccak256(
            abi.encodePacked("evmchan deposit", block.chainid, address(tchain), tchain.depositNonce() - 1)
        );
        bytes memory raw = abi.encodePacked(
            hex"0200000001", prev, hex"0000000000ffffffff01", hex"0100000000000000", uint8(SPK.length), SPK, hex"00000000"
        );
        return sha256(abi.encodePacked(sha256(raw)));
    }

    function _feeTo(TokenReenterer r) internal returns (uint256 fee) {
        Case memory c = Synthetic.valid(0);
        for (uint256 i; i < c.prevouts.length; ++i) {
            _seed(c.inTxids[i], c.inVouts[i], c.prevouts[i]);
        }
        _at(c.height);
        r.submit(c.rawTx, c.prevouts);
        fee = uint256(c.fee) * unit;
        assertTrue(fee != 0, "fee");
        assertEq(tchain.withdrawable(address(r)), fee, "fee to the submitter");
        _payee(address(r));
        owed[address(tchain)][address(r)] = fee;
        c.fee = 0;
        _applied(c);
    }
}
