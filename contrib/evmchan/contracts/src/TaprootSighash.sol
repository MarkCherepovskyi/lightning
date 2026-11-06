// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BtcTx, TxView, Prevout} from "./BtcTx.sol";

/// @title BIP341 signature hash for key-path spends (ext_flag 0, no annex),
/// hash types DEFAULT, ALL, NONE, SINGLE, each optionally with ANYONECANPAY
/// (SPEC 4.3.9).
library TaprootSighash {
    /// sha256("TapSighash")
    bytes32 internal constant TAG = 0xf40a48df4b2a70c8b4924bf2654661ed3d95fd66a313eb87237597c628e4a031;

    uint8 internal constant DEFAULT = 0x00;
    uint8 internal constant NONE = 0x02;
    uint8 internal constant SINGLE = 0x03;
    uint8 internal constant ANYONECANPAY = 0x80;

    function validHashType(uint8 hashType) internal pure returns (bool) {
        return hashType <= 0x03 || (hashType >= 0x81 && hashType <= 0x83);
    }

    /// @notice The BIP341 signature message, prefixed with the epoch byte 0x00
    /// (what the BIP341 vectors call sigMsg).
    /// @dev hashType must satisfy validHashType, and for SINGLE `index` must
    /// have an output of the same index (BIP341 makes it a failure; the caller
    /// checks it).
    function sigMsg(bytes calldata raw, TxView memory t, Prevout[] calldata pv, uint256 index, uint8 hashType)
        internal
        pure
        returns (bytes memory)
    {
        return abi.encodePacked(_txPart(raw, t, pv, hashType), _inputPart(raw, t, pv, index, hashType));
    }

    /// epoch, hash_type, nVersion, nLockTime, the hashes the type commits to,
    /// spend_type
    function _txPart(bytes calldata raw, TxView memory t, Prevout[] calldata pv, uint8 hashType)
        private
        pure
        returns (bytes memory m)
    {
        uint8 base = hashType & 3;
        m = abi.encodePacked(uint8(0), hashType, raw[0:4], raw[raw.length - 4:raw.length]);
        if (hashType & ANYONECANPAY == 0) {
            bytes32 prevouts = BtcTx.shaPrevouts(raw, t);
            bytes32 amounts = BtcTx.shaAmounts(t, pv);
            bytes32 scripts = BtcTx.shaScripts(t, pv);
            m = abi.encodePacked(m, prevouts, amounts, scripts, BtcTx.shaSequences(raw, t));
        }
        if (base != NONE && base != SINGLE) m = abi.encodePacked(m, BtcTx.shaOutputs(raw, t));
        // spend_type = 2 * ext_flag + annex_present = 0
        m = abi.encodePacked(m, uint8(0));
    }

    /// The input (whole, or its index) and, for SINGLE, its output.
    function _inputPart(bytes calldata raw, TxView memory t, Prevout[] calldata pv, uint256 index, uint8 hashType)
        private
        pure
        returns (bytes memory m)
    {
        if (hashType & ANYONECANPAY != 0) {
            // outpoint, amount, scriptPubKey, nSequence
            uint256 o = BtcTx.inputOffset(t, index);
            m = abi.encodePacked(raw[o:o + 36], BtcTx.le64(pv[index].amount));
            m = abi.encodePacked(m, BtcTx.compactSizeBytes(pv[index].scriptPubKey.length), pv[index].scriptPubKey);
            m = abi.encodePacked(m, raw[o + 37:o + 41]);
        } else {
            m = abi.encodePacked(BtcTx.le32(uint32(index)));
        }
        if (hashType & 3 == SINGLE) m = abi.encodePacked(m, sha256(BtcTx.outBytes(raw, t, index)));
    }

    /// TapSighash = tagged_hash("TapSighash", sigMsg).
    function sigHash(bytes calldata raw, TxView memory t, Prevout[] calldata pv, uint256 index, uint8 hashType)
        internal
        pure
        returns (bytes32)
    {
        return sha256(abi.encodePacked(TAG, TAG, sigMsg(raw, t, pv, index, hashType)));
    }
}
