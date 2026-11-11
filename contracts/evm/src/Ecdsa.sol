// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Secp256k1} from "./Secp256k1.sol";

/// @title Bitcoin ECDSA signatures: strict DER parsing, and verification
/// against a compressed public key with ecrecover. No hints (SPEC 4.3.10).
///
/// @notice Soundness (from spike S3, with the hints now computed on-chain):
///   1. The key's Y is computed by Secp256k1.liftX: the unique y < p with
///      y^2 = x^3 + 7 and the parity of the 02/03 prefix. A key whose x is not
///      on the curve (or >= p) is rejected, as Bitcoin does. So `expected` =
///      address(keccak256(x || y)) is the address of the key's own point P.
///   2. ecrecover(z, v, r, s) returns address(Q), Q = r^-1 * (s * R - z * G),
///      where R is the point with x = r and the y parity v - 27. If
///      address(Q) == expected then Q = P (up to a 160-bit keccak second
///      preimage), so s * R = z * G + r * P, i.e. R = s^-1 * (z * G + r * P)
///      with R.x = r: exactly ECDSA verification of (r, s) on z under P. That
///      holds for either v, so trying v = 27 and then v = 28 only decides
///      whether an honest signature is found, never whether a forged one passes.
///   3. ecrecover reduces z mod n as Bitcoin does, and rejects r, s outside
///      [1, n); we check those ranges anyway, plus low S. It returns zero for
///      an r that is not the x of a curve point and for the point at infinity
///      (true of geth and revm, so of Base and OP), and `expected` is never zero.
/// The one honest signature this rejects has R.x >= n (so r = R.x - n, and
/// ecrecover lifts the wrong point), with probability about 2^-128.
library Ecdsa {
    uint256 internal constant HALF_N = Secp256k1.N / 2;

    /// @notice Parses a Bitcoin signature: DER followed by one hash type byte.
    /// Enforces BIP66 (IsValidSignatureEncoding), 0 < r < n and 0 < s <= n/2
    /// (low S). The hash type is returned unchecked.
    function parse(bytes memory sig) internal pure returns (bool ok, uint256 r, uint256 s, uint8 hashType) {
        uint256 len = sig.length;
        // 0x30 [total-len] 0x02 [R-len] [R] 0x02 [S-len] [S] [sighash]
        if (len < 9 || len > 73) return (false, 0, 0, 0);
        if (uint8(sig[0]) != 0x30 || uint8(sig[1]) != len - 3) return (false, 0, 0, 0);
        uint256 lenR = uint8(sig[3]);
        if (5 + lenR >= len) return (false, 0, 0, 0);
        uint256 lenS = uint8(sig[5 + lenR]);
        if (lenR + lenS + 7 != len) return (false, 0, 0, 0);
        if (!_derInt(sig, 2, lenR) || !_derInt(sig, lenR + 4, lenS)) return (false, 0, 0, 0);
        r = _readInt(sig, 4, lenR);
        s = _readInt(sig, 6 + lenR, lenS);
        // _readInt returns N (out of range) for integers wider than 32 bytes.
        if (r == 0 || r >= Secp256k1.N || s == 0 || s > HALF_N) return (false, 0, 0, 0);
        return (true, r, s, uint8(sig[len - 1]));
    }

    /// @notice The point of a 33-byte compressed key: prefix 02 or 03, x < p
    /// and on the curve (see the library notes, 1).
    function liftKey(bytes memory pubkey) internal view returns (bool ok, uint256 x, uint256 y) {
        if (pubkey.length != 33) return (false, 0, 0);
        uint8 prefix = uint8(pubkey[0]);
        if (prefix != 0x02 && prefix != 0x03) return (false, 0, 0);
        assembly ("memory-safe") {
            x := mload(add(pubkey, 33))
        }
        (ok, y) = Secp256k1.liftX(x, prefix & 1);
    }

    /// @notice True iff (r, s) is a valid ECDSA signature of `digest` by the
    /// point (x, y) from liftKey (see the library notes, 2 and 3).
    function verify(uint256 x, uint256 y, bytes32 digest, uint256 r, uint256 s) internal pure returns (bool) {
        address expected = Secp256k1.addr(x, y);
        if (ecrecover(digest, 27, bytes32(r), bytes32(s)) == expected) return true;
        return ecrecover(digest, 28, bytes32(r), bytes32(s)) == expected;
    }

    /// BIP66 rules for one INTEGER at sig[off] (its 0x02 tag) of length `n`:
    /// non-empty, not negative, no unnecessary leading zero byte.
    function _derInt(bytes memory sig, uint256 off, uint256 n) private pure returns (bool) {
        if (uint8(sig[off]) != 0x02 || n == 0) return false;
        uint8 first = uint8(sig[off + 2]);
        if (first & 0x80 != 0) return false;
        if (n > 1 && first == 0 && uint8(sig[off + 3]) & 0x80 == 0) return false;
        return true;
    }

    /// Big-endian integer of n bytes at sig[off]; N if it does not fit 32 bytes.
    function _readInt(bytes memory sig, uint256 off, uint256 n) private pure returns (uint256 v) {
        if (n == 33) {
            // only a leading zero byte can make a valid 33-byte INTEGER of a value < 2^256
            if (uint8(sig[off]) != 0) return Secp256k1.N;
            off += 1;
            n = 32;
        }
        if (n > 32) return Secp256k1.N;
        assembly ("memory-safe") {
            v := shr(mul(8, sub(32, n)), mload(add(add(sig, 32), off)))
        }
    }
}
