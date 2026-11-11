// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Secp256k1} from "./Secp256k1.sol";

/// @title BIP340 Schnorr signature verification on secp256k1, with no hints.
/// @notice Uses the ecrecover precompile as a multi-scalar multiplier:
///   ecrecover(h, v, r, s) returns addr(r^-1 * (s * R' - h * G)), where R' is
///   the point with x = r and the y parity v - 27. With R' = P (r = P.x,
///   v = 27 for the even y of lift_x(P.x), which is P as BIP340 defines it),
///   h = -s_sig * P.x and s = -e * P.x, it returns addr(s_sig * G - e * P).
///   We compare that with addr(R), where R = (sig[0:32], ry) and ry, the even
///   y of lift_x(R.x), is computed on-chain with the modexp precompile
///   (Secp256k1.liftX; SPEC 4.3.10). The address comparison pins
///   s_sig * G - e * P to exactly R, which is BIP340's check that the point is
///   not infinity, has even y and has x = r.
///
/// Deviations from BIP340 (all are rejections of signatures that BIP340
/// accepts, none is an acceptance BIP340 rejects; none can be triggered by
/// anyone without breaking SHA256 or solving a discrete log):
///   - P.x in [n, p): ecrecover needs r < n. A key with such an x cannot be
///     produced with a known secret key without ~2^128 work.
///   - e == 0 (challenge hash is a multiple of n): ecrecover needs s != 0.
///     Finding such a message is a SHA256 preimage.
///   - s_sig == 0: rejected outright (SPEC 4.3.10). Such a signature needs
///     R = -e * P with e = H(R.x || P.x || m), a fixed point of SHA256 that
///     nobody can find.
/// The point comparison is on 160-bit addresses; forging a match is a
/// 160-bit second preimage on keccak256 of a point the attacker does not
/// control independently of R.
library Schnorr {
    uint256 internal constant P = Secp256k1.P;
    uint256 internal constant N = Secp256k1.N;

    /// sha256("BIP0340/challenge")
    bytes32 internal constant CHALLENGE_TAG = 0x7bb52d7a9fef58323eb1bf7a407db382d2f3f2d81bb1224f49fe518f6d48d37c;

    /// @notice BIP340 verify for a 32-byte message (the taproot case), with
    /// the signature as its two 32-byte halves r and s.
    function verify(bytes32 pubkeyX, bytes32 msgHash, uint256 rx, uint256 s) internal view returns (bool) {
        return verifyBytes(pubkeyX, abi.encodePacked(msgHash), rx, s);
    }

    /// @notice BIP340 verify for a message of any length (BIP340 allows it;
    /// the official vectors 15-18 use it).
    function verifyBytes(bytes32 pubkeyX, bytes memory message, uint256 rx, uint256 s)
        internal
        view
        returns (bool)
    {
        uint256 px = uint256(pubkeyX);
        // BIP340 fails if px >= p, r >= p or s >= n. We also fail for px == 0
        // (not on the curve: 7 is not a square mod p) and px in [n, p) (see
        // the library notes). Whether px is on the curve is checked by
        // ecrecover, which returns 0 when it cannot decompress its r.
        if (px == 0 || px >= N || rx >= P || s >= N) return false;
        (bool ok, uint256 ry) = Secp256k1.liftX(rx, 0);
        if (!ok) return false; // r is not the x of a curve point
        uint256 e = uint256(sha256(abi.encodePacked(CHALLENGE_TAG, CHALLENGE_TAG, rx, pubkeyX, message))) % N;
        if (e == 0) return false; // see the library notes
        // h = -s * px mod n; h == 0 iff s == 0 (see the library notes)
        uint256 h = mulmod(s, px, N);
        if (h == 0) return false;
        h = N - h;
        // sEc = -e * px mod n, never 0 since e, px are in [1, n) and n is prime
        uint256 sEc = N - mulmod(e, px, N);
        address q = ecrecover(bytes32(h), 27, bytes32(px), bytes32(sEc));
        // q == 0: px not on the curve, or s*G - e*P is the point at infinity.
        return q != address(0) && q == Secp256k1.addr(rx, ry);
    }
}
