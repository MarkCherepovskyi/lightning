// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChainBase} from "./ChainBase.sol";
import {UtxoChainHarness} from "./Harness.sol";
import {UtxoChain} from "../src/UtxoChain.sol";
import {Prevout} from "../src/BtcTx.sol";
import {Synthetic} from "./generated/Synthetic.sol";
import {Case} from "./Types.sol";
import "../src/Errors.sol" as E;

/// Takes a fee by submitting, then withdraws it and re-enters `withdraw` from
/// its receive function.
contract Reenterer {
    UtxoChain internal immutable chain;
    bool internal immutable propagate; // let the inner revert through (else catch it)
    uint256 public received;
    uint256 public calls;
    bytes public innerError;

    constructor(UtxoChain c, bool propagate_) {
        chain = c;
        propagate = propagate_;
    }

    function submit(bytes calldata raw, Prevout[] calldata pv) external {
        chain.submit(raw, pv);
    }

    function attack() external {
        chain.withdraw();
    }

    receive() external payable {
        received += msg.value;
        calls += 1;
        if (calls > 1) return;
        if (propagate) {
            chain.withdraw();
        } else {
            try chain.withdraw() {} catch (bytes memory e) {
                innerError = e;
            }
        }
    }
}

contract ChainTest is ChainBase {
    uint256 constant GENESIS = Synthetic.GENESIS_TIME;
    uint256 constant INTERVAL = Synthetic.INTERVAL;
    uint64 constant MAX_MONEY = 21_000_000 * 100_000_000;

    function testConstructor() public {
        vm.roll(1234);
        _deploy(GENESIS, INTERVAL);
        assertEq(chain.GENESIS_TIME(), GENESIS, "GENESIS_TIME");
        assertEq(chain.INTERVAL(), INTERVAL, "INTERVAL");
        assertEq(chain.DEPLOY_BLOCK(), 1234, "DEPLOY_BLOCK");
        assertEq(chain.depositNonce(), 0, "depositNonce");
        vm.expectRevert(abi.encodeWithSelector(E.BadInterval.selector));
        new UtxoChainHarness(0);
    }

    function testHeightFollowsTheClock() public {
        _deploy(GENESIS, INTERVAL);
        assertEq(chain.height(), 0, "height at deploy");
        vm.warp(GENESIS + INTERVAL - 1);
        assertEq(chain.height(), 0, "just before 1");
        vm.warp(GENESIS + 5 * INTERVAL);
        assertEq(chain.height(), 5, "5");
        vm.warp(GENESIS + 6 * INTERVAL - 1);
        assertEq(chain.height(), 5, "still 5");
        vm.warp(GENESIS + (uint256(type(uint32).max) + 1) * INTERVAL);
        vm.expectRevert(abi.encodeWithSelector(E.HeightOverflow.selector));
        chain.height();
    }

    function testNothingAppliedAtHeightZero() public {
        _deploy(GENESIS, INTERVAL);
        vm.expectRevert(abi.encodeWithSelector(E.TooEarly.selector));
        chain.deposit{value: SAT}(hex"0014aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        Case memory c = Synthetic.valid(0);
        vm.expectRevert(abi.encodeWithSelector(E.TooEarly.selector));
        chain.submit(c.rawTx, c.prevouts);
        _at(1);
        chain.deposit{value: SAT}(hex"0014aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    }

    /// SPEC 3.3, byte for byte: the deposit txs and txids match the generator's
    /// independent build (for a contract at DEPOSIT_ADDRESS on chain 31337),
    /// and a tx spending deposit 0 is then valid.
    function testDepositTxsByteExact() public {
        _deploy(GENESIS, INTERVAL);
        vm.chainId(31337);
        vm.etch(Synthetic.DEPOSIT_ADDRESS, address(chain).code);
        chain = UtxoChainHarness(payable(Synthetic.DEPOSIT_ADDRESS));
        _checkInvariant();
        _at(5);
        for (uint256 i; i < Synthetic.DEPOSIT_COUNT; ++i) {
            (uint64 amount, bytes memory spk, bytes memory raw, bytes32 txid) = Synthetic.depositTx(i);
            vm.expectEmit(true, true, false, true, address(chain));
            emit TxApplied(5, txid, raw);
            bytes32 got = chain.deposit{value: uint256(amount) * SAT}(spk);
            assertEq(got, txid, "deposit txid");
            assertEq(sha256(abi.encodePacked(sha256(raw))), txid, "txid is the double SHA256");
            assertTrue(chain.isUnspent(txid, 0, amount, spk, 5), "deposit output");
            assertEq(chain.depositNonce(), i + 1, "depositNonce");
            outs.push(Out(txid, 0, amount, spk, 5));
            _checkInvariant();
        }
        Case memory c = Synthetic.depositSpend();
        _submitValid(c);
    }

    function testDepositRules() public {
        _deploy(GENESIS, INTERVAL);
        _at(1);
        bytes memory spk = hex"0014aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        bytes memory bad = abi.encodeWithSelector(E.BadDepositValue.selector);
        vm.expectRevert(bad);
        chain.deposit{value: 0}(spk);
        vm.expectRevert(bad);
        chain.deposit{value: SAT - 1}(spk);
        vm.expectRevert(bad);
        chain.deposit{value: SAT + 1}(spk);
        vm.expectRevert(bad);
        chain.deposit{value: (uint256(MAX_MONEY) + 1) * SAT}(spk);
        _deposit(uint256(MAX_MONEY) * SAT, spk);

        bytes memory badScript = abi.encodeWithSelector(E.BadDepositScript.selector);
        vm.expectRevert(badScript);
        chain.deposit{value: SAT}(new bytes(10_001));
        vm.expectRevert(badScript);
        chain.deposit{value: SAT}(hex"60141111111111111111111111111111111111111111"); // exit
        vm.expectRevert(badScript);
        chain.deposit{value: SAT}(hex"6a"); // OP_RETURN
        vm.expectRevert(badScript);
        chain.deposit{value: SAT}(hex"6a0401020304");
        _deposit(SAT, new bytes(10_000));
        _deposit(SAT, ""); // never spendable, but allowed (SPEC 4.2)
        _deposit(SAT, hex"601311111111111111111111111111111111111111"); // v16, 19 bytes: not an exit
        _deposit(SAT, hex"6014111111111111111111111111111111111111111100"); // 21 bytes: not an exit
    }

    function _deposit(uint256 value, bytes memory spk) internal {
        uint32 h = chain.height();
        bytes32 txid = chain.deposit{value: value}(spk);
        uint64 amount = uint64(value / SAT);
        assertTrue(chain.isUnspent(txid, 0, amount, spk, h), "deposit stored");
        outs.push(Out(txid, 0, amount, spk, h));
        _checkInvariant();
    }

    function testUtxoKeyAndIsUnspent() public {
        _deploy(GENESIS, INTERVAL);
        bytes32 txid = keccak256("tx");
        assertEq(chain.utxoKey(txid, 7), keccak256(abi.encodePacked(txid, uint32(7))), "utxoKey");
        Prevout memory p = Prevout(1234, hex"5120aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 3);
        _seed(txid, 7, p);
        assertEq(
            chain.utxo(chain.utxoKey(txid, 7)), keccak256(abi.encodePacked(p.amount, p.scriptPubKey, p.height)), "commitment"
        );
        assertTrue(chain.isUnspent(txid, 7, 1234, p.scriptPubKey, 3), "unspent");
        assertTrue(!chain.isUnspent(txid, 7, 1235, p.scriptPubKey, 3), "other amount");
        assertTrue(!chain.isUnspent(txid, 7, 1234, p.scriptPubKey, 4), "other height");
        assertTrue(!chain.isUnspent(txid, 7, 1234, hex"5120", 3), "other script");
        assertTrue(!chain.isUnspent(txid, 6, 1234, p.scriptPubKey, 3), "other vout");
    }

    /// Exits (SPEC 4.4) are credited, not stored; OP_RETURN is burned; fees go
    /// to the submitter; withdraw pays out once.
    function testExitsFeesAndWithdraw() public {
        _deploy(GENESIS, INTERVAL);
        Case memory c = Synthetic.valid(Synthetic.EXIT_CASE);
        _submitValid(c);
        address to;
        uint256 amount;
        for (uint256 j; j < c.outScripts.length; ++j) {
            if (_isExit(c.outScripts[j])) {
                to = _exitAddress(c.outScripts[j]);
                amount = uint256(c.outAmounts[j]) * SAT;
            }
        }
        assertTrue(to != address(0) && amount != 0, "case has an exit");
        assertEq(chain.withdrawable(to), amount, "exit credited");
        uint256 before = to.balance;
        vm.prank(to);
        chain.withdraw();
        assertEq(to.balance - before, amount, "exit paid");
        owed[address(chain)][to] = 0;
        _checkInvariant();
        vm.prank(to);
        vm.expectRevert(abi.encodeWithSelector(E.NothingToWithdraw.selector));
        chain.withdraw();

        uint256 fee = uint256(c.fee) * SAT;
        assertTrue(fee != 0, "case has a fee");
        before = address(this).balance;
        chain.withdraw();
        assertEq(address(this).balance - before, fee, "fee paid");
        owed[address(chain)][address(this)] = 0;
        _checkInvariant();
    }

    /// A recipient that re-enters withdraw gets paid once: the balance is
    /// zeroed before the call (SPEC 4.2).
    function testReentrantWithdraw() public {
        _deploy(GENESIS, INTERVAL);
        Reenterer r = new Reenterer(chain, false);
        uint256 fee = _feeTo(r);
        uint256 bal = address(chain).balance;
        r.attack();
        assertEq(r.received(), fee, "paid once");
        assertEq(r.calls(), 1, "one payment");
        assertEq(r.innerError(), abi.encodeWithSelector(E.NothingToWithdraw.selector), "re-entry refused");
        assertEq(chain.withdrawable(address(r)), 0, "zeroed");
        assertEq(bal - address(chain).balance, fee, "contract paid the fee once");
        owed[address(chain)][address(r)] = 0;
        _checkInvariant();
    }

    /// A recipient whose receive reverts gets WithdrawFailed and keeps its balance.
    function testWithdrawToRevertingRecipient() public {
        _deploy(GENESIS, INTERVAL);
        Reenterer r = new Reenterer(chain, true);
        uint256 fee = _feeTo(r);
        vm.expectRevert(abi.encodeWithSelector(E.WithdrawFailed.selector));
        r.attack();
        assertEq(chain.withdrawable(address(r)), fee, "still owed");
        _checkInvariant();
    }

    function _feeTo(Reenterer r) internal returns (uint256 fee) {
        Case memory c = Synthetic.valid(0);
        for (uint256 i; i < c.prevouts.length; ++i) {
            _seed(c.inTxids[i], c.inVouts[i], c.prevouts[i]);
        }
        _at(c.height);
        r.submit(c.rawTx, c.prevouts);
        fee = uint256(c.fee) * SAT;
        assertTrue(fee != 0, "fee");
        assertEq(chain.withdrawable(address(r)), fee, "fee to the submitter");
        _payee(address(r));
        owed[address(chain)][address(r)] = fee;
        // the ledger: c's inputs are spent and its outputs exist, with no fee to this contract
        c.fee = 0;
        _applied(c);
    }
}
