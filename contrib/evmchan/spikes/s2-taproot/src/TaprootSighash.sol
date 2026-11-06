// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Tx, TxIn, TxOut} from "./BtcTx.sol";

/// @title BIP341 signature message and TapSighash for key-path spends
/// (ext_flag = 0, no annex).
library TaprootSighash {
    /// sha256("TapSighash")
    bytes32 internal constant TAG = 0xf40a48df4b2a70c8b4924bf2654661ed3d95fd66a313eb87237597c628e4a031;

    uint8 internal constant DEFAULT = 0x00;
    uint8 internal constant ALL = 0x01;
    uint8 internal constant NONE = 0x02;
    uint8 internal constant SINGLE = 0x03;
    uint8 internal constant ANYONECANPAY = 0x80;

    error BadHashType(uint8 hashType);
    error BadInputIndex();
    error SpentOutputsMismatch();
    error NoMatchingOutput();

    /// The per-transaction hashes (single SHA256, as BIP341 defines them).
    /// Compute once per transaction and reuse for every input.
    struct Precomputed {
        bytes32 prevouts;
        bytes32 amounts;
        bytes32 scriptPubKeys;
        bytes32 sequences;
        bytes32 outputs;
    }

    function precompute(Tx memory t, TxOut[] memory spent) internal pure returns (Precomputed memory pc) {
        uint256 n = t.inputs.length;
        if (spent.length != n) revert SpentOutputsMismatch();

        bytes memory b = new bytes(36 * n);
        for (uint256 i; i < n; ++i) {
            TxIn memory input = t.inputs[i];
            _put(b, 36 * i, input.prevTxid);
            _put(b, 36 * i + 32, le32(input.prevVout));
        }
        pc.prevouts = sha256(b);

        b = new bytes(8 * n);
        for (uint256 i; i < n; ++i) {
            _put(b, 8 * i, le64(spent[i].amount));
        }
        pc.amounts = sha256(b);

        pc.scriptPubKeys = sha256(_serializeScripts(spent));

        b = new bytes(4 * n);
        for (uint256 i; i < n; ++i) {
            _put(b, 4 * i, le32(t.inputs[i].sequence));
        }
        pc.sequences = sha256(b);

        pc.outputs = sha256(_serializeOutputs(t.outputs));
    }

    /// The BIP341 signature message, prefixed with the epoch byte 0x00 (the
    /// form the BIP341 test vectors call sigMsg).
    function sigMsg(
        Tx memory t,
        TxOut[] memory spent,
        Precomputed memory pc,
        uint256 inputIndex,
        uint8 hashType
    ) internal pure returns (bytes memory m) {
        if (!validHashType(hashType)) revert BadHashType(hashType);
        if (inputIndex >= t.inputs.length) revert BadInputIndex();
        if (spent.length != t.inputs.length) revert SpentOutputsMismatch();
        uint8 base = hashType & 3;
        bool acp = hashType & ANYONECANPAY != 0;

        m = abi.encodePacked(uint8(0), hashType, le32(t.version), le32(t.locktime));
        if (!acp) {
            m = abi.encodePacked(m, pc.prevouts, pc.amounts, pc.scriptPubKeys, pc.sequences);
        }
        if (base != NONE && base != SINGLE) {
            m = abi.encodePacked(m, pc.outputs);
        }
        // spend_type = 2 * ext_flag + annex_present = 0
        m = abi.encodePacked(m, uint8(0));
        if (acp) {
            TxIn memory input = t.inputs[inputIndex];
            TxOut memory prev = spent[inputIndex];
            m = abi.encodePacked(
                m,
                input.prevTxid,
                le32(input.prevVout),
                le64(prev.amount),
                compactSize(prev.scriptPubKey.length),
                prev.scriptPubKey,
                le32(input.sequence)
            );
        } else {
            m = abi.encodePacked(m, le32(uint32(inputIndex)));
        }
        if (base == SINGLE) {
            // BIP341: failure, unlike the BIP143 "1" quirk.
            if (inputIndex >= t.outputs.length) revert NoMatchingOutput();
            TxOut memory out = t.outputs[inputIndex];
            m = abi.encodePacked(
                m, sha256(abi.encodePacked(le64(out.amount), compactSize(out.scriptPubKey.length), out.scriptPubKey))
            );
        }
    }

    /// TapSighash = tagged_hash("TapSighash", sigMsg).
    function sigHash(
        Tx memory t,
        TxOut[] memory spent,
        Precomputed memory pc,
        uint256 inputIndex,
        uint8 hashType
    ) internal pure returns (bytes32) {
        return sha256(abi.encodePacked(TAG, TAG, sigMsg(t, spent, pc, inputIndex, hashType)));
    }

    function validHashType(uint8 hashType) internal pure returns (bool) {
        return hashType <= 0x03 || (hashType >= 0x81 && hashType <= 0x83);
    }

    function le32(uint32 x) internal pure returns (bytes4) {
        x = ((x & 0xff00ff00) >> 8) | ((x & 0x00ff00ff) << 8);
        x = (x >> 16) | (x << 16);
        return bytes4(x);
    }

    function le64(uint64 x) internal pure returns (bytes8) {
        x = ((x & 0xff00ff00ff00ff00) >> 8) | ((x & 0x00ff00ff00ff00ff) << 8);
        x = ((x & 0xffff0000ffff0000) >> 16) | ((x & 0x0000ffff0000ffff) << 16);
        x = (x >> 32) | (x << 32);
        return bytes8(x);
    }

    function compactSize(uint256 n) internal pure returns (bytes memory) {
        if (n < 0xfd) return abi.encodePacked(uint8(n));
        if (n <= 0xffff) return abi.encodePacked(uint8(0xfd), bytes2(uint16(n) >> 8 | uint16(n) << 8));
        if (n <= 0xffffffff) return abi.encodePacked(uint8(0xfe), le32(uint32(n)));
        return abi.encodePacked(uint8(0xff), le64(uint64(n)));
    }

    function _serializeScripts(TxOut[] memory outs) private pure returns (bytes memory b) {
        for (uint256 i; i < outs.length; ++i) {
            b = abi.encodePacked(b, compactSize(outs[i].scriptPubKey.length), outs[i].scriptPubKey);
        }
    }

    function _serializeOutputs(TxOut[] memory outs) private pure returns (bytes memory b) {
        for (uint256 i; i < outs.length; ++i) {
            b = abi.encodePacked(
                b, le64(outs[i].amount), compactSize(outs[i].scriptPubKey.length), outs[i].scriptPubKey
            );
        }
    }

    /// Writes a left-aligned word at b[off]. It may write up to 31 bytes past
    /// what the caller means; callers write in increasing order so later writes
    /// fix that, and the excess past the end of b lands in unallocated memory.
    function _put(bytes memory b, uint256 off, bytes32 w) private pure {
        assembly ("memory-safe") {
            mstore(add(add(b, 32), off), w)
        }
    }
}
