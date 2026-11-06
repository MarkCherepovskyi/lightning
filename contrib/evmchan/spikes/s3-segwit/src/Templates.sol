// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Tx} from "s2/BtcTx.sol";
import {Bip143} from "./Bip143.sol";
import {Ecdsa} from "./Ecdsa.sol";

/// Everything a script check needs about the input being spent, plus the
/// stream of hints (see Ecdsa.sol; one 33-byte ECDSA hint per signature, in
/// the order the signatures are checked, and 32 bytes per Schnorr signature).
struct SpendCtx {
    Tx t;
    Bip143.Precomputed pc;
    uint256 index;
    uint64 amount;
    bytes hints;
    uint256 cursor;
}

// Input-level failures carry the input index.
error BadSignatureEncoding(uint256 input);
error BadSignature(uint256 input);
error MissingHint(uint256 input);
error KeyHashMismatch(uint256 input);
error WitnessScriptMismatch(uint256 input);
error UnknownWitnessScript(uint256 input);
error BadWitness(uint256 input);
error BadPreimage(uint256 input);
error CsvNotMet(uint256 input);
error CltvNotMet(uint256 input);

/// @title The output scripts Core Lightning spends, recognised by exact bytes
/// (bitcoin/script.c), and the checks each spend path's Bitcoin Script makes.
///
/// @notice Each check below is the Script semantics of its template, with the
/// witness stack as Script would see it: the stack size is exact (CLEANSTACK,
/// consensus for segwit), the path is chosen by the same data Script uses, and
/// every opcode's condition is enforced (CHECKSIG/CHECKMULTISIG via BIP143 and
/// ECDSA, HASH160 of a preimage, OP_SIZE 32, CSV against nSequence, CLTV
/// against nLockTime). BIP68 (the spent output's age against nSequence) is
/// checked for the whole transaction by the validator. Where Bitcoin policy is
/// stricter than consensus we follow policy, which only ever rejects:
/// MINIMALIF (an IF argument is empty or 0x01), NULLFAIL (a signature that is
/// checked must be valid or, where Script allows a false result, empty),
/// NULLDUMMY, LOW_S, STRICTENC hash types, compressed keys only.
library Templates {
    uint256 internal constant MAX_ELEMENT = 520;
    uint32 internal constant SEQUENCE_FINAL = 0xffffffff;
    uint32 internal constant SEQ_DISABLE = 1 << 31;
    uint32 internal constant SEQ_TYPE = 1 << 22;
    uint32 internal constant SEQ_MASK = 0xffff;
    uint32 internal constant LOCKTIME_THRESHOLD = 500_000_000;

    // ------------------------------------------------------------ P2WPKH

    function checkP2wpkh(SpendCtx memory c, bytes20 program, bytes[] memory w) internal pure {
        if (w.length != 2) revert BadWitness(c.index);
        if (ripemd160(abi.encodePacked(sha256(w[1]))) != program) revert KeyHashMismatch(c.index);
        // scriptCode: OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG
        _checkSig(c, w[0], w[1], abi.encodePacked(hex"76a914", program, hex"88ac"));
    }

    // ------------------------------------------------------------ P2WSH

    function checkP2wsh(SpendCtx memory c, bytes32 program, bytes[] memory w) internal pure {
        if (w.length == 0) revert BadWitness(c.index);
        bytes memory ws = w[w.length - 1];
        if (sha256(ws) != program) revert WitnessScriptMismatch(c.index);
        bytes[] memory st = new bytes[](w.length - 1);
        for (uint256 i; i < st.length; ++i) {
            if (w[i].length > MAX_ELEMENT) revert BadWitness(c.index);
            st[i] = w[i];
        }

        bool ok;
        bytes memory a;
        bytes memory b;
        uint256 n;
        (ok, a, b) = matchFunding(ws);
        if (ok) return _spendFunding(c, st, ws, a, b);
        (ok, a, b, n) = matchToLocal(ws);
        if (ok) return _spendToLocal(c, st, ws, a, b, n);
        (ok, a, n) = matchToRemote(ws);
        if (ok) return _spendToRemote(c, st, ws, a, n);
        (ok, a) = matchAnchor(ws);
        if (ok) return _spendAnchor(c, st, ws, a);
        Htlc memory h;
        (ok, h) = matchHtlcOffered(ws);
        if (ok) return _spendHtlc(c, st, ws, h);
        (ok, h) = matchHtlcReceived(ws);
        if (ok) return _spendHtlc(c, st, ws, h);
        revert UnknownWitnessScript(c.index);
    }

    // ------------------------------------------------------------ recognisers
    // Each extracts the fields at their fixed offsets, rebuilds the script the
    // way bitcoin/script.c does, and compares all bytes.

    /// bitcoin_redeem_2of2: 2 <key1> <key2> 2 OP_CHECKMULTISIG
    function matchFunding(bytes memory ws) internal pure returns (bool, bytes memory k1, bytes memory k2) {
        if (ws.length != 71) return (false, k1, k2);
        k1 = _slice(ws, 2, 33);
        k2 = _slice(ws, 36, 33);
        return (_eq(ws, abi.encodePacked(hex"5221", k1, hex"21", k2, hex"52ae")), k1, k2);
    }

    /// bitcoin_wscript_to_local and bitcoin_wscript_htlc_tx (same script):
    /// OP_IF <revocationkey> OP_ELSE <delay> OP_CSV OP_DROP <delayedkey> OP_ENDIF OP_CHECKSIG
    function matchToLocal(bytes memory ws)
        internal
        pure
        returns (bool, bytes memory rev, bytes memory delayed, uint256 delay)
    {
        if (ws.length < 75 || ws[0] != 0x63 || ws[1] != 0x21) return (false, rev, delayed, 0);
        (bool ok, uint256 d, uint256 next) = _readNum(ws, 36);
        if (!ok || next + 38 != ws.length) return (false, rev, delayed, 0);
        delay = d;
        rev = _slice(ws, 2, 33);
        delayed = _slice(ws, next + 3, 33);
        bytes memory expect =
            abi.encodePacked(hex"6321", rev, hex"67", _num(delay), hex"b27521", delayed, hex"68ac");
        return (_eq(ws, expect), rev, delayed, delay);
    }

    /// bitcoin_wscript_to_remote_anchored: <key> OP_CHECKSIGVERIFY <csv> OP_CSV
    function matchToRemote(bytes memory ws) internal pure returns (bool, bytes memory key, uint256 csv) {
        if (ws.length < 37 || ws[0] != 0x21) return (false, key, 0);
        (bool ok, uint256 n, uint256 next) = _readNum(ws, 35);
        if (!ok || next + 1 != ws.length) return (false, key, 0);
        csv = n;
        key = _slice(ws, 1, 33);
        return (_eq(ws, abi.encodePacked(hex"21", key, hex"ad", _num(csv), hex"b2")), key, csv);
    }

    /// bitcoin_wscript_anchor: <key> OP_CHECKSIG OP_IFDUP OP_NOTIF 16 OP_CSV OP_ENDIF
    function matchAnchor(bytes memory ws) internal pure returns (bool, bytes memory key) {
        if (ws.length != 40) return (false, key);
        key = _slice(ws, 1, 33);
        return (_eq(ws, abi.encodePacked(hex"21", key, hex"ac7364", hex"60", hex"b268")), key);
    }

    struct Htlc {
        bool offered;
        bytes20 revocationHash; // RIPEMD160(SHA256(revocationpubkey))
        bytes remoteKey;
        bytes localKey;
        bytes20 paymentHash; // RIPEMD160(payment_hash)
        uint256 cltv; // received only
    }

    /// bitcoin_wscript_htlc_offer, option_anchors variant:
    /// OP_DUP OP_HASH160 <revh> OP_EQUAL OP_IF OP_CHECKSIG OP_ELSE
    ///   <remotekey> OP_SWAP OP_SIZE 32 OP_EQUAL OP_NOTIF
    ///     OP_DROP 2 OP_SWAP <localkey> 2 OP_CHECKMULTISIG
    ///   OP_ELSE OP_HASH160 <ph> OP_EQUALVERIFY OP_CHECKSIG OP_ENDIF
    ///   1 OP_CSV OP_DROP OP_ENDIF
    function matchHtlcOffered(bytes memory ws) internal pure returns (bool, Htlc memory h) {
        if (ws.length != 136) return (false, h);
        h.offered = true;
        h.revocationHash = _bytes20(ws, 3);
        h.remoteKey = _slice(ws, 28, 33);
        h.localKey = _slice(ws, 71, 33);
        h.paymentHash = _bytes20(ws, 109);
        bytes memory expect = abi.encodePacked(
            hex"76a914", h.revocationHash, hex"8763ac6721", h.remoteKey, hex"7c82012087", hex"6475527c21", h.localKey
        );
        expect = abi.encodePacked(expect, hex"52ae67a914", h.paymentHash, hex"88ac6851b27568");
        return (_eq(ws, expect), h);
    }

    /// bitcoin_wscript_htlc_receive, option_anchors variant:
    /// OP_DUP OP_HASH160 <revh> OP_EQUAL OP_IF OP_CHECKSIG OP_ELSE
    ///   <remotekey> OP_SWAP OP_SIZE 32 OP_EQUAL OP_IF
    ///     OP_HASH160 <ph> OP_EQUALVERIFY 2 OP_SWAP <localkey> 2 OP_CHECKMULTISIG
    ///   OP_ELSE OP_DROP <cltv> OP_CLTV OP_DROP OP_CHECKSIG OP_ENDIF
    ///   1 OP_CSV OP_DROP OP_ENDIF
    function matchHtlcReceived(bytes memory ws) internal pure returns (bool, Htlc memory h) {
        if (ws.length < 139 || ws[0] != 0x76) return (false, h);
        (bool ok, uint256 n, uint256 next) = _readNum(ws, 130);
        if (!ok || next + 8 != ws.length) return (false, h);
        h.cltv = n;
        h.revocationHash = _bytes20(ws, 3);
        h.remoteKey = _slice(ws, 28, 33);
        h.paymentHash = _bytes20(ws, 69);
        h.localKey = _slice(ws, 93, 33);
        bytes memory expect = abi.encodePacked(
            hex"76a914", h.revocationHash, hex"8763ac6721", h.remoteKey, hex"7c82012087", hex"63a914", h.paymentHash
        );
        expect = abi.encodePacked(expect, hex"88527c21", h.localKey, hex"52ae6775", _num(h.cltv), hex"b175ac6851b27568");
        return (_eq(ws, expect), h);
    }

    // ------------------------------------------------------------ spend paths

    /// <> <sig1> <sig2>. CHECKMULTISIG pairs sig2 with key2 first, then sig1
    /// with key1; with 2 of 2 no key can be skipped, so both must be valid.
    function _spendFunding(SpendCtx memory c, bytes[] memory st, bytes memory ws, bytes memory k1, bytes memory k2)
        private
        pure
    {
        if (st.length != 3 || st[0].length != 0) revert BadWitness(c.index); // NULLDUMMY
        _checkSig(c, st[1], k1, ws);
        _checkSig(c, st[2], k2, ws);
    }

    /// Revocation: <revsig> 1. Delayed: <delayedsig> <> after `delay` blocks.
    function _spendToLocal(
        SpendCtx memory c,
        bytes[] memory st,
        bytes memory ws,
        bytes memory rev,
        bytes memory delayed,
        uint256 delay
    ) private pure {
        if (st.length != 2) revert BadWitness(c.index);
        if (_isTrue(c, st[1])) {
            _checkSig(c, st[0], rev, ws);
        } else {
            _csv(c, delay);
            _checkSig(c, st[0], delayed, ws);
        }
    }

    /// <sig>; CHECKSIGVERIFY, then CSV leaves <csv> as the result, so csv = 0
    /// would fail the script.
    function _spendToRemote(SpendCtx memory c, bytes[] memory st, bytes memory ws, bytes memory key, uint256 csv)
        private
        pure
    {
        if (st.length != 1 || csv == 0) revert BadWitness(c.index);
        _checkSig(c, st[0], key, ws);
        _csv(c, csv);
    }

    /// <sig> at any time, or <> after 16 blocks (CHECKSIG false, IFDUP leaves
    /// nothing, NOTIF runs 16 CSV, which leaves 16 as the true result).
    function _spendAnchor(SpendCtx memory c, bytes[] memory st, bytes memory ws, bytes memory key) private pure {
        if (st.length != 1) revert BadWitness(c.index);
        if (st[0].length == 0) {
            _csv(c, 16);
        } else {
            _checkSig(c, st[0], key, ws);
        }
    }

    /// The path is chosen by the top stack element x, as in Script:
    /// - HASH160(x) == revocation hash: <revsig> <revkey>.
    /// - else SIZE(x) == 32, offered: remote claims with <sig> <preimage>.
    /// - else SIZE(x) == 32, received: HTLC-success tx, <> <remotesig> <localsig> <preimage>.
    /// - else offered: HTLC-timeout tx, <> <remotesig> <localsig> <x>.
    /// - else received: remote after the timeout, <sig> <x>, CLTV.
    /// The last four also need 1 CSV. CHECKMULTISIG's key order is
    /// (remotekey, localkey), so the remote signature comes first.
    function _spendHtlc(SpendCtx memory c, bytes[] memory st, bytes memory ws, Htlc memory h) private pure {
        if (st.length == 0) revert BadWitness(c.index);
        bytes memory x = st[st.length - 1];
        if (ripemd160(abi.encodePacked(sha256(x))) == h.revocationHash) {
            if (st.length != 2) revert BadWitness(c.index);
            _checkSig(c, st[0], x, ws);
            return;
        }
        bool preimage = x.length == 32;
        if (preimage && ripemd160(abi.encodePacked(sha256(x))) != h.paymentHash) revert BadPreimage(c.index);
        if (preimage != h.offered) {
            // the 2-of-2 to the HTLC-success (received) or HTLC-timeout (offered) tx
            if (st.length != 4 || st[0].length != 0) revert BadWitness(c.index);
            _checkSig(c, st[1], h.remoteKey, ws);
            _checkSig(c, st[2], h.localKey, ws);
        } else {
            if (st.length != 2) revert BadWitness(c.index);
            if (!h.offered) _cltv(c, h.cltv);
            _checkSig(c, st[0], h.remoteKey, ws);
        }
        _csv(c, 1);
    }

    // ------------------------------------------------------------ opcodes

    /// OP_CHECKSIG under NULLFAIL: the signature must be valid.
    function _checkSig(SpendCtx memory c, bytes memory sig, bytes memory pubkey, bytes memory scriptCode)
        private
        pure
    {
        (bool ok, uint256 r, uint256 s, uint8 hashType) = Ecdsa.parse(sig);
        if (!ok || !Bip143.validHashType(hashType)) revert BadSignatureEncoding(c.index);
        bytes32 digest = Bip143.sigHash(c.t, c.pc, c.index, scriptCode, c.amount, hashType);
        if (c.cursor + 33 > c.hints.length) revert MissingHint(c.index);
        bytes memory hints = c.hints;
        uint256 off = c.cursor;
        uint8 recId = uint8(hints[off]);
        uint256 keyY;
        assembly ("memory-safe") {
            keyY := mload(add(add(hints, 33), off))
        }
        c.cursor = off + 33;
        if (!Ecdsa.verify(pubkey, digest, r, s, recId, keyY)) revert BadSignature(c.index);
    }

    /// OP_CHECKSEQUENCEVERIFY (BIP112) with a non-negative argument from the
    /// script. The spent output's age is checked against nSequence by BIP68.
    function _csv(SpendCtx memory c, uint256 n) private pure {
        if (n & SEQ_DISABLE != 0) return; // NOP
        uint32 seq = c.t.inputs[c.index].sequence;
        if (c.t.version < 2 || seq & SEQ_DISABLE != 0) revert CsvNotMet(c.index);
        if ((seq & SEQ_TYPE) != (n & SEQ_TYPE)) revert CsvNotMet(c.index);
        if (seq & SEQ_MASK < n & SEQ_MASK) revert CsvNotMet(c.index);
    }

    /// OP_CHECKLOCKTIMEVERIFY (BIP65) with a non-negative argument from the
    /// script. nLockTime finality against the height is checked by the validator.
    function _cltv(SpendCtx memory c, uint256 n) private pure {
        uint32 lt = c.t.locktime;
        if ((n < LOCKTIME_THRESHOLD) != (lt < LOCKTIME_THRESHOLD)) revert CltvNotMet(c.index);
        if (n > lt) revert CltvNotMet(c.index);
        if (c.t.inputs[c.index].sequence == SEQUENCE_FINAL) revert CltvNotMet(c.index);
    }

    /// An OP_IF argument under MINIMALIF: 0x01 is true, empty is false.
    function _isTrue(SpendCtx memory c, bytes memory v) private pure returns (bool) {
        if (v.length == 0) return false;
        if (v.length == 1 && v[0] == 0x01) return true;
        revert BadWitness(c.index);
    }

    // ------------------------------------------------------------ script numbers and bytes

    /// A script number pushed by bitcoin/script.c add_number() at ws[off]:
    /// OP_0, OP_1..OP_16, or a 1-5 byte push of a non-negative little-endian
    /// number. Minimality is not checked here; callers rebuild the script with
    /// _num() and compare, which rejects every other encoding.
    function _readNum(bytes memory ws, uint256 off) private pure returns (bool ok, uint256 v, uint256 next) {
        if (off >= ws.length) return (false, 0, 0);
        uint8 op = uint8(ws[off]);
        if (op == 0) return (true, 0, off + 1);
        if (op >= 0x51 && op <= 0x60) return (true, op - 0x50, off + 1);
        if (op < 1 || op > 5 || off + 1 + op > ws.length) return (false, 0, 0);
        for (uint256 i; i < op; ++i) {
            v |= uint256(uint8(ws[off + 1 + i])) << (8 * i);
        }
        if (uint8(ws[off + op]) & 0x80 != 0) return (false, 0, 0); // negative
        return (true, v, off + 1 + op);
    }

    /// bitcoin/script.c add_number() (minimal, non-negative CScriptNum).
    function _num(uint256 v) private pure returns (bytes memory) {
        if (v == 0) return hex"00";
        if (v <= 16) return abi.encodePacked(uint8(0x50 + v));
        uint256 len = v <= 0x7f ? 1 : v <= 0x7fff ? 2 : v <= 0x7fffff ? 3 : v <= 0x7fffffff ? 4 : 5;
        bytes memory b = new bytes(len + 1);
        b[0] = bytes1(uint8(len));
        for (uint256 i; i < len; ++i) {
            b[1 + i] = bytes1(uint8(v >> (8 * i)));
        }
        return b;
    }

    function _eq(bytes memory a, bytes memory b) private pure returns (bool) {
        return a.length == b.length && keccak256(a) == keccak256(b);
    }

    function _bytes20(bytes memory b, uint256 off) private pure returns (bytes20) {
        require(off + 20 <= b.length);
        bytes32 w;
        assembly ("memory-safe") {
            w := mload(add(add(b, 32), off))
        }
        return bytes20(w);
    }

    function _slice(bytes memory b, uint256 off, uint256 len) private pure returns (bytes memory out) {
        require(off + len <= b.length);
        out = new bytes(len);
        assembly ("memory-safe") {
            mcopy(add(out, 32), add(add(b, 32), off), len)
        }
    }
}
