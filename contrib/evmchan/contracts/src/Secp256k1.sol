// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title secp256k1 point decompression with the modexp precompile (EIP-198).
library Secp256k1 {
    uint256 internal constant P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F;
    uint256 internal constant N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;
    /// (P + 1) / 4: since P = 3 mod 4, a^((P+1)/4) is a square root of a
    /// whenever a is a square mod P.
    uint256 private constant SQRT_EXP = 0x3fffffffffffffffffffffffffffffffffffffffffffffffffffffffbfffff0c;

    /// @notice The y with y^2 = x^3 + 7 (mod P) and the given parity (0 even,
    /// 1 odd), if x < P is the x coordinate of a curve point.
    /// @dev Whatever the precompile returns, y is accepted only if y < P and
    /// y^2 = x^3 + 7, so a wrong result can only cause a rejection. For a given
    /// x there are exactly two such y (y and P - y, of opposite parity; y = 0
    /// is impossible as the curve has no point of order 2), so the result is
    /// the unique point with that x and parity.
    function liftX(uint256 x, uint256 parity) internal view returns (bool ok, uint256 y) {
        if (x >= P) return (false, 0);
        uint256 c = addmod(mulmod(mulmod(x, x, P), x, P), 7, P);
        bool success;
        assembly ("memory-safe") {
            let p := mload(0x40)
            mstore(p, 32)
            mstore(add(p, 0x20), 32)
            mstore(add(p, 0x40), 32)
            mstore(add(p, 0x60), c)
            mstore(add(p, 0x80), SQRT_EXP)
            mstore(add(p, 0xa0), P)
            // Yul evaluates arguments right to left, so the call must come
            // first, before returndatasize() is read.
            success := staticcall(gas(), 0x05, p, 0xc0, p, 0x20)
            success := and(success, eq(returndatasize(), 0x20))
            y := mload(p)
        }
        if (!success || y >= P || mulmod(y, y, P) != c) return (false, 0);
        if (y & 1 != parity) y = P - y;
        return (true, y);
    }

    /// The Ethereum address of a point, as ecrecover returns it.
    function addr(uint256 x, uint256 y) internal pure returns (address) {
        return address(uint160(uint256(keccak256(abi.encodePacked(x, y)))));
    }
}
