// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BtcTx, Tx, TxOut} from "../src/BtcTx.sol";
import {Schnorr} from "../src/Schnorr.sol";
import {TaprootSighash} from "../src/TaprootSighash.sol";
import {TaprootKeyPath} from "../src/TaprootKeyPath.sol";

/// External wrappers around the libraries, for expectRevert and the gas report.
contract Harness {
    function verify(bytes32 pubkeyX, bytes32 msgHash, bytes calldata sig, uint256 ry) external pure returns (bool) {
        return Schnorr.verify(pubkeyX, msgHash, sig, ry);
    }

    function verifyBytes(bytes32 pubkeyX, bytes calldata message, bytes calldata sig, uint256 ry)
        external
        pure
        returns (bool)
    {
        return Schnorr.verifyBytes(pubkeyX, message, sig, ry);
    }

    function parse(bytes calldata raw) external pure returns (Tx memory t, bytes[][] memory w, bytes32 txid) {
        return BtcTx.parse(raw);
    }

    function sigHash(Tx calldata t, TxOut[] calldata spent, uint256 index, uint8 hashType)
        external
        pure
        returns (bytes32)
    {
        Tx memory tm = t;
        TxOut[] memory sm = spent;
        return TaprootSighash.sigHash(tm, sm, TaprootSighash.precompute(tm, sm), index, hashType);
    }

    /// Full check of a raw signed transaction whose inputs are all P2TR key path.
    function verifyTx(bytes calldata raw, TxOut[] calldata spent, uint256[] calldata ry)
        external
        pure
        returns (bool)
    {
        (Tx memory t, bytes[][] memory w,) = BtcTx.parse(raw);
        TxOut[] memory sm = spent;
        TaprootSighash.Precomputed memory pc = TaprootSighash.precompute(t, sm);
        for (uint256 i; i < t.inputs.length; ++i) {
            if (!TaprootKeyPath.verifyInput(t, sm, pc, i, w[i], ry[i])) return false;
        }
        return true;
    }

    /// Same as verifyTx, returning the gas of each phase (measured with gasleft,
    /// excluding calldata decoding): gas = [parse, precompute, then per input
    /// the sighash and the Schnorr verify].
    struct Ctx {
        Tx t;
        bytes[][] w;
        TxOut[] spent;
        TaprootSighash.Precomputed pc;
    }

    function measureTx(bytes calldata raw, TxOut[] calldata spent, uint256[] calldata ry)
        external
        view
        returns (bool ok, uint256[] memory gas)
    {
        Ctx memory c;
        c.spent = spent;
        uint256 g = gasleft();
        (c.t, c.w,) = BtcTx.parse(raw);
        gas = new uint256[](2 + 2 * c.t.inputs.length);
        gas[0] = g - gasleft();
        g = gasleft();
        c.pc = TaprootSighash.precompute(c.t, c.spent);
        gas[1] = g - gasleft();
        ok = true;
        for (uint256 i; i < c.t.inputs.length; ++i) {
            ok = _measureInput(c, i, ry[i], gas) && ok;
        }
    }

    function _measureInput(Ctx memory c, uint256 i, uint256 ry, uint256[] memory gas) private view returns (bool v) {
        uint256 g = gasleft();
        bytes32 h = TaprootSighash.sigHash(c.t, c.spent, c.pc, i, 0);
        gas[2 + 2 * i] = g - gasleft();
        bytes memory spk = c.spent[i].scriptPubKey;
        bytes32 key;
        assembly ("memory-safe") {
            key := mload(add(spk, 34))
        }
        (uint256 rx, uint256 s) = Schnorr.split(c.w[i][0]);
        g = gasleft();
        v = Schnorr.verify(key, h, rx, s, ry);
        gas[3 + 2 * i] = g - gasleft();
    }
}
