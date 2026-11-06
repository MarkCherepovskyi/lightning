// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title BIP340 Schnorr signature verification on secp256k1.
/// @notice Uses the ecrecover precompile as a multi-scalar multiplier:
///   ecrecover(h, v, r, s) returns addr(r^-1 * (s * R' - h * G)), where R' is
///   the point with x = r and the y parity given by v. With R' = P (r = P.x,
///   v = 27 for the even y of lift_x), h = -s_sig * P.x and s = -e * P.x, it
///   returns addr(s_sig * G - e * P). We compare that with addr(R), where R is
///   (sig[0:32], ry) and ry is a caller-supplied hint for R's y coordinate.
///
/// The hint is checked (ry < p, ry even, (rx, ry) on the curve), and the
/// address comparison then pins the computed point to exactly (rx, ry), so a
/// wrong hint can only cause a rejection, never an acceptance.
///
/// Deviations from BIP340 (all are rejections of signatures that BIP340
/// accepts, none is an acceptance BIP340 rejects; none can be triggered by
/// anyone without breaking SHA256 or solving a discrete log):
///   - P.x in [n, p): ecrecover needs r < n. A key with such an x cannot be
///     produced with a known secret key without ~2^128 work.
///   - e == 0 (challenge hash is a multiple of n): ecrecover needs s != 0.
///     Finding such a message is a SHA256 preimage.
/// The point comparison is on 160-bit addresses; forging a match is a
/// 160-bit second preimage on keccak256 of a point the attacker does not
/// control independently of R.
library Schnorr {
    uint256 internal constant P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F;
    uint256 internal constant N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    /// sha256("BIP0340/challenge")
    bytes32 internal constant CHALLENGE_TAG = 0x7bb52d7a9fef58323eb1bf7a407db382d2f3f2d81bb1224f49fe518f6d48d37c;

    /// @notice BIP340 verify for a 32-byte message (the taproot case).
    /// @param pubkeyX x-only public key (BIP340 `pk`)
    /// @param msgHash the 32-byte message
    /// @param sig 64-byte signature r || s
    /// @param ry hint: the even y coordinate of the point R with x = sig[0:32]
    function verify(bytes32 pubkeyX, bytes32 msgHash, bytes memory sig, uint256 ry)
        internal
        pure
        returns (bool)
    {
        if (sig.length != 64) return false;
        (uint256 rx, uint256 s) = split(sig);
        return verify(pubkeyX, msgHash, rx, s, ry);
    }

    /// @notice Same, with the signature as its two 32-byte halves r and s.
    function verify(bytes32 pubkeyX, bytes32 msgHash, uint256 rx, uint256 s, uint256 ry)
        internal
        pure
        returns (bool)
    {
        if (!_precheck(pubkeyX, rx, s, ry)) return false;
        uint256 e = uint256(sha256(abi.encodePacked(CHALLENGE_TAG, CHALLENGE_TAG, rx, pubkeyX, msgHash))) % N;
        return _check(uint256(pubkeyX), rx, ry, s, e);
    }

    /// @notice BIP340 verify for a message of any length (BIP340 allows it;
    /// used for the official vectors 15-18).
    function verifyBytes(bytes32 pubkeyX, bytes memory message, bytes memory sig, uint256 ry)
        internal
        pure
        returns (bool)
    {
        if (sig.length != 64) return false;
        (uint256 rx, uint256 s) = split(sig);
        if (!_precheck(pubkeyX, rx, s, ry)) return false;
        uint256 e = uint256(sha256(abi.encodePacked(CHALLENGE_TAG, CHALLENGE_TAG, rx, pubkeyX, message))) % N;
        return _check(uint256(pubkeyX), rx, ry, s, e);
    }

    /// Splits a signature of at least 64 bytes into r and s.
    function split(bytes memory sig) internal pure returns (uint256 rx, uint256 s) {
        assembly ("memory-safe") {
            rx := mload(add(sig, 32))
            s := mload(add(sig, 64))
        }
    }

    /// Range checks from BIP340 plus validation of the R.y hint.
    function _precheck(bytes32 pubkeyX, uint256 rx, uint256 s, uint256 ry) private pure returns (bool) {
        uint256 px = uint256(pubkeyX);
        // BIP340 fails if px >= p. We also fail for px == 0 (not on the curve:
        // 7 is not a square mod p) and px in [n, p) (see the library notes).
        // Whether px is on the curve is checked by ecrecover, which returns 0
        // when it cannot decompress r.
        if (px == 0 || px >= N) return false;
        if (rx >= P) return false;
        if (s >= N) return false;
        // Hint: canonical, even, and (rx, ry) on y^2 = x^3 + 7.
        if (ry >= P || ry & 1 != 0) return false;
        if (mulmod(ry, ry, P) != addmod(mulmod(mulmod(rx, rx, P), rx, P), 7, P)) return false;
        return true;
    }

    /// Checks s*G - e*P == (rx, ry) through ecrecover.
    function _check(uint256 px, uint256 rx, uint256 ry, uint256 s, uint256 e) private pure returns (bool) {
        if (e == 0) return false; // see the library notes
        // h = -s * px mod n. h == 0 iff s == 0; no vector has that, and a valid
        // signature with s == 0 cannot be made without breaking SHA256.
        uint256 h = mulmod(s, px, N);
        if (h != 0) h = N - h;
        // sEc = -e * px mod n, never 0 since e, px are in [1, n) and n is prime
        uint256 sEc = N - mulmod(e, px, N);
        address q = ecrecover(bytes32(h), 27, bytes32(px), bytes32(sEc));
        // q == 0: px not on the curve, or s*G - e*P is the point at infinity.
        if (q == address(0)) return false;
        return q == address(uint160(uint256(keccak256(abi.encodePacked(rx, ry)))));
    }
}
