// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Tx, TxOut} from "./BtcTx.sol";
import {Schnorr} from "./Schnorr.sol";
import {TaprootSighash} from "./TaprootSighash.sol";

/// @title Verification of one P2TR key-path input.
library TaprootKeyPath {
    /// @notice True when input `index` of `t` spends a P2TR output (witness v1,
    /// 32-byte program) with a valid key-path witness. A stack with an annex
    /// (two or more items, the last starting with 0x50) or a script-path stack
    /// is rejected, as are a non-empty scriptSig and a 65-byte signature whose hash type byte is 0x00 or
    /// not a BIP341 hash type.
    /// @param ry the R.y hint for the signature (see Schnorr.verify)
    function verifyInput(
        Tx memory t,
        TxOut[] memory spent,
        TaprootSighash.Precomputed memory pc,
        uint256 index,
        bytes[] memory witness,
        uint256 ry
    ) internal pure returns (bool) {
        bytes memory spk = spent[index].scriptPubKey;
        if (spk.length != 34 || spk[0] != 0x51 || spk[1] != 0x20) return false;
        bytes32 outputKey;
        assembly ("memory-safe") {
            outputKey := mload(add(spk, 34))
        }

        // A native witness program must have an empty scriptSig.
        if (t.inputs[index].scriptSig.length != 0) return false;
        if (witness.length != 1) return false;
        bytes memory sig = witness[0];
        uint8 hashType;
        if (sig.length == 65) {
            hashType = uint8(sig[64]);
            if (hashType == 0 || !TaprootSighash.validHashType(hashType)) return false;
        } else if (sig.length != 64) {
            return false;
        }
        // BIP341: SINGLE without a matching output is a failure.
        if (hashType & 3 == TaprootSighash.SINGLE && index >= t.outputs.length) return false;
        (uint256 rx, uint256 s) = Schnorr.split(sig);
        return Schnorr.verify(outputKey, TaprootSighash.sigHash(t, spent, pc, index, hashType), rx, s, ry);
    }
}
