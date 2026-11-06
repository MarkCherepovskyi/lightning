// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Tx, TxIn, TxOut} from "s2/BtcTx.sol";
import {TaprootSighash} from "s2/TaprootSighash.sol";

/// @title BIP143 signature hash (segwit v0) for ALL, NONE, SINGLE and their
/// ANYONECANPAY variants.
/// @notice Only the six defined hash types are accepted (Bitcoin's STRICTENC
/// policy; consensus would also accept undefined ones and treat them as ALL).
///
/// SINGLE when the input index has no output of the same index: BIP143 (and
/// Bitcoin Core's SignatureHash for witness v0) uses a hashOutputs of 32 zero
/// bytes, so the signature commits to no output. It does NOT have the legacy
/// "hash = 1" bug: the signature still commits to this input's outpoint, amount,
/// script and sequence (and, without ANYONECANPAY, to all outpoints). We follow
/// BIP143 exactly here, so the hash matches what any Bitcoin signer computes;
/// such a signature only ever spends what its own signer chose to give away.
/// CLN never makes one.
library Bip143 {
    uint8 internal constant ALL = 0x01;
    uint8 internal constant NONE = 0x02;
    uint8 internal constant SINGLE = 0x03;
    uint8 internal constant ANYONECANPAY = 0x80;

    error BadHashType(uint8 hashType);
    error BadInputIndex();

    /// hashPrevouts, hashSequence and hashOutputs (double SHA256). Filled on
    /// the first sighash that needs them, then reused for every input.
    struct Precomputed {
        bool done;
        bytes32 prevouts;
        bytes32 sequences;
        bytes32 outputs;
    }

    function validHashType(uint8 hashType) internal pure returns (bool) {
        uint8 base = hashType & ~ANYONECANPAY;
        return base >= ALL && base <= SINGLE;
    }

    function sigHash(
        Tx memory t,
        Precomputed memory pc,
        uint256 index,
        bytes memory scriptCode,
        uint64 amount,
        uint8 hashType
    ) internal pure returns (bytes32) {
        if (!validHashType(hashType)) revert BadHashType(hashType);
        if (index >= t.inputs.length) revert BadInputIndex();
        (bytes32 hashPrevouts, bytes32 hashSequence, bytes32 hashOutputs) = _txHashes(t, pc, index, hashType);
        TxIn memory input = t.inputs[index];
        bytes memory head = abi.encodePacked(
            TaprootSighash.le32(t.version), hashPrevouts, hashSequence, input.prevTxid, TaprootSighash.le32(input.prevVout)
        );
        bytes memory tail = abi.encodePacked(
            TaprootSighash.le32(input.sequence), hashOutputs, TaprootSighash.le32(t.locktime), TaprootSighash.le32(hashType)
        );
        return _dsha(
            abi.encodePacked(
                head, TaprootSighash.compactSize(scriptCode.length), scriptCode, TaprootSighash.le64(amount), tail
            )
        );
    }

    /// hashPrevouts, hashSequence and hashOutputs as this hash type uses them.
    function _txHashes(Tx memory t, Precomputed memory pc, uint256 index, uint8 hashType)
        private
        pure
        returns (bytes32 hashPrevouts, bytes32 hashSequence, bytes32 hashOutputs)
    {
        bool acp = hashType & ANYONECANPAY != 0;
        uint8 base = hashType & ~ANYONECANPAY;
        if (!acp || base == ALL) _precompute(t, pc);
        if (!acp) hashPrevouts = pc.prevouts;
        if (!acp && base == ALL) hashSequence = pc.sequences;
        if (base == ALL) {
            hashOutputs = pc.outputs;
        } else if (base == SINGLE && index < t.outputs.length) {
            hashOutputs = _dsha(_serializeOutput(t.outputs[index]));
        }
    }

    function _precompute(Tx memory t, Precomputed memory pc) private pure {
        if (pc.done) return;
        bytes memory prevouts;
        bytes memory sequences;
        for (uint256 i; i < t.inputs.length; ++i) {
            TxIn memory input = t.inputs[i];
            prevouts = abi.encodePacked(prevouts, input.prevTxid, TaprootSighash.le32(input.prevVout));
            sequences = abi.encodePacked(sequences, TaprootSighash.le32(input.sequence));
        }
        bytes memory outputs;
        for (uint256 i; i < t.outputs.length; ++i) {
            outputs = abi.encodePacked(outputs, _serializeOutput(t.outputs[i]));
        }
        pc.prevouts = _dsha(prevouts);
        pc.sequences = _dsha(sequences);
        pc.outputs = _dsha(outputs);
        pc.done = true;
    }

    function _serializeOutput(TxOut memory out) private pure returns (bytes memory) {
        return abi.encodePacked(
            TaprootSighash.le64(out.amount), TaprootSighash.compactSize(out.scriptPubKey.length), out.scriptPubKey
        );
    }

    function _dsha(bytes memory b) private pure returns (bytes32) {
        return sha256(abi.encodePacked(sha256(b)));
    }
}
