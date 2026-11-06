// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {Vectors, SchnorrVector} from "./Vectors.sol";
import {Schnorr} from "../src/Schnorr.sol";
import {TaprootSighash} from "../src/TaprootSighash.sol";

contract SchnorrTest is TestBase {
    uint256 constant P = Schnorr.P;
    uint256 constant N = Schnorr.N;

    function testTags() public pure {
        assertEq(sha256("BIP0340/challenge"), Schnorr.CHALLENGE_TAG, "challenge tag");
        assertEq(sha256("TapSighash"), TaprootSighash.TAG, "TapSighash tag");
    }

    /// All 19 official BIP340 vectors (9 valid, 10 invalid), with the expected result.
    function testBip340Official() public view {
        uint256 pass;
        uint256 fail;
        for (uint256 i; i < Vectors.BIP340_COUNT; ++i) {
            SchnorrVector memory v = Vectors.bip340(i);
            bool got = Schnorr.verifyBytes(v.pubkey, v.message, v.sig, v.ry);
            assertTrue(got == v.ok, string.concat("bip340 vector ", str(i)));
            if (v.message.length == 32) {
                assertTrue(Schnorr.verify(v.pubkey, bytes32(v.message), v.sig, v.ry) == v.ok, str(i));
            }
            // The verdict must not depend on the hint for a failing vector:
            // try the other root and a recomputed one too.
            if (!v.ok) {
                assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, v.sig, liftX(rxOf(v.sig))), str(i));
                if (v.ry != 0) assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, v.sig, P - v.ry), str(i));
            }
            if (v.ok) ++pass;
            else ++fail;
        }
        assertEq(pass, 9, "valid vectors");
        assertEq(fail, 10, "invalid vectors");
    }

    function testRandomVectors() public pure {
        for (uint256 i; i < Vectors.RANDOM_COUNT; ++i) {
            SchnorrVector memory v = Vectors.random(i);
            assertTrue(Schnorr.verify(v.pubkey, bytes32(v.message), v.sig, v.ry), str(i));
        }
    }

    /// Bit flips in the signature, message and key, over every valid vector.
    function testFlippedBits() public view {
        for (uint256 i; i < Vectors.BIP340_COUNT + Vectors.RANDOM_COUNT; ++i) {
            SchnorrVector memory v = i < Vectors.BIP340_COUNT ? Vectors.bip340(i) : Vectors.random(i - Vectors.BIP340_COUNT);
            if (!v.ok) continue;
            for (uint256 bit = 0; bit < 512; bit += 37) {
                bytes memory sig = bytes.concat(v.sig);
                sig[bit / 8] ^= bytes1(uint8(1 << (bit % 8)));
                // With the stale hint, and with an honest hint for the new R.x.
                assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, sig, v.ry), "sig bit, old hint");
                assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, sig, liftX(rxOf(sig))), "sig bit, new hint");
            }
            for (uint256 bit = 0; bit < 8 * v.message.length; bit += 29) {
                bytes memory m = bytes.concat(v.message);
                m[bit / 8] ^= bytes1(uint8(1 << (bit % 8)));
                assertFalse(Schnorr.verifyBytes(v.pubkey, m, v.sig, v.ry), "msg bit");
            }
            bytes memory longer = bytes.concat(v.message, hex"00");
            assertFalse(Schnorr.verifyBytes(v.pubkey, longer, v.sig, v.ry), "msg extended");
            for (uint256 bit = 0; bit < 256; bit += 23) {
                bytes32 pk = v.pubkey ^ bytes32(uint256(1) << bit);
                assertFalse(Schnorr.verifyBytes(pk, v.message, v.sig, v.ry), "key bit");
            }
        }
    }

    function testBadHints() public view {
        for (uint256 i; i < Vectors.BIP340_COUNT + Vectors.RANDOM_COUNT; ++i) {
            SchnorrVector memory v = i < Vectors.BIP340_COUNT ? Vectors.bip340(i) : Vectors.random(i - Vectors.BIP340_COUNT);
            if (!v.ok) continue;
            assertEq(v.ry, liftX(rxOf(v.sig)), "generator hint equals on-chain lift_x");
            assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, v.sig, P - v.ry), "odd root");
            assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, v.sig, v.ry + 2), "not on curve");
            assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, v.sig, 0), "zero");
            // ry + P is congruent and, when it fits, odd: still must fail.
            if (v.ry < type(uint256).max - P) {
                assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, v.sig, v.ry + P), "ry + p");
            }
            // 2p - ry: congruent to the odd root, even, >= p (fits only for ry near p).
            if (v.ry > P - (type(uint256).max - P + 1)) {
                assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, v.sig, 2 * P - v.ry), "2p - ry");
            }
        }
    }

    function testLengthsAndRanges() public view {
        SchnorrVector memory v = Vectors.bip340(1);
        assertTrue(Schnorr.verifyBytes(v.pubkey, v.message, v.sig, v.ry), "baseline");
        bytes memory sig = v.sig;
        assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, bytes.concat(sig, hex"01"), v.ry), "65 bytes");
        bytes memory short = new bytes(63);
        for (uint256 i; i < 63; ++i) short[i] = sig[i];
        assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, short, v.ry), "63 bytes");
        assertFalse(Schnorr.verifyBytes(v.pubkey, v.message, "", v.ry), "empty");
        // s + n would be the same scalar mod n; must fail (only fits if s < 2^256 - n).
        (uint256 rx, uint256 s) = Schnorr.split(sig);
        assertFalse(Schnorr.verify(v.pubkey, bytes32(v.message), rx, N, v.ry), "s = n");
        assertFalse(Schnorr.verify(v.pubkey, bytes32(v.message), rx, type(uint256).max, v.ry), "s = max");
        assertFalse(Schnorr.verify(v.pubkey, bytes32(v.message), P, s, v.ry), "r = p");
        assertFalse(Schnorr.verify(v.pubkey, bytes32(v.message), 0, s, v.ry), "r = 0");
        assertFalse(Schnorr.verify(bytes32(0), bytes32(v.message), rx, s, v.ry), "pk = 0");
        assertFalse(Schnorr.verify(bytes32(P), bytes32(v.message), rx, s, v.ry), "pk = p");
        assertFalse(Schnorr.verify(bytes32(type(uint256).max), bytes32(v.message), rx, s, v.ry), "pk = max");
        // A pubkey x in [n, p) on the curve: rejected (documented deviation; no
        // valid signature can exist without its secret key anyway).
        uint256 x = N;
        while (liftX(x) == 0) ++x;
        assertTrue(x < P, "found x in [n, p)");
        assertFalse(Schnorr.verify(bytes32(x), bytes32(v.message), rx, s, v.ry), "pk in [n, p)");
    }

    /// The same signature on the key of another vector, and a signature for one
    /// vector replayed on another message, both fail.
    function testCrossVectors() public pure {
        for (uint256 i; i < Vectors.RANDOM_COUNT; ++i) {
            SchnorrVector memory a = Vectors.random(i);
            SchnorrVector memory b = Vectors.random((i + 1) % Vectors.RANDOM_COUNT);
            assertFalse(Schnorr.verifyBytes(b.pubkey, a.message, a.sig, a.ry), "other key");
            assertFalse(Schnorr.verifyBytes(a.pubkey, b.message, a.sig, a.ry), "other message");
        }
    }

    function rxOf(bytes memory sig) internal pure returns (uint256 rx) {
        (rx,) = Schnorr.split(sig);
    }

    /// Even y of lift_x(x) via the modexp precompile, or 0 if x is not on the curve.
    function liftX(uint256 x) internal view returns (uint256) {
        if (x >= P) return 0;
        uint256 c = addmod(mulmod(mulmod(x, x, P), x, P), 7, P);
        (bool ok, bytes memory out) = address(5).staticcall(abi.encodePacked(uint256(32), uint256(32), uint256(32), c, (P + 1) / 4, P));
        require(ok, "modexp");
        uint256 y = abi.decode(out, (uint256));
        if (mulmod(y, y, P) != c) return 0;
        return y & 1 == 0 ? y : P - y;
    }
}
