// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {Harness} from "./Harness.sol";
import {Vectors, KeyPathInput, SignedSpend} from "./Vectors.sol";
import {BtcTx, Tx, TxOut} from "../src/BtcTx.sol";
import {Schnorr} from "../src/Schnorr.sol";
import {TaprootSighash} from "../src/TaprootSighash.sol";
import {TaprootKeyPath} from "../src/TaprootKeyPath.sol";

contract TaprootSighashTest is TestBase {
    Harness h = new Harness();

    function unsignedTx() internal pure returns (Tx memory t) {
        (t,,) = BtcTx.parse(Vectors.BIP341_RAW_UNSIGNED_TX);
    }

    function wit(bytes memory sig) internal pure returns (bytes[] memory w) {
        w = new bytes[](1);
        w[0] = sig;
    }

    function outputKey(TxOut memory o) internal pure returns (bytes32 k) {
        bytes memory spk = o.scriptPubKey;
        assembly {
            k := mload(add(spk, 34))
        }
    }

    function testPrecomputedHashes() public pure {
        Tx memory t = unsignedTx();
        assertEq(t.inputs.length, 9, "inputs");
        assertEq(t.outputs.length, 2, "outputs");
        TaprootSighash.Precomputed memory pc = TaprootSighash.precompute(t, Vectors.bip341Spent());
        assertEq(pc.prevouts, Vectors.BIP341_HASHPREVOUTS, "hashPrevouts");
        assertEq(pc.amounts, Vectors.BIP341_HASHAMOUNTS, "hashAmounts");
        assertEq(pc.scriptPubKeys, Vectors.BIP341_HASHSCRIPTPUBKEYS, "hashScriptPubkeys");
        assertEq(pc.sequences, Vectors.BIP341_HASHSEQUENCES, "hashSequences");
        assertEq(pc.outputs, Vectors.BIP341_HASHOUTPUTS, "hashOutputs");
    }

    /// sigMsg, sigHash and signature for each of the 7 key-path inputs
    /// (hash types 3, 0x83, 1, 0, 2, 0x82, 0x81).
    function testKeyPathVectors() public view {
        Tx memory t = unsignedTx();
        TxOut[] memory spent = Vectors.bip341Spent();
        TaprootSighash.Precomputed memory pc = TaprootSighash.precompute(t, spent);
        uint256 seen;
        for (uint256 j; j < Vectors.BIP341_INPUT_COUNT; ++j) {
            KeyPathInput memory v = Vectors.bip341Input(j);
            seen |= uint256(1) << (v.hashType & 3 | (v.hashType >> 5));
            assertEq(TaprootSighash.sigMsg(t, spent, pc, v.index, v.hashType), v.sigMsg, str(j));
            bytes32 sh = TaprootSighash.sigHash(t, spent, pc, v.index, v.hashType);
            assertEq(sh, v.sigHash, str(j));
            assertTrue(Schnorr.verify(outputKey(spent[v.index]), sh, sliceSig(v.sig), v.ry), str(j));
            assertTrue(TaprootKeyPath.verifyInput(t, spent, pc, v.index, wit(v.sig), v.ry), str(j));
            // Through the external harness (calldata structs) too.
            assertEq(h.sigHash(t, spent, v.index, v.hashType), v.sigHash, str(j));
        }
        // DEFAULT, ALL, NONE, SINGLE and the three ANYONECANPAY types.
        assertEq(seen, 0x01 | 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80, "hash types covered");
    }

    /// Parses the fully signed vector tx and checks every P2TR input from its
    /// own witnesses.
    function testFullySignedTx() public pure {
        (Tx memory t, bytes[][] memory w,) = BtcTx.parse(Vectors.BIP341_FULLY_SIGNED_TX);
        Tx memory u = unsignedTx();
        assertEq(keccak256(abi.encode(t.inputs.length, t.outputs, t.version, t.locktime)),
            keccak256(abi.encode(u.inputs.length, u.outputs, u.version, u.locktime)), "same tx");
        TxOut[] memory spent = Vectors.bip341Spent();
        TaprootSighash.Precomputed memory pc = TaprootSighash.precompute(t, spent);
        for (uint256 j; j < Vectors.BIP341_INPUT_COUNT; ++j) {
            KeyPathInput memory v = Vectors.bip341Input(j);
            assertEq(w[v.index].length, 1, "one witness item");
            assertEq(w[v.index][0], v.sig, "witness is the signature");
            assertTrue(TaprootKeyPath.verifyInput(t, spent, pc, v.index, w[v.index], v.ry), str(j));
        }
        // Input 2 is P2PKH (scriptSig, no witness), input 5 P2WPKH: not key path.
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, 2, w[2], 0), "p2pkh");
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, 5, w[5], 0), "p2wpkh");
    }

    /// Changes to the tx or the spent outputs break exactly the signatures
    /// whose hash type commits to the changed field.
    function testCommitments() public pure {
        // Order of the 7 inputs: SINGLE(in 0), SINGLE|ACP(in 1), ALL(in 3),
        // DEFAULT(in 4), NONE(in 6), NONE|ACP(in 7), ALL|ACP(in 8).
        // Output 0's amount: ALL-like types break, and SINGLE of input 0.
        _mutate(0, 0, [true, false, true, true, false, false, true]);
        // Output 1's amount: ALL-like types break, and SINGLE|ACP of input 1.
        _mutate(0, 1, [false, true, true, true, false, false, true]);
        // Input 0's sequence: every non-ACP type breaks (sha_sequences).
        _mutate(2, 0, [true, false, true, true, true, false, false]);
        // A spent amount of the non-taproot input 2: non-ACP breaks.
        _mutate(3, 2, [true, false, true, true, true, false, false]);
        // A spent scriptPubKey of input 2: non-ACP breaks.
        _mutate(4, 2, [true, false, true, true, true, false, false]);
        // Version and locktime: everything breaks.
        _mutate(5, 0, [true, true, true, true, true, true, true]);
        _mutate(6, 0, [true, true, true, true, true, true, true]);
        // Input 1's own prevout (ACP, commits to its outpoint): only non-ACP and input 1 break.
        _mutate(7, 1, [true, true, true, true, true, false, false]);
    }

    /// kind: 0 output amount (which = output index), 2 input
    /// sequence, 3 spent amount, 4 spent script, 5 version, 6 locktime, 7 prevout vout.
    function _mutate(uint256 kind, uint256 which, bool[7] memory breaks) internal pure {
        Tx memory t = unsignedTx();
        TxOut[] memory spent = Vectors.bip341Spent();
        if (kind == 0) t.outputs[which].amount += 1;
        if (kind == 2) t.inputs[which].sequence ^= 1;
        if (kind == 3) spent[which].amount += 1;
        if (kind == 4) spent[which].scriptPubKey = bytes.concat(spent[which].scriptPubKey, hex"00");
        if (kind == 5) t.version += 1;
        if (kind == 6) t.locktime += 1;
        if (kind == 7) t.inputs[which].prevVout += 1;
        TaprootSighash.Precomputed memory pc = TaprootSighash.precompute(t, spent);
        for (uint256 j; j < Vectors.BIP341_INPUT_COUNT; ++j) {
            KeyPathInput memory v = Vectors.bip341Input(j);
            bool ok = TaprootKeyPath.verifyInput(t, spent, pc, v.index, wit(v.sig), v.ry);
            assertTrue(ok == !breaks[j], string.concat("mutation ", str(kind), " input ", str(j)));
        }
    }

    function testHashTypeHandling() public pure {
        Tx memory t = unsignedTx();
        TxOut[] memory spent = Vectors.bip341Spent();
        TaprootSighash.Precomputed memory pc = TaprootSighash.precompute(t, spent);
        KeyPathInput memory v = Vectors.bip341Input(3); // DEFAULT, 64-byte sig, input 4
        assertEq(v.sig.length, 64, "default sig");
        // 0x00 appended explicitly: invalid (BIP341: 65-byte sig needs hash_type != 0).
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, v.index, wit(bytes.concat(v.sig, hex"00")), v.ry), "65-byte 0x00");
        // Same sig claiming ALL: different sighash.
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, v.index, wit(bytes.concat(v.sig, hex"01")), v.ry), "as ALL");
        KeyPathInput memory a = Vectors.bip341Input(2); // ALL (0x01), input 3
        bytes memory sig = bytes.concat(a.sig);
        uint8[8] memory others = [0x00, 0x02, 0x03, 0x04, 0x80, 0x81, 0x84, 0xff];
        for (uint256 i; i < others.length; ++i) {
            sig[64] = bytes1(others[i]);
            assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, a.index, wit(sig), a.ry), str(others[i]));
        }
        // Dropping the type byte of an ALL sig makes it DEFAULT: different sighash.
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, a.index, wit(sliceSig(a.sig)), a.ry), "ALL as DEFAULT");
        // Bad witness shapes.
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, a.index, new bytes[](0), a.ry), "empty stack");
        bytes[] memory two = new bytes[](2);
        two[0] = a.sig;
        two[1] = hex"50";
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, a.index, two, a.ry), "annex");
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, a.index, wit(bytes.concat(a.sig, hex"00")), a.ry), "66 bytes");
        // A non-empty scriptSig on a P2TR input.
        t.inputs[a.index].scriptSig = hex"00";
        assertFalse(TaprootKeyPath.verifyInput(t, spent, pc, a.index, wit(a.sig), a.ry), "scriptSig");
    }

    function testSighashReverts() public {
        Tx memory t = unsignedTx();
        TxOut[] memory spent = Vectors.bip341Spent();
        uint8[5] memory bad = [0x04, 0x80, 0x84, 0x7f, 0xff];
        for (uint256 i; i < bad.length; ++i) {
            vm.expectRevert(abi.encodeWithSelector(TaprootSighash.BadHashType.selector, bad[i]));
            h.sigHash(t, spent, 0, bad[i]);
        }
        vm.expectRevert(TaprootSighash.BadInputIndex.selector);
        h.sigHash(t, spent, 9, 0x00);
        // SINGLE for input 3 with only 2 outputs (BIP341: fail, no "1" quirk).
        vm.expectRevert(TaprootSighash.NoMatchingOutput.selector);
        h.sigHash(t, spent, 3, 0x03);
        vm.expectRevert(TaprootSighash.NoMatchingOutput.selector);
        h.sigHash(t, spent, 3, 0x83);
        TxOut[] memory fewer = new TxOut[](8);
        for (uint256 i; i < 8; ++i) fewer[i] = spent[i];
        vm.expectRevert(TaprootSighash.SpentOutputsMismatch.selector);
        h.sigHash(t, fewer, 0, 0x00);
    }

    function testSignedSpends() public view {
        SignedSpend memory s1 = Vectors.spend1In();
        SignedSpend memory s2 = Vectors.spend2In();
        assertTrue(h.verifyTx(s1.rawTx, s1.spent, s1.ry), "1-in spend");
        assertTrue(h.verifyTx(s2.rawTx, s2.spent, s2.ry), "2-in spend");
        (,, bytes32 txid1) = h.parse(s1.rawTx);
        (,, bytes32 txid2) = h.parse(s2.rawTx);
        assertEq(txid1, Vectors.SPEND1_TXID, "txid 1");
        assertEq(txid2, Vectors.SPEND2_TXID, "txid 2");
        // Swapping the two inputs' signatures fails.
        (Tx memory t, bytes[][] memory w,) = BtcTx.parse(s2.rawTx);
        TaprootSighash.Precomputed memory pc = TaprootSighash.precompute(t, s2.spent);
        assertFalse(TaprootKeyPath.verifyInput(t, s2.spent, pc, 0, w[1], s2.ry[1]), "swapped sig");
        // Wrong spent amount fails.
        s1.spent[0].amount -= 1;
        assertFalse(h.verifyTx(s1.rawTx, s1.spent, s1.ry), "wrong amount");
    }

    function testParserStrictness() public {
        bytes memory raw = Vectors.spend1In().rawTx;
        vm.expectRevert(BtcTx.TrailingBytes.selector);
        h.parse(bytes.concat(raw, hex"00"));
        vm.expectRevert(BtcTx.Truncated.selector);
        h.parse(sliceBytes(raw, 0, raw.length - 1));
        // Segwit flag 2.
        bytes memory bad = bytes.concat(raw);
        bad[5] = 0x02;
        vm.expectRevert(BtcTx.BadSegwitFlag.selector);
        h.parse(bad);
        // Non-canonical input count: 0xfd 0x01 0x00 instead of 0x01.
        bad = bytes.concat(sliceBytes(raw, 0, 6), hex"fd0100", sliceBytes(raw, 7, raw.length - 7));
        vm.expectRevert(BtcTx.NonCanonicalCompactSize.selector);
        h.parse(bad);
        // Segwit serialization with every witness empty: the 1-in tx ends with
        // witness (01 40 <64 bytes>) then locktime.
        bad = bytes.concat(sliceBytes(raw, 0, raw.length - 4 - 66), hex"00", sliceBytes(raw, raw.length - 4, 4));
        vm.expectRevert(BtcTx.EmptyWitnesses.selector);
        h.parse(bad);
        // No inputs (legacy layout: version, 0x00 would be the marker; with
        // flag 1 and then 0 inputs).
        vm.expectRevert(BtcTx.NoInputs.selector);
        h.parse(hex"02000000000100");
    }

    function sliceSig(bytes memory sig) internal pure returns (bytes memory) {
        return sliceBytes(sig, 0, 64);
    }

    function sliceBytes(bytes memory b, uint256 off, uint256 len) internal pure returns (bytes memory out) {
        out = new bytes(len);
        for (uint256 i; i < len; ++i) out[i] = b[off + i];
    }
}
