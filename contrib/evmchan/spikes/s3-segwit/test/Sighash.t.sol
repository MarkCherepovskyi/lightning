// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {Vectors, SighashVector} from "./Vectors.sol";
import {BtcTx, Tx} from "s2/BtcTx.sol";
import {Bip143} from "../src/Bip143.sol";
import {Ecdsa} from "../src/Ecdsa.sol";

contract Bip143Harness {
    function sigHash(bytes memory raw, uint256 index, bytes memory code, uint64 amount, uint8 ht)
        external
        pure
        returns (bytes32)
    {
        (Tx memory t,,) = BtcTx.parse(raw);
        Bip143.Precomputed memory pc;
        return Bip143.sigHash(t, pc, index, code, amount, ht);
    }

    function parse(bytes memory sig) external pure returns (bool ok, uint256 r, uint256 s, uint8 ht) {
        return Ecdsa.parse(sig);
    }
}

contract SighashTest is TestBase {
    Bip143Harness h = new Bip143Harness();

    /// BIP143's "Native P2WPKH" example: input 1, SIGHASH_ALL.
    function testBip143NativeP2wpkhExample() public view {
        bytes memory raw =
            hex"0100000002fff7f7881a8099afa6940d42d1e7f6362bec38171ea3edf433541db4e4ad969f0000000000eeffffffef51e1b804cc89d182d279655c3aa89e815b1b309fe287d9b2b55d57b90ec68a0100000000ffffffff02202cb206000000001976a9148280b37df378db99f66f85c95a783a76ac7a6d5988ac9093510d000000001976a9143bde42dbee7e4dbe6a21b2d50ce2f0167faa815988ac11000000";
        bytes32 got = h.sigHash(raw, 1, hex"76a9141d0f172a0ecb48aee1be1f2687d2963ae33f71a188ac", 600000000, 1);
        assertEq(got, 0xc37af31116d1b27caf68aae9e3ac82f1477929014d5b917657d0eb49478cb670, "BIP143 example");
    }

    /// NONE, SINGLE, ALL|ACP, NONE|ACP, SINGLE without a matching output and
    /// SINGLE|ACP without one, against the Python implementation.
    function testAllHashTypes() public view {
        for (uint256 i; i < Vectors.SIGHASH_COUNT; ++i) {
            SighashVector memory s = Vectors.sighash(i);
            bytes32 got = h.sigHash(Vectors.HASHTYPES_TX, s.index, s.scriptCode, s.amount, s.hashType);
            assertEq(got, s.sigHash, string.concat("input ", str(i)));
        }
    }

    function testUndefinedHashTypesRejected() public {
        uint8[6] memory bad = [0x00, 0x04, 0x80, 0x84, 0x41, 0xc1];
        for (uint256 i; i < bad.length; ++i) {
            vm.expectRevert(abi.encodeWithSelector(Bip143.BadHashType.selector, bad[i]));
            h.sigHash(Vectors.HASHTYPES_TX, 0, hex"00", 1, bad[i]);
        }
    }

    function testDerStrictness() public view {
        // a valid low-S signature (from vector #3), then BIP66 violations of it
        bytes memory good =
            hex"3044022027163bb4415546634540376c2cee64544bd781a39052f409e21f9de84bd7d2e002206ca8e1f5e57b3351bea904ea9f26ccf328d20df8cd320fa2a32140d62c629a3901";
        (bool ok,,, uint8 ht) = h.parse(good);
        assertTrue(ok && ht == 1, "good");
        bytes[8] memory bad = [
            bytes(hex"3045022027163bb4415546634540376c2cee64544bd781a39052f409e21f9de84bd7d2e002206ca8e1f5e57b3351bea904ea9f26ccf328d20df8cd320fa2a32140d62c629a3901"), // total length
            hex"3144022027163bb4415546634540376c2cee64544bd781a39052f409e21f9de84bd7d2e002206ca8e1f5e57b3351bea904ea9f26ccf328d20df8cd320fa2a32140d62c629a3901", // not a sequence
            hex"3044032027163bb4415546634540376c2cee64544bd781a39052f409e21f9de84bd7d2e002206ca8e1f5e57b3351bea904ea9f26ccf328d20df8cd320fa2a32140d62c629a3901", // R tag
            hex"3044022087163bb4415546634540376c2cee64544bd781a39052f409e21f9de84bd7d2e002206ca8e1f5e57b3351bea904ea9f26ccf328d20df8cd320fa2a32140d62c629a3901", // negative R
            hex"3044021f27163bb4415546634540376c2cee64544bd781a39052f409e21f9de84bd7d2e002206ca8e1f5e57b3351bea904ea9f26ccf328d20df8cd320fa2a32140d62c629a3901", // R length
            hex"3044022027163bb4415546634540376c2cee64544bd781a39052f409e21f9de84bd7d2e002206ca8e1f5e57b3351bea904ea9f26ccf328d20df8cd320fa2a32140d62c629a39", // no hash type
            hex"30450220000000000000000000000000000000000000000000000000000000000000000102206ca8e1f5e57b3351bea904ea9f26ccf328d20df8cd320fa2a32140d62c629a390101", // wrong lengths
            hex"30060201000201010001" // r = 0
        ];
        for (uint256 i; i < bad.length; ++i) {
            (ok,,,) = h.parse(bad[i]);
            assertTrue(!ok, string.concat("bad DER ", str(i)));
        }
    }
}
