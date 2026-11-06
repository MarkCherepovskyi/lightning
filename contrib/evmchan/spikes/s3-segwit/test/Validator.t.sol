// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {Vectors, Case, Bad} from "./Vectors.sol";
import {SegwitValidator, Prevout} from "../src/SegwitValidator.sol";

/// Every real CLN tx (and every synthetic spend) validates at its height and
/// at its lowest valid height, fails one block earlier, and every tampered
/// copy fails with the expected error.
contract ValidatorTest is TestBase {
    SegwitValidator v = new SegwitValidator(Vectors.GENESIS_TIME, Vectors.INTERVAL);

    function testRealTxsValidate() public view {
        for (uint256 i; i < Vectors.REAL_COUNT; ++i) {
            _valid(Vectors.real(i));
        }
    }

    function testSyntheticTxsValidate() public view {
        for (uint256 i; i < Vectors.SYNTHETIC_COUNT; ++i) {
            _valid(Vectors.synthetic(i));
        }
    }

    function testRealTxsFailOneBlockEarlier() public {
        for (uint256 i; i < Vectors.REAL_COUNT; ++i) {
            _early(Vectors.real(i));
        }
    }

    function testSyntheticTxsFailOneBlockEarlier() public {
        for (uint256 i; i < Vectors.SYNTHETIC_COUNT; ++i) {
            _early(Vectors.synthetic(i));
        }
    }

    function testTamperedTxsFail() public view {
        for (uint256 i; i < Vectors.BAD_COUNT; ++i) {
            Bad memory b = Vectors.bad(i);
            (bool ok, bytes memory ret) =
                address(v).staticcall(abi.encodeCall(v.validateTx, (b.rawTx, b.prevouts, b.hints, b.height)));
            assertTrue(!ok, b.name);
            assertEq(ret, b.err, b.name);
        }
    }

    /// Fewer or more prevouts than inputs.
    function testPrevoutCount() public {
        Case memory c = Vectors.real(0);
        Prevout[] memory none = new Prevout[](0);
        vm.expectRevert(abi.encodeWithSignature("PrevoutCountMismatch()"));
        v.validateTx(c.rawTx, none, c.hints, c.height);
    }

    /// Hints cannot make a forged signature pass: the commitment tx with output
    /// 0 changed (so both funding signatures are invalid) is rejected whatever
    /// hints are given for its first signature.
    function testFuzzHintsCannotForge(uint8 recId, uint256 keyY) public view {
        Case memory c = Vectors.real(4); // #7 commitment
        bytes memory raw = c.rawTx;
        uint256 off = _output0AmountOffset(raw);
        raw[off] = bytes1(uint8(raw[off]) ^ 1);
        bytes memory hints = c.hints;
        hints[0] = bytes1(recId);
        assembly ("memory-safe") {
            mstore(add(hints, 33), keyY)
        }
        try v.validateTx(raw, c.prevouts, hints, c.height) {
            revert("forged tx accepted");
        } catch {}
    }

    /// Same, with the true key Y and each recovery id: still BadSignature.
    function testBothRecoveryIdsRejectForgery() public {
        Case memory c = Vectors.real(4);
        bytes memory raw = c.rawTx;
        uint256 off = _output0AmountOffset(raw);
        raw[off] = bytes1(uint8(raw[off]) ^ 1);
        for (uint8 rec; rec < 2; ++rec) {
            bytes memory hints = c.hints;
            hints[0] = bytes1(rec);
            vm.expectRevert(abi.encodeWithSignature("BadSignature(uint256)", uint256(0)));
            v.validateTx(raw, c.prevouts, hints, c.height);
        }
    }

    /// A wrong hint makes an honest tx fail (unless it is the right hint).
    function testFuzzWrongHintFailsHonestTx(uint8 recId, uint256 keyY) public view {
        Case memory c = Vectors.real(4);
        bytes memory hints = c.hints;
        uint256 goodY = uint256(_word(hints, 1));
        if (recId == uint8(hints[0]) && keyY == goodY) return;
        hints[0] = bytes1(recId);
        assembly ("memory-safe") {
            mstore(add(hints, 33), keyY)
        }
        try v.validateTx(c.rawTx, c.prevouts, hints, c.height) {
            revert("wrong hint accepted");
        } catch {}
    }

    function _valid(Case memory c) internal view {
        (bytes32 txid, uint64 fee) = v.validateTx(c.rawTx, c.prevouts, c.hints, c.height);
        assertEq(txid, c.txid, c.name);
        assertEq(fee, c.fee, c.name);
        v.validateTx(c.rawTx, c.prevouts, c.hints, c.minHeight);
    }

    function _early(Case memory c) internal {
        vm.expectRevert(c.earlyError);
        v.validateTx(c.rawTx, c.prevouts, c.hints, c.minHeight - 1);
    }

    /// Offset of the first byte of output 0's amount in a segwit tx with
    /// one input whose scriptSig is empty.
    function _output0AmountOffset(bytes memory raw) internal pure returns (uint256) {
        // version 4, marker+flag 2, input count 1, outpoint 36, scriptSig len 1, sequence 4, output count 1
        require(raw[6] == 0x01 && raw[6 + 1 + 36] == 0x00);
        return 4 + 2 + 1 + 36 + 1 + 4 + 1;
    }

    function _word(bytes memory b, uint256 off) internal pure returns (bytes32 w) {
        assembly ("memory-safe") {
            w := mload(add(add(b, 32), off))
        }
    }
}
