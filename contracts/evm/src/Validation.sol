// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BtcTx, TxView, Stack, Prevout} from "./BtcTx.sol";
import {Templates} from "./Templates.sol";
import {
    PrevoutCountMismatch,
    NullPrevout,
    AmountTooLarge,
    OutputsExceedInputs,
    TooManySigops,
    NonFinal,
    TimeRelativeLockUnsupported,
    RelativeLockNotMet,
    UnsupportedScriptPubKey
} from "./Errors.sol";

/// @title SPEC 4.3: whether a tx is valid at height h against the prevouts
/// its submitter claims, except for what needs the UTXO set (each prevout's
/// commitment in it, and no outpoint spent twice), which UtxoChain checks as
/// it spends the inputs, between `parse` and `check`.
library Validation {
    uint64 internal constant MAX_MONEY = 21_000_000 * 100_000_000;
    uint256 internal constant MAX_SIGOPS_COST = 16_000;
    /// SPEC 4.3.4 counts each input at 4: no template path has more
    /// (GetSigOpCount(true) of an HTLC script: CHECKSIG, 2 for the 2-of-2
    /// CHECKMULTISIG, CHECKSIG).
    uint256 internal constant SIGOPS_PER_INPUT = 4;
    uint32 internal constant LOCKTIME_THRESHOLD = 500_000_000;

    /// @param h the height the tx would be applied at
    /// @param mtp MTP(h) = T(max(h - 6, 0)), the median time past of block h
    /// @return t the parsed tx
    /// @return fee the sum of the inputs minus the sum of the outputs, in sats
    function validate(bytes calldata raw, Prevout[] calldata pv, uint256 h, uint256 mtp)
        internal
        view
        returns (TxView memory t, uint256 fee)
    {
        t = parse(raw, pv);
        fee = check(raw, t, pv, h, mtp);
    }

    /// 4.3.1, and 4.3.2's empty scriptSigs and prevout count.
    function parse(bytes calldata raw, Prevout[] calldata pv) internal pure returns (TxView memory t) {
        t = BtcTx.parse(raw);
        if (pv.length != t.nIn) revert PrevoutCountMismatch();
    }

    /// The rest of 4.3, for a tx from `parse`.
    function check(bytes calldata raw, TxView memory t, Prevout[] calldata pv, uint256 h, uint256 mtp)
        internal
        view
        returns (uint256 fee)
    {
        _checkNullPrevouts(raw, t);
        fee = _checkAmounts(raw, t, pv);
        _checkSigops(raw, t);
        _checkLockTime(raw, t, h, mtp);
        _checkRelativeLocks(raw, t, pv, h);
        _checkScripts(raw, t, pv);
    }

    /// 4.3.2: no input spends the null prevout (32 zero bytes, index 0xffffffff).
    function _checkNullPrevouts(bytes calldata raw, TxView memory t) private pure {
        for (uint256 i; i < t.nIn; ++i) {
            if (BtcTx.prevTxid(raw, t, i) == 0 && BtcTx.prevVout(raw, t, i) == type(uint32).max) revert NullPrevout(i);
        }
    }

    /// 4.3.3: every amount, and each sum, at most MAX_MONEY; inputs >= outputs.
    function _checkAmounts(bytes calldata raw, TxView memory t, Prevout[] calldata pv)
        private
        pure
        returns (uint256)
    {
        uint256 total;
        for (uint256 i; i < pv.length; ++i) {
            uint64 a = pv[i].amount;
            if (a > MAX_MONEY) revert AmountTooLarge();
            total += a;
        }
        uint256 out;
        uint256 n = BtcTx.nOut(t);
        for (uint256 j; j < n; ++j) {
            uint64 a = BtcTx.outAmount(raw, t, j);
            if (a > MAX_MONEY) revert AmountTooLarge();
            out += a;
        }
        if (total > MAX_MONEY || out > MAX_MONEY) revert AmountTooLarge();
        if (out > total) revert OutputsExceedInputs();
        return total - out;
    }

    /// 4.3.4: 4 * (legacy sigops in all output scripts) + 4 * (input count) <= 16,000.
    function _checkSigops(bytes calldata raw, TxView memory t) private pure {
        uint256 legacy;
        uint256 n = BtcTx.nOut(t);
        for (uint256 j; j < n; ++j) {
            legacy += legacySigops(BtcTx.outScript(raw, t, j));
        }
        if (4 * legacy + SIGOPS_PER_INPUT * t.nIn > MAX_SIGOPS_COST) revert TooManySigops();
    }

    /// Bitcoin's GetSigOpCount(false): CHECKSIG(VERIFY) 1, CHECKMULTISIG(VERIFY)
    /// 20, pushed data skipped, and the count stops at a push that runs past
    /// the end of the script (GetOp fails).
    function legacySigops(bytes calldata s) internal pure returns (uint256 n) {
        assembly ("memory-safe") {
            let p := s.offset
            let end := add(p, s.length)
            for {} lt(p, end) {} {
                let op := byte(0, calldataload(p))
                p := add(p, 1)
                if iszero(op) { continue } // OP_0
                if gt(op, 0x4e) {
                    // not a push: CHECKSIG(VERIFY) 0xac/0xad count 1,
                    // CHECKMULTISIG(VERIFY) 0xae/0xaf count 20
                    if and(gt(op, 0xab), lt(op, 0xb0)) { n := add(n, sub(20, mul(19, lt(op, 0xae)))) }
                    continue
                }
                {
                    // a push: 0x01-0x4b push that many bytes; PUSHDATA1/2/4
                    // (0x4c-0x4e) give the size in 1, 2 or 4 bytes LE
                    let size := op
                    if gt(op, 0x4b) {
                        let k := 1
                        if eq(op, 0x4d) { k := 2 }
                        if eq(op, 0x4e) { k := 4 }
                        if gt(add(p, k), end) { break }
                        let w := calldataload(p)
                        size := 0
                        for { let j := 0 } lt(j, k) { j := add(j, 1) } {
                            size := or(size, shl(mul(8, j), byte(j, w)))
                        }
                        p := add(p, k)
                    }
                    if gt(size, sub(end, p)) { break }
                    p := add(p, size)
                }
            }
        }
    }

    /// 4.3.5 (BIP113): final if every sequence is 0xffffffff, or nLockTime is a
    /// height below h, or a time below MTP(h).
    function _checkLockTime(bytes calldata raw, TxView memory t, uint256 h, uint256 mtp) private pure {
        uint32 lt = t.locktime;
        if (lt < LOCKTIME_THRESHOLD ? lt < h : lt < mtp) return;
        for (uint256 i; i < t.nIn; ++i) {
            if (BtcTx.sequence(raw, t, i) != Templates.SEQUENCE_FINAL) revert NonFinal();
        }
    }

    /// 4.3.6 (BIP68), for version >= 2: an input whose sequence has bit 31
    /// clear is a time lock (unsupported) if bit 22 is set, and otherwise needs
    /// h - prevout.height >= sequence & 0xffff.
    function _checkRelativeLocks(bytes calldata raw, TxView memory t, Prevout[] calldata pv, uint256 h)
        private
        pure
    {
        if (t.version < 2) return;
        for (uint256 i; i < t.nIn; ++i) {
            uint32 seq = BtcTx.sequence(raw, t, i);
            if (seq & Templates.SEQ_DISABLE != 0) continue;
            if (seq & Templates.SEQ_TYPE != 0) revert TimeRelativeLockUnsupported(i);
            if (h < uint256(pv[i].height) + (seq & Templates.SEQ_MASK)) revert RelativeLockNotMet(i);
        }
    }

    /// 4.3.7: each input's scriptPubKey is P2WPKH, P2TR or P2WSH, and its
    /// witness satisfies it.
    function _checkScripts(bytes calldata raw, TxView memory t, Prevout[] calldata pv) private view {
        for (uint256 i; i < t.nIn; ++i) {
            // Nothing allocated while checking one input is used afterwards
            // (the lazily computed hashes are stored in t's own fields), so
            // the memory is reused for the next input: without this, a tx
            // with hundreds of inputs pays quadratic memory gas.
            uint256 freeMem;
            assembly {
                freeMem := mload(0x40)
            }
            bytes calldata spk = pv[i].scriptPubKey;
            Stack memory w = BtcTx.stack(raw, t, i);
            if (spk.length == 22 && spk[0] == 0x00 && spk[1] == 0x14) {
                Templates.p2wpkh(raw, t, Templates.Input(i, pv[i].amount, w), bytes20(spk[2:22]));
            } else if (spk.length == 34 && spk[0] == 0x00 && spk[1] == 0x20) {
                Templates.p2wsh(raw, t, Templates.Input(i, pv[i].amount, w), bytes32(spk[2:34]));
            } else if (spk.length == 34 && spk[0] == 0x51 && spk[1] == 0x20) {
                Templates.p2tr(raw, t, pv, i, bytes32(spk[2:34]), w);
            } else {
                revert UnsupportedScriptPubKey(i);
            }
            assembly {
                mstore(0x40, freeMem)
            }
        }
    }
}
