// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BtcTx, Tx, TxOut} from "s2/BtcTx.sol";
import {TaprootSighash} from "s2/TaprootSighash.sol";
import {TaprootKeyPath} from "s2/TaprootKeyPath.sol";
import {Bip143} from "./Bip143.sol";
import {SpendCtx, Templates, BadSignature, MissingHint} from "./Templates.sol";

/// An output being spent, as the real contract would take it from calldata
/// and check against its UTXO commitment.
struct Prevout {
    uint64 amount;
    bytes scriptPubKey;
    uint32 height; // the height it was created at
}

error PrevoutCountMismatch();
error PrevoutFromFuture(uint256 input);
error DuplicateInput(uint256 input);
error NullPrevout(uint256 input);
error TxTooLarge();
error TooManySigops();
error AmountTooLarge();
error OutputsExceedInputs();
error NonFinal();
error RelativeLockNotMet(uint256 input);
error TimeRelativeLockUnsupported(uint256 input);
error ScriptSigNotEmpty(uint256 input);
error UnsupportedScriptPubKey(uint256 input);
error UnusedHints();

/// @title Validation of a signed Bitcoin transaction against the outputs it
/// spends, at a given height of the virtual chain. Spike S3.
/// @notice Spendable outputs: P2WPKH, P2TR key path, and P2WSH with the CLN
/// templates of Templates.sol. Time: block h has the timestamp
/// genesisTime + h * interval (as in spike S1's toy chain); that timestamp,
/// not BIP113's median time past, is what time-based nLockTime is compared to.
/// Time-based BIP68 relative locks are rejected (CLN never uses them).
/// Size and sigops: a tx is rejected above 100,000 serialized bytes (so its
/// weight is under 400,000, the standard limit, well under the 4M block
/// limit) or above the standard 16,000 sigop cost, counting outputs the
/// legacy way (x4) and each input at its template's maximum (4). Coinbase
/// outputs are assumed never to be prevouts (the virtual chain's coinbases
/// are dummies that are not added to the UTXO set).
contract SegwitValidator {
    uint64 internal constant MAX_MONEY = 21_000_000 * 100_000_000;
    uint32 internal constant LOCKTIME_THRESHOLD = 500_000_000;
    uint256 internal constant MAX_TX_SIZE = 100_000;
    uint256 internal constant MAX_SIGOPS_COST = 16_000;
    uint256 internal constant MAX_INPUT_SIGOPS = 4; // the HTLC scripts: CHECKSIG + 2-of-2 + CHECKSIG

    uint256 public immutable genesisTime;
    uint256 public immutable interval;

    constructor(uint256 genesisTime_, uint256 interval_) {
        genesisTime = genesisTime_;
        interval = interval_;
    }

    /// @notice Reverts unless `rawTx` may be applied at `height`.
    /// @param prevouts the outputs spent, in input order
    /// @param hints the signature hints, in the order the signatures are
    ///        checked (see Ecdsa.sol and Schnorr.sol); all must be consumed
    /// @return txid in internal byte order (as in an outpoint)
    /// @return fee inputs minus outputs, in sats
    function validateTx(bytes calldata rawTx, Prevout[] calldata prevouts, bytes calldata hints, uint32 height)
        external
        view
        returns (bytes32 txid, uint64 fee)
    {
        if (rawTx.length > MAX_TX_SIZE) revert TxTooLarge();
        (Tx memory t, bytes[][] memory witnesses, bytes32 id) = BtcTx.parse(rawTx);
        Prevout[] memory pv = prevouts;
        uint256 n = t.inputs.length;
        if (pv.length != n) revert PrevoutCountMismatch();

        _checkSigops(t);
        fee = _checkAmounts(t, pv);
        _checkInputsDistinct(t);
        _checkLockTime(t, height);
        _checkRelativeLocks(t, pv, height);

        SpendCtx memory c;
        c.t = t;
        c.hints = hints;
        TxOut[] memory spent;
        TaprootSighash.Precomputed memory tpc;
        for (uint256 i; i < n; ++i) {
            if (t.inputs[i].scriptSig.length != 0) revert ScriptSigNotEmpty(i);
            bytes memory spk = pv[i].scriptPubKey;
            c.index = i;
            c.amount = pv[i].amount;
            if (spk.length == 22 && spk[0] == 0x00 && spk[1] == 0x14) {
                Templates.checkP2wpkh(c, bytes20(_word(spk, 2)), witnesses[i]);
            } else if (spk.length == 34 && spk[0] == 0x00 && spk[1] == 0x20) {
                Templates.checkP2wsh(c, _word(spk, 2), witnesses[i]);
            } else if (spk.length == 34 && spk[0] == 0x51 && spk[1] == 0x20) {
                if (spent.length == 0) {
                    spent = _txOuts(pv);
                    tpc = TaprootSighash.precompute(t, spent);
                }
                if (c.cursor + 32 > hints.length) revert MissingHint(i);
                uint256 ry = uint256(bytes32(hints[c.cursor:c.cursor + 32]));
                c.cursor += 32;
                if (!TaprootKeyPath.verifyInput(t, spent, tpc, i, witnesses[i], ry)) revert BadSignature(i);
            } else {
                revert UnsupportedScriptPubKey(i);
            }
        }
        if (c.cursor != hints.length) revert UnusedHints();
        txid = id;
    }

    /// The timestamp of block `height` of the virtual chain.
    function blockTime(uint32 height) public view returns (uint256) {
        return genesisTime + uint256(height) * interval;
    }

    function _checkAmounts(Tx memory t, Prevout[] memory pv) private pure returns (uint64) {
        uint256 total;
        for (uint256 i; i < pv.length; ++i) {
            if (pv[i].amount > MAX_MONEY) revert AmountTooLarge();
            total += pv[i].amount;
        }
        uint256 out;
        for (uint256 i; i < t.outputs.length; ++i) {
            if (t.outputs[i].amount > MAX_MONEY) revert AmountTooLarge();
            out += t.outputs[i].amount;
        }
        if (total > MAX_MONEY || out > MAX_MONEY) revert AmountTooLarge();
        if (out > total) revert OutputsExceedInputs();
        return uint64(total - out);
    }

    function _checkInputsDistinct(Tx memory t) private pure {
        for (uint256 i; i < t.inputs.length; ++i) {
            if (t.inputs[i].prevTxid == 0 && t.inputs[i].prevVout == type(uint32).max) revert NullPrevout(i);
        }
        for (uint256 i = 1; i < t.inputs.length; ++i) {
            for (uint256 j; j < i; ++j) {
                if (t.inputs[i].prevTxid == t.inputs[j].prevTxid && t.inputs[i].prevVout == t.inputs[j].prevVout) {
                    revert DuplicateInput(i);
                }
            }
        }
    }

    /// Sigop cost bound: outputs counted as GetLegacySigOpCount does
    /// (CHECKSIG(VERIFY) 1, CHECKMULTISIG(VERIFY) 20, pushes skipped, stop at a
    /// truncated push), times 4; plus MAX_INPUT_SIGOPS per input.
    function _checkSigops(Tx memory t) private pure {
        uint256 legacy;
        for (uint256 i; i < t.outputs.length; ++i) {
            legacy += _legacySigops(t.outputs[i].scriptPubKey);
        }
        if (legacy * 4 + t.inputs.length * MAX_INPUT_SIGOPS > MAX_SIGOPS_COST) revert TooManySigops();
    }

    function _legacySigops(bytes memory s) private pure returns (uint256 n) {
        uint256 i;
        while (i < s.length) {
            uint8 op = uint8(s[i++]);
            if (op == 0xac || op == 0xad) {
                n += 1;
            } else if (op == 0xae || op == 0xaf) {
                n += 20;
            } else if (op >= 0x01 && op <= 0x4e) {
                uint256 len = op;
                if (op >= 0x4c) {
                    uint256 k = op == 0x4c ? 1 : op == 0x4d ? 2 : 4;
                    if (i + k > s.length) break;
                    len = 0;
                    for (uint256 j; j < k; ++j) {
                        len |= uint256(uint8(s[i + j])) << (8 * j);
                    }
                    i += k;
                }
                if (len > s.length - i) break;
                i += len;
            }
        }
    }

    /// IsFinalTx: nLockTime 0, or below the height (or the block's time), or
    /// every nSequence final.
    function _checkLockTime(Tx memory t, uint32 height) private view {
        uint32 lt = t.locktime;
        if (lt == 0) return;
        if (lt < LOCKTIME_THRESHOLD ? lt < height : lt < blockTime(height)) return;
        for (uint256 i; i < t.inputs.length; ++i) {
            if (t.inputs[i].sequence != Templates.SEQUENCE_FINAL) revert NonFinal();
        }
    }

    /// BIP68 for version >= 2: an input whose nSequence has the disable bit
    /// clear needs its output to be (nSequence & 0xffff) blocks old.
    function _checkRelativeLocks(Tx memory t, Prevout[] memory pv, uint32 height) private pure {
        for (uint256 i; i < pv.length; ++i) {
            if (pv[i].height > height) revert PrevoutFromFuture(i);
            uint32 seq = t.inputs[i].sequence;
            if (t.version < 2 || seq & Templates.SEQ_DISABLE != 0) continue;
            if (seq & Templates.SEQ_TYPE != 0) revert TimeRelativeLockUnsupported(i);
            if (uint256(height) < uint256(pv[i].height) + (seq & Templates.SEQ_MASK)) revert RelativeLockNotMet(i);
        }
    }

    function _txOuts(Prevout[] memory pv) private pure returns (TxOut[] memory outs) {
        outs = new TxOut[](pv.length);
        for (uint256 i; i < pv.length; ++i) {
            outs[i] = TxOut(pv[i].amount, pv[i].scriptPubKey);
        }
    }

    function _word(bytes memory b, uint256 off) private pure returns (bytes32 w) {
        assembly ("memory-safe") {
            w := mload(add(add(b, 32), off))
        }
    }
}
