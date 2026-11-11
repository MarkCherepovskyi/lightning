// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BtcTx, TxView, Stack, Prevout} from "./BtcTx.sol";
import {Bip143} from "./Bip143.sol";
import {TaprootSighash} from "./TaprootSighash.sol";
import {Ecdsa} from "./Ecdsa.sol";
import {Schnorr} from "./Schnorr.sol";
import {
    BadWitness,
    KeyHashMismatch,
    WitnessScriptMismatch,
    UnknownWitnessScript,
    BadPreimage,
    CsvNotMet,
    CltvNotMet,
    BadSignatureEncoding,
    BadSignature,
    BadPublicKey,
    NoMatchingOutput
} from "./Errors.sol";

/// @title The scriptPubKeys the contract can spend (SPEC 4.3.7): P2WPKH, P2TR
/// key path, and P2WSH with the scripts Core Lightning builds for the anchors
/// channel type, recognised by exact bytes (bitcoin/script.c).
///
/// @notice Each P2WSH check is the Script semantics of its template, with the
/// witness stack as Script would see it: the stack size and the form of each
/// item are exactly those of SPEC 4.3.7's table (CLEANSTACK is consensus for
/// segwit), the path is chosen by the same data Script uses, and every
/// opcode's condition is enforced (CHECKSIG/CHECKMULTISIG via BIP143 and
/// ECDSA, HASH160 of a preimage, OP_SIZE 32, CSV against nSequence, CLTV
/// against nLockTime). BIP68 (the spent output's age against nSequence) and
/// nLockTime finality are checked for the whole tx by Validation. Where
/// Bitcoin policy is stricter than consensus we follow policy, which only
/// ever rejects: MINIMALIF (an IF argument is empty or 0x01), NULLFAIL (a
/// signature that is checked must be valid; only the anchor's `<>` path takes
/// a false CHECKSIG, with an empty signature), NULLDUMMY, LOW_S, STRICTENC hash
/// types, and every key a CHECKSIG uses a compressed point on the curve, even
/// with an empty signature (SPEC 4.3.8; stricter than Bitcoin's STRICTENC,
/// which checks only the encoding).
library Templates {
    uint32 internal constant SEQUENCE_FINAL = 0xffffffff;
    uint32 internal constant SEQ_DISABLE = 1 << 31;
    uint32 internal constant SEQ_TYPE = 1 << 22;
    uint32 internal constant SEQ_MASK = 0xffff;
    uint32 internal constant LOCKTIME_THRESHOLD = 500_000_000;

    /// The input being spent, with its witness.
    struct Input {
        uint256 index;
        uint64 amount;
        Stack w;
    }

    // ------------------------------------------------------------ P2WPKH

    /// Witness `<sig> <33-byte compressed key>`, hash160(key) == program.
    function p2wpkh(bytes calldata raw, TxView memory t, Input memory s, bytes20 program) internal view {
        Stack memory w = s.w;
        if (w.n != 2 || w.len[1] != 33) revert BadWitness(s.index);
        bytes memory key = BtcTx.item(raw, w, 1);
        if (ripemd160(abi.encodePacked(sha256(key))) != program) revert KeyHashMismatch(s.index);
        // scriptCode: OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG
        _checkSig(raw, t, s, BtcTx.item(raw, w, 0), key, abi.encodePacked(hex"76a914", program, hex"88ac"));
    }

    // ------------------------------------------------------------ P2TR key path

    /// One witness item: a 64-byte signature (DEFAULT), or 65 bytes ending in
    /// a defined hash type other than 0x00. One item means no annex and no
    /// script path.
    function p2tr(
        bytes calldata raw,
        TxView memory t,
        Prevout[] calldata pv,
        uint256 index,
        bytes32 outputKey,
        Stack memory w
    ) internal view {
        if (w.n != 1) revert BadWitness(index);
        uint256 off = w.off[0];
        uint8 hashType;
        if (w.len[0] == 65) {
            hashType = uint8(raw[off + 64]);
            if (hashType == 0 || !TaprootSighash.validHashType(hashType)) revert BadSignatureEncoding(index);
        } else if (w.len[0] != 64) {
            revert BadSignatureEncoding(index);
        }
        // BIP341: SINGLE without an output of the same index is a failure
        // (unlike BIP143's zero hashOutputs).
        if (hashType & 3 == TaprootSighash.SINGLE && index >= BtcTx.nOut(t)) revert NoMatchingOutput(index);
        uint256 rx;
        uint256 sig_s;
        assembly ("memory-safe") {
            rx := calldataload(add(raw.offset, off))
            sig_s := calldataload(add(raw.offset, add(off, 32)))
        }
        bytes32 digest = TaprootSighash.sigHash(raw, t, pv, index, hashType);
        if (!Schnorr.verify(outputKey, digest, rx, sig_s)) revert BadSignature(index);
    }

    // ------------------------------------------------------------ P2WSH

    function p2wsh(bytes calldata raw, TxView memory t, Input memory s, bytes32 program) internal view {
        Stack memory w = s.w;
        if (w.n == 0) revert BadWitness(s.index);
        bytes memory ws = BtcTx.item(raw, w, w.n - 1);
        if (sha256(ws) != program) revert WitnessScriptMismatch(s.index);
        uint256 len = ws.length;
        bytes1 op = len == 0 ? bytes1(0xff) : ws[0];

        if (op == 0x52 && len == 71) {
            (bool ok, bytes memory k1, bytes memory k2) = matchFunding(ws);
            if (ok) return _spendFunding(raw, t, s, ws, k1, k2);
        } else if (op == 0x63) {
            (bool ok, bytes memory rev, bytes memory delayed, uint256 delay) = matchToLocal(ws);
            if (ok) return _spendToLocal(raw, t, s, ws, rev, delayed, delay);
        } else if (op == 0x21 && len == 40) {
            (bool ok, bytes memory key) = matchAnchor(ws);
            if (ok) return _spendAnchor(raw, t, s, ws, key);
        } else if (op == 0x21) {
            (bool ok, bytes memory key) = matchToRemote(ws);
            if (ok) return _spendToRemote(raw, t, s, ws, key);
        } else if (op == 0x76) {
            (bool ok, Htlc memory h) = matchHtlcOffered(ws);
            if (!ok) (ok, h) = matchHtlcReceived(ws);
            if (ok) return _spendHtlc(raw, t, s, ws, h);
        }
        revert UnknownWitnessScript(s.index);
    }

    // ------------------------------------------------------------ recognisers
    // Each extracts the fields at their fixed offsets, rebuilds the script the
    // way bitcoin/script.c does, and compares all bytes.

    /// bitcoin_redeem_2of2: 2 <key1> <key2> 2 OP_CHECKMULTISIG, with
    /// key1 <= key2 (script.c sorts the keys by pubkey_cmp, a memcmp of the
    /// 33-byte encodings).
    function matchFunding(bytes memory ws) internal pure returns (bool, bytes memory k1, bytes memory k2) {
        if (ws.length != 71) return (false, k1, k2);
        k1 = _slice(ws, 2, 33);
        k2 = _slice(ws, 36, 33);
        if (!_leq(k1, k2)) return (false, k1, k2);
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
        bytes memory expect = abi.encodePacked(hex"6321", rev, hex"67", _num(delay), hex"b27521", delayed, hex"68ac");
        return (_eq(ws, expect), rev, delayed, delay);
    }

    /// bitcoin_wscript_to_remote_anchored with csv_lock 1 (SPEC 4.3.7's table
    /// has only `CSV 1`; another csv_lock is a lease, SPEC 5.4):
    /// <key> OP_CHECKSIGVERIFY 1 OP_CSV
    function matchToRemote(bytes memory ws) internal pure returns (bool, bytes memory key) {
        if (ws.length != 37) return (false, key);
        key = _slice(ws, 1, 33);
        return (_eq(ws, abi.encodePacked(hex"21", key, hex"ad51b2")), key);
    }

    /// bitcoin_wscript_anchor: <key> OP_CHECKSIG OP_IFDUP OP_NOTIF 16 OP_CSV OP_ENDIF
    function matchAnchor(bytes memory ws) internal pure returns (bool, bytes memory key) {
        if (ws.length != 40) return (false, key);
        key = _slice(ws, 1, 33);
        return (_eq(ws, abi.encodePacked(hex"21", key, hex"ac736460b268")), key);
    }

    struct Htlc {
        bool offered;
        bytes20 revocationHash; // RIPEMD160(SHA256(revocationpubkey))
        bytes remoteKey;
        bytes localKey;
        bytes20 paymentHash; // RIPEMD160(payment_hash)
        uint256 cltv; // received only
    }

    /// bitcoin_wscript_htlc_offer, anchors variant:
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

    /// bitcoin_wscript_htlc_receive, anchors variant:
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
    // s.w.n - 1 is the number of stack items before the script; item k is
    // BtcTx.item(raw, s.w, k).

    /// `<> <sig1> <sig2>`. CHECKMULTISIG pairs sig2 with key2 first, then sig1
    /// with key1; with 2 of 2 no key can be skipped, so both must be valid.
    function _spendFunding(
        bytes calldata raw,
        TxView memory t,
        Input memory s,
        bytes memory ws,
        bytes memory k1,
        bytes memory k2
    ) private view {
        Stack memory w = s.w;
        if (w.n != 4 || w.len[0] != 0) revert BadWitness(s.index); // NULLDUMMY
        _checkSig(raw, t, s, BtcTx.item(raw, w, 1), k1, ws);
        _checkSig(raw, t, s, BtcTx.item(raw, w, 2), k2, ws);
    }

    /// Revocation: `<revsig> <01>`. Delayed: `<delayedsig> <>` and CSV delay.
    function _spendToLocal(
        bytes calldata raw,
        TxView memory t,
        Input memory s,
        bytes memory ws,
        bytes memory rev,
        bytes memory delayed,
        uint256 delay
    ) private view {
        Stack memory w = s.w;
        if (w.n != 3) revert BadWitness(s.index);
        if (w.len[1] == 0) {
            _csv(raw, t, s.index, delay);
            _checkSig(raw, t, s, BtcTx.item(raw, w, 0), delayed, ws);
        } else if (w.len[1] == 1 && raw[w.off[1]] == 0x01) {
            _checkSig(raw, t, s, BtcTx.item(raw, w, 0), rev, ws);
        } else {
            revert BadWitness(s.index); // MINIMALIF
        }
    }

    /// `<sig>` and CSV 1: CHECKSIGVERIFY, then 1 CSV leaves 1 as the result.
    function _spendToRemote(
        bytes calldata raw,
        TxView memory t,
        Input memory s,
        bytes memory ws,
        bytes memory key
    ) private view {
        if (s.w.n != 2) revert BadWitness(s.index);
        _checkSig(raw, t, s, BtcTx.item(raw, s.w, 0), key, ws);
        _csv(raw, t, s.index, 1);
    }

    /// `<sig>` at any time, or `<>` and CSV 16 (CHECKSIG is false, IFDUP
    /// leaves nothing, NOTIF runs 16 CSV, which leaves 16 as the true result).
    function _spendAnchor(
        bytes calldata raw,
        TxView memory t,
        Input memory s,
        bytes memory ws,
        bytes memory key
    ) private view {
        if (s.w.n != 2) revert BadWitness(s.index);
        if (s.w.len[0] == 0) {
            _key(s.index, key); // CHECKSIG still checks the key
            _csv(raw, t, s.index, 16);
        } else {
            _checkSig(raw, t, s, BtcTx.item(raw, s.w, 0), key, ws);
        }
    }

    /// The path is chosen by the top stack item x, as in Script, and each
    /// path's stack is exactly the one in SPEC 4.3.7's table:
    /// - HASH160(x) == revocation hash: `<revsig> <revocationpubkey>`.
    /// - offered, x a 32-byte preimage: remote success, `<remotesig> <x>`, CSV 1.
    /// - offered, x empty: HTLC-timeout tx, `<> <remotesig> <localsig> <>`, CSV 1.
    /// - received, x a 32-byte preimage: HTLC-success tx,
    ///   `<> <remotesig> <localsig> <x>`, CSV 1.
    /// - received, x empty: remote timeout, `<sig> <>`, CLTV cltv_expiry, CSV 1.
    /// A 32-byte x must hash to the payment hash. CHECKMULTISIG's key order is
    /// (remotekey, localkey), so the remote signature comes first.
    function _spendHtlc(bytes calldata raw, TxView memory t, Input memory s, bytes memory ws, Htlc memory h)
        private
        view
    {
        uint256 n = s.w.n - 1; // stack items before the script
        if (n == 0) revert BadWitness(s.index);
        bytes memory x = BtcTx.item(raw, s.w, n - 1);
        bytes20 xh = ripemd160(abi.encodePacked(sha256(x)));
        if (xh == h.revocationHash) {
            if (n != 2) revert BadWitness(s.index);
            _checkSig(raw, t, s, BtcTx.item(raw, s.w, 0), x, ws);
            return;
        }
        bool preimage = x.length == 32;
        if (!preimage && x.length != 0) revert BadWitness(s.index);
        if (preimage && xh != h.paymentHash) revert BadPreimage(s.index);
        if (preimage != h.offered) {
            // the 2-of-2 to the HTLC-success (received) or HTLC-timeout (offered) tx
            if (n != 4 || s.w.len[0] != 0) revert BadWitness(s.index); // NULLDUMMY
            _checkSig(raw, t, s, BtcTx.item(raw, s.w, 1), h.remoteKey, ws);
            _checkSig(raw, t, s, BtcTx.item(raw, s.w, 2), h.localKey, ws);
        } else {
            // remote success (offered) or remote timeout (received)
            if (n != 2) revert BadWitness(s.index);
            if (!h.offered) _cltv(raw, t, s.index, h.cltv);
            _checkSig(raw, t, s, BtcTx.item(raw, s.w, 0), h.remoteKey, ws);
        }
        _csv(raw, t, s.index, 1);
    }

    // ------------------------------------------------------------ opcodes

    /// OP_CHECKSIG under NULLFAIL: the signature must be valid.
    function _checkSig(
        bytes calldata raw,
        TxView memory t,
        Input memory s,
        bytes memory sig,
        bytes memory pubkey,
        bytes memory scriptCode
    ) private view {
        (bool ok, uint256 r, uint256 sigS, uint8 hashType) = Ecdsa.parse(sig);
        if (!ok || !Bip143.validHashType(hashType)) revert BadSignatureEncoding(s.index);
        bytes32 digest = Bip143.sigHash(raw, t, s.index, scriptCode, s.amount, hashType);
        _verify(s.index, pubkey, digest, r, sigS);
    }

    function _verify(uint256 index, bytes memory pubkey, bytes32 digest, uint256 r, uint256 sigS) private view {
        (uint256 x, uint256 y) = _key(index, pubkey);
        if (!Ecdsa.verify(x, y, digest, r, sigS)) revert BadSignature(index);
    }

    /// A key a CHECKSIG uses: a 33-byte compressed point on the curve.
    function _key(uint256 index, bytes memory pubkey) private view returns (uint256 x, uint256 y) {
        bool ok;
        (ok, x, y) = Ecdsa.liftKey(pubkey);
        if (!ok) revert BadPublicKey(index);
    }

    /// `CSV n`, Bitcoin's CheckSequence as SPEC 4.3.7 defines it: the tx
    /// version (a uint32) is at least 2, the input's sequence has the disable
    /// bit (31) clear and the type bit (22) clear (n is always a number of
    /// blocks here), and sequence & 0xffff >= n. BIP68 (SPEC 4.3.6) applies
    /// on top, in Validation.
    /// @dev n is compared whole, so an n of 2^16 or more (which no CLN script
    /// has: to_self_delay is a u16, and leases are refused by the plugin) is
    /// never met, and a to_local script with such a delay is unspendable. Bitcoin would treat an n with bit 31 set as a NOP and
    /// compare only n's low 16 bits and type bit otherwise; this only rejects more.
    function _csv(bytes calldata raw, TxView memory t, uint256 index, uint256 n) private pure {
        uint32 seq = BtcTx.sequence(raw, t, index);
        if (t.version < 2 || seq & SEQ_DISABLE != 0 || seq & SEQ_TYPE != 0 || (seq & SEQ_MASK) < n) {
            revert CsvNotMet(index);
        }
    }

    /// `CLTV x`, Bitcoin's CheckLockTime: nLockTime and x are both heights
    /// (< 500,000,000) or both times, nLockTime >= x, and the input's sequence
    /// is not final. nLockTime finality itself (SPEC 4.3.5) is in Validation.
    function _cltv(bytes calldata raw, TxView memory t, uint256 index, uint256 x) private pure {
        uint32 lt = t.locktime;
        if ((x < LOCKTIME_THRESHOLD) != (lt < LOCKTIME_THRESHOLD) || lt < x) revert CltvNotMet(index);
        if (BtcTx.sequence(raw, t, index) == SEQUENCE_FINAL) revert CltvNotMet(index);
    }

    // ------------------------------------------------------------ script numbers and bytes

    /// A script number pushed by bitcoin/script.c add_number() at ws[off]:
    /// OP_0, OP_1..OP_16, or a 1-5 byte push of a non-negative little-endian
    /// number (a CScriptNum of up to 5 bytes). Minimality is not checked here;
    /// callers rebuild the script with _num() and compare, which rejects every
    /// other encoding.
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
        // add_number() takes a u32: a bigger number is not a CLN script
        if (v > type(uint32).max) return (false, 0, 0);
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

    /// a <= b for two 33-byte strings, as memcmp orders them.
    function _leq(bytes memory a, bytes memory b) private pure returns (bool) {
        uint256 a0;
        uint256 b0;
        uint256 a1;
        uint256 b1;
        assembly ("memory-safe") {
            a0 := mload(add(a, 32))
            b0 := mload(add(b, 32))
            a1 := byte(0, mload(add(a, 64)))
            b1 := byte(0, mload(add(b, 64)))
        }
        return a0 < b0 || (a0 == b0 && a1 <= b1);
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
