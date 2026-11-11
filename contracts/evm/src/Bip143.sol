// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BtcTx, TxView} from "./BtcTx.sol";

/// @title BIP143 signature hash (segwit v0) for ALL, NONE, SINGLE and their
/// ANYONECANPAY variants (SPEC 4.3.8).
/// @notice Only the six defined hash types are accepted (Bitcoin's STRICTENC
/// policy; consensus would also accept undefined ones).
///
/// SINGLE when the input has no output of the same index: BIP143 (and Bitcoin
/// Core's SignatureHash for witness v0) uses a hashOutputs of 32 zero bytes, so
/// the signature commits to no output. It does NOT have the legacy "hash = 1"
/// bug: the signature still commits to this input's outpoint, amount, script
/// and sequence. We follow BIP143 exactly, as SPEC 4.3.8 says; such a
/// signature only ever spends what its own signer chose to give away. CLN
/// never makes one.
///
/// The outputs are hashed straight from the raw tx, where they lie contiguous;
/// the outpoints and sequences are gathered from it (BtcTx).
library Bip143 {
    uint8 internal constant ALL = 0x01;
    uint8 internal constant NONE = 0x02;
    uint8 internal constant SINGLE = 0x03;
    uint8 internal constant ANYONECANPAY = 0x80;

    function validHashType(uint8 hashType) internal pure returns (bool) {
        uint8 base = hashType & ~ANYONECANPAY;
        return base >= ALL && base <= SINGLE;
    }

    /// @dev hashType must satisfy validHashType.
    function sigHash(
        bytes calldata raw,
        TxView memory t,
        uint256 index,
        bytes memory scriptCode,
        uint64 amount,
        uint8 hashType
    ) internal pure returns (bytes32) {
        bytes memory head = _head(raw, t, index, hashType);
        bytes memory tail = _tail(raw, t, index, hashType);
        bytes memory preimage =
            abi.encodePacked(head, BtcTx.compactSizeBytes(scriptCode.length), scriptCode, BtcTx.le64(amount), tail);
        return sha256(abi.encodePacked(sha256(preimage)));
    }

    /// nVersion, hashPrevouts, hashSequence, outpoint
    function _head(bytes calldata raw, TxView memory t, uint256 index, uint8 hashType)
        private
        pure
        returns (bytes memory)
    {
        bool acp = hashType & ANYONECANPAY != 0;
        bytes32 hashPrevouts;
        bytes32 hashSequence;
        if (!acp) hashPrevouts = sha256(abi.encodePacked(BtcTx.shaPrevouts(raw, t)));
        if (!acp && hashType == ALL) hashSequence = sha256(abi.encodePacked(BtcTx.shaSequences(raw, t)));
        uint256 o = BtcTx.inputOffset(t, index);
        return abi.encodePacked(raw[0:4], hashPrevouts, hashSequence, raw[o:o + 36]);
    }

    /// nSequence, hashOutputs, nLockTime, nHashType
    function _tail(bytes calldata raw, TxView memory t, uint256 index, uint8 hashType)
        private
        pure
        returns (bytes memory)
    {
        uint8 base = hashType & ~ANYONECANPAY;
        bytes32 hashOutputs;
        if (base == ALL) {
            hashOutputs = sha256(abi.encodePacked(BtcTx.shaOutputs(raw, t)));
        } else if (base == SINGLE && index < BtcTx.nOut(t)) {
            hashOutputs = sha256(abi.encodePacked(sha256(BtcTx.outBytes(raw, t, index))));
        }
        uint256 o = BtcTx.inputOffset(t, index);
        return abi.encodePacked(raw[o + 37:o + 41], hashOutputs, raw[raw.length - 4:raw.length], BtcTx.le32(hashType));
    }
}
