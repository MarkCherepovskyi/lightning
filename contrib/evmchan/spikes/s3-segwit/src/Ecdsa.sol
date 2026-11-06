// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Bitcoin ECDSA signatures: strict DER parsing and verification with
/// ecrecover against a known compressed public key.
///
/// @notice Soundness of the hints. The submitter supplies two hints per
/// signature: the recovery id `recId` (0 or 1) and `keyY`, the Y coordinate of
/// the public key. Neither can make a forged signature pass:
///   1. keyY is accepted only if keyY < p, keyY^2 = x^3 + 7 (mod p) and its
///      parity matches the key's 02/03 prefix. For a given x there is exactly
///      one such y, so (x, keyY) is the point the 33-byte key encodes (and a
///      key whose x is not on the curve is rejected, as Bitcoin does). Hence
///      `expected` = address(keccak256(x || keyY)) is the key's own address.
///   2. ecrecover(z, 27 + recId, r, s) returns address(Q) with
///      Q = r^-1 * (s * R - z * G), where R is the curve point with x = r and
///      the y parity recId. If address(Q) == expected then Q = P (up to a
///      160-bit keccak second preimage), so s * R = z * G + r * P, i.e.
///      R = s^-1 * (z * G + r * P), and R.x = r: that is exactly ECDSA
///      verification of (r, s) on z under P. This holds for either recId, so
///      the recovery id only decides whether an honest signature is found.
///   3. ecrecover reduces z mod n like Bitcoin does, and rejects r, s outside
///      [1, n); we check those ranges anyway, plus low S.
/// A wrong hint thus yields Q != P and a rejection: it can make an honest
/// transaction fail, never a forged one pass. The one honest case ecrecover
/// cannot handle is R.x >= n (r = R.x - n), which happens with probability
/// about 2^-128 per signature; such a signature is rejected.
library Ecdsa {
    uint256 internal constant P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F;
    uint256 internal constant N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
    uint256 internal constant HALF_N = N / 2;

    /// @notice Parses a Bitcoin signature: DER followed by one hash type byte.
    /// Enforces BIP66 (IsValidSignatureEncoding), 0 < r < n and 0 < s <= n/2
    /// (low S, Bitcoin's LOW_S policy). The hash type is returned unchecked.
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
        if (r == 0 || r >= N || s == 0 || s > HALF_N) return (false, 0, 0, 0);
        return (true, r, s, uint8(sig[len - 1]));
    }

    /// @notice True iff (r, s) is a valid signature of `digest` by the
    /// compressed key `pubkey`, using the hints `recId` and `keyY` (see the
    /// library notes). r and s must come from `parse`.
    function verify(bytes memory pubkey, bytes32 digest, uint256 r, uint256 s, uint8 recId, uint256 keyY)
        internal
        pure
        returns (bool)
    {
        if (pubkey.length != 33 || recId > 1) return false;
        uint8 prefix = uint8(pubkey[0]);
        if (prefix != 0x02 && prefix != 0x03) return false;
        uint256 x;
        assembly ("memory-safe") {
            x := mload(add(pubkey, 33))
        }
        if (x >= P || keyY >= P || keyY & 1 != prefix & 1) return false;
        if (mulmod(keyY, keyY, P) != addmod(mulmod(mulmod(x, x, P), x, P), 7, P)) return false;
        address expected = address(uint160(uint256(keccak256(abi.encodePacked(x, keyY)))));
        address q = ecrecover(digest, 27 + recId, bytes32(r), bytes32(s));
        return q != address(0) && q == expected;
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
            if (uint8(sig[off]) != 0) return N;
            off += 1;
            n = 32;
        }
        if (n > 32) return N;
        assembly ("memory-safe") {
            v := shr(mul(8, sub(32, n)), mload(add(add(sig, 32), off)))
        }
    }
}
