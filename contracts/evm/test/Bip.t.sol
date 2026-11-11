// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {SighashHarness} from "./Harness.sol";
import {BipVectors, SchnorrVector, KeyPathInput} from "./generated/BipVectors.sol";
import {Prevout} from "../src/BtcTx.sol";
import {Schnorr} from "../src/Schnorr.sol";
import {Secp256k1} from "../src/Secp256k1.sol";
import {TaprootSighash} from "../src/TaprootSighash.sol";

/// The official BIP340 and BIP341 vectors, and the on-chain point
/// decompression the contract uses instead of hints.
contract BipTest is TestBase {
    SighashHarness internal h = new SighashHarness();

    function _split(bytes memory sig) internal pure returns (uint256 rx, uint256 s) {
        assembly ("memory-safe") {
            rx := mload(add(sig, 32))
            s := mload(add(sig, 64))
        }
    }

    function testTags() public pure {
        assertEq(sha256("BIP0340/challenge"), Schnorr.CHALLENGE_TAG, "challenge tag");
        assertEq(sha256("TapSighash"), TaprootSighash.TAG, "TapSighash tag");
    }

    /// All 19 official BIP340 vectors (9 valid, 10 invalid). R.y is computed
    /// on-chain, so no hint can change a verdict.
    function testBip340Official() public view {
        uint256 pass;
        for (uint256 i; i < BipVectors.BIP340_COUNT; ++i) {
            SchnorrVector memory v = BipVectors.bip340(i);
            (uint256 rx, uint256 s) = _split(v.sig);
            bool got = Schnorr.verifyBytes(v.pubkey, v.message, rx, s);
            assertTrue(got == v.ok, string.concat("bip340 vector ", str(v.index)));
            if (v.message.length == 32) {
                assertTrue(Schnorr.verify(v.pubkey, bytes32(v.message), rx, s) == v.ok, str(v.index));
            }
            if (v.ok) ++pass;
        }
        assertEq(pass, 9, "valid vectors");
        assertEq(BipVectors.BIP340_COUNT, 19, "vectors");
    }

    /// Bit flips in the signature, message and key of every valid vector.
    function testBip340FlippedBits() public view {
        for (uint256 i; i < BipVectors.BIP340_COUNT; ++i) {
            SchnorrVector memory v = BipVectors.bip340(i);
            if (!v.ok) continue;
            for (uint256 bit; bit < 512; bit += 37) {
                bytes memory sig = bytes.concat(v.sig);
                sig[bit / 8] ^= bytes1(uint8(1 << (bit % 8)));
                (uint256 rx, uint256 s) = _split(sig);
                assertTrue(!Schnorr.verifyBytes(v.pubkey, v.message, rx, s), "sig bit");
            }
            (uint256 rx0, uint256 s0) = _split(v.sig);
            for (uint256 bit; bit < 8 * v.message.length; bit += 29) {
                bytes memory m = bytes.concat(v.message);
                m[bit / 8] ^= bytes1(uint8(1 << (bit % 8)));
                assertTrue(!Schnorr.verifyBytes(v.pubkey, m, rx0, s0), "msg bit");
            }
            for (uint256 bit; bit < 256; bit += 23) {
                bytes32 pk = v.pubkey ^ bytes32(uint256(1) << bit);
                assertTrue(!Schnorr.verifyBytes(pk, v.message, rx0, s0), "key bit");
            }
            // s = 0, s = n, r = p: rejected
            assertTrue(!Schnorr.verifyBytes(v.pubkey, v.message, rx0, 0), "s = 0");
            assertTrue(!Schnorr.verifyBytes(v.pubkey, v.message, rx0, Secp256k1.N), "s = n");
            assertTrue(!Schnorr.verifyBytes(v.pubkey, v.message, Secp256k1.P, s0), "r = p");
        }
    }

    /// BIP341 keyPathSpending: the five tx hashes, and each of the 7 inputs'
    /// sigMsg, sigHash and signature (hash types 3, 0x83, 1, 0, 2, 0x82, 0x81).
    function testBip341KeyPath() public view {
        bytes memory raw = BipVectors.BIP341_RAW_UNSIGNED_TX;
        Prevout[] memory spent = BipVectors.bip341Spent();
        bytes32[5] memory hs = h.hashes(raw, spent);
        assertEq(hs[0], BipVectors.BIP341_HASHAMOUNTS, "hashAmounts");
        assertEq(hs[1], BipVectors.BIP341_HASHOUTPUTS, "hashOutputs");
        assertEq(hs[2], BipVectors.BIP341_HASHPREVOUTS, "hashPrevouts");
        assertEq(hs[3], BipVectors.BIP341_HASHSCRIPTPUBKEYS, "hashScriptPubkeys");
        assertEq(hs[4], BipVectors.BIP341_HASHSEQUENCES, "hashSequences");
        uint256 seen;
        for (uint256 j; j < BipVectors.BIP341_INPUT_COUNT; ++j) {
            KeyPathInput memory v = BipVectors.bip341Input(j);
            seen |= uint256(1) << (v.hashType & 3 | (v.hashType >> 5));
            bytes memory m = h.sigMsg(raw, spent, v.index, v.hashType);
            assertEq(m, v.sigMsg, str(j));
            bytes32 sh = sha256(abi.encodePacked(TaprootSighash.TAG, TaprootSighash.TAG, m));
            assertEq(sh, v.sigHash, str(j));
            bytes memory spk = spent[v.index].scriptPubKey;
            bytes32 key;
            assembly ("memory-safe") {
                key := mload(add(spk, 34))
            }
            (uint256 rx, uint256 s) = _split(v.sig);
            assertTrue(Schnorr.verify(key, sh, rx, s), str(j));
            assertTrue(!Schnorr.verify(key, sh ^ bytes32(uint256(1)), rx, s), str(j));
        }
        // bits 0-3 for DEFAULT, ALL, NONE, SINGLE; 5-7 for ALL|ACP, NONE|ACP, SINGLE|ACP
        assertEq(seen, 0xef, "all 7 hash types");
    }

    function testLiftX() public view {
        uint256 gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798;
        uint256 gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8;
        (bool ok, uint256 y) = Secp256k1.liftX(gx, 0);
        assertTrue(ok && y == gy, "G, even");
        (ok, y) = Secp256k1.liftX(gx, 1);
        assertTrue(ok && y == Secp256k1.P - gy, "G, odd");
        (ok,) = Secp256k1.liftX(0, 0);
        assertTrue(!ok, "x = 0 is not on the curve");
        (ok,) = Secp256k1.liftX(5, 0);
        assertTrue(!ok, "x = 5 is not on the curve");
        (ok,) = Secp256k1.liftX(Secp256k1.P, 0);
        assertTrue(!ok, "x = p");
        (ok,) = Secp256k1.liftX(Secp256k1.P + 1, 0);
        assertTrue(!ok, "x > p");
    }
}
