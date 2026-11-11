// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {
    TxTooLarge,
    Truncated,
    NonCanonicalCompactSize,
    BadSegwitFlag,
    EmptyWitnesses,
    NoInputs,
    NoOutputs,
    TrailingBytes,
    ScriptSigNotEmpty,
    BadWitness
} from "./Errors.sol";

/// An output being spent, as `submit` takes it (SPEC 4.2). `height` is the
/// height it was created at. All three are bound to the UTXO set by
/// utxo[key] == keccak256(amount, scriptPubKey, height).
struct Prevout {
    uint64 amount;
    bytes scriptPubKey;
    uint32 height;
}

/// A parsed transaction: offsets into the raw bytes (calldata) rather than
/// copies, plus the hashes of its parts, computed on first use.
///
/// Every scriptSig is empty (the parser rejects any other, SPEC 4.3.2), so
/// every input is 41 bytes and input i starts at inStart + 41 * i:
/// prevout txid (32), prevout index (4, LE), scriptSig length 0x00 (1),
/// sequence (4, LE).
struct TxView {
    uint32 version;
    uint32 locktime;
    uint256 nIn;
    uint256 inStart; // offset of input 0
    uint256 bodyStart; // offset of the input count (after the segwit marker and flag)
    /// outOff[j] is the offset of output j, outOff[nOut] the end of the last
    /// output, so output j is raw[outOff[j]:outOff[j + 1]].
    uint256[] outOff;
    /// Offset of input i's witness (its item count), or NO_WITNESS.
    uint256[] witOff;
    bytes32 txid; // double SHA256 of the serialization without witness, internal byte order
    // Single SHA256 of the concatenations BIP341 defines (BIP143 hashes each
    // once more). Bit k of `have` is set once the k-th of them is computed.
    uint256 have;
    bytes32 shaPrevouts;
    bytes32 shaSequences;
    bytes32 shaOutputs;
    bytes32 shaAmounts;
    bytes32 shaScripts;
}

/// The witness items of one input, as offsets and lengths in the raw tx.
/// No template has more than 5 items (HTLC-success: 4 and the script), so a
/// longer witness is rejected when it is read.
struct Stack {
    uint256 n;
    uint256[5] off;
    uint256[5] len;
}

/// @title Strict parser for segwit transactions, reading calldata in place.
/// @notice SPEC 4.3.1: at most 100,000 bytes; canonical compact sizes; no
/// trailing bytes; marker and flag 00 01, and only if some witness is
/// non-empty; at least one input and one output; the version as a uint32.
/// SPEC 4.3.2's empty scriptSig is enforced here too.
library BtcTx {
    uint256 internal constant MAX_TX_SIZE = 100_000;
    uint256 internal constant INPUT_SIZE = 41;
    uint256 internal constant MAX_STACK = 5;
    uint256 internal constant NO_WITNESS = type(uint256).max;

    uint256 private constant HAVE_PREVOUTS = 1;
    uint256 private constant HAVE_SEQUENCES = 2;
    uint256 private constant HAVE_OUTPUTS = 4;
    uint256 private constant HAVE_AMOUNTS = 8;
    uint256 private constant HAVE_SCRIPTS = 16;

    function parse(bytes calldata raw) internal pure returns (TxView memory t) {
        uint256 len = raw.length;
        if (len > MAX_TX_SIZE) revert TxTooLarge();
        t.version = uint32(le(raw, 0, 4));
        uint256 off = 4;
        bool segwit;
        if (u8(raw, 4) == 0) {
            if (u8(raw, 5) != 1) revert BadSegwitFlag();
            segwit = true;
            off = 6;
        }
        t.bodyStart = off;

        uint256 n;
        (n, off) = compactSize(raw, off);
        if (n == 0) revert NoInputs();
        if (n > (len - off) / INPUT_SIZE) revert Truncated();
        t.nIn = n;
        t.inStart = off;
        for (uint256 i; i < n; ++i) {
            if (u8(raw, off + 36) != 0) revert ScriptSigNotEmpty(i);
            off += INPUT_SIZE;
        }

        (n, off) = compactSize(raw, off);
        if (n == 0) revert NoOutputs();
        if (n > (len - off) / 9) revert Truncated();
        uint256[] memory outOff = new uint256[](n + 1);
        for (uint256 j; j < n; ++j) {
            outOff[j] = off;
            uint256 sl;
            (sl, off) = compactSize(raw, off + 8);
            if (sl > len - off) revert Truncated();
            off += sl;
        }
        outOff[n] = off;
        t.outOff = outOff;

        uint256 nIn = t.nIn;
        uint256[] memory witOff = new uint256[](nIn);
        if (segwit) {
            bool any;
            for (uint256 i; i < nIn; ++i) {
                witOff[i] = off;
                uint256 items;
                (items, off) = compactSize(raw, off);
                if (items > len - off) revert Truncated();
                if (items != 0) any = true;
                for (uint256 k; k < items; ++k) {
                    uint256 il;
                    (il, off) = compactSize(raw, off);
                    if (il > len - off) revert Truncated();
                    off += il;
                }
            }
            if (!any) revert EmptyWitnesses();
        } else {
            for (uint256 i; i < nIn; ++i) {
                witOff[i] = NO_WITNESS;
            }
        }
        t.witOff = witOff;

        if (off + 4 > len) revert Truncated();
        if (off + 4 != len) revert TrailingBytes();
        t.locktime = uint32(le(raw, off, 4));

        // txid: the serialization without marker, flag and witnesses, which
        // is the raw tx with those three parts left out.
        bytes32 h = segwit
            ? sha256(abi.encodePacked(raw[0:4], raw[t.bodyStart:outOff[n]], raw[off:len]))
            : sha256(raw);
        t.txid = sha256(abi.encodePacked(h));
    }

    // ------------------------------------------------------------ fields

    function inputOffset(TxView memory t, uint256 i) internal pure returns (uint256) {
        return t.inStart + INPUT_SIZE * i;
    }

    function prevTxid(bytes calldata raw, TxView memory t, uint256 i) internal pure returns (bytes32 w) {
        uint256 off = inputOffset(t, i);
        assembly ("memory-safe") {
            w := calldataload(add(raw.offset, off))
        }
    }

    function prevVout(bytes calldata raw, TxView memory t, uint256 i) internal pure returns (uint32) {
        return uint32(le(raw, inputOffset(t, i) + 32, 4));
    }

    function sequence(bytes calldata raw, TxView memory t, uint256 i) internal pure returns (uint32) {
        return uint32(le(raw, inputOffset(t, i) + 37, 4));
    }

    function nOut(TxView memory t) internal pure returns (uint256) {
        return t.outOff.length - 1;
    }

    function outAmount(bytes calldata raw, TxView memory t, uint256 j) internal pure returns (uint64) {
        return uint64(le(raw, t.outOff[j], 8));
    }

    function outScript(bytes calldata raw, TxView memory t, uint256 j) internal pure returns (bytes calldata) {
        (uint256 sl, uint256 off) = compactSize(raw, t.outOff[j] + 8);
        return raw[off:off + sl];
    }

    /// Output j as it is serialized (amount, compact size, script).
    function outBytes(bytes calldata raw, TxView memory t, uint256 j) internal pure returns (bytes calldata) {
        return raw[t.outOff[j]:t.outOff[j + 1]];
    }

    /// The witness of input i. Reverts with BadWitness(i) above 5 items.
    function stack(bytes calldata raw, TxView memory t, uint256 i) internal pure returns (Stack memory s) {
        uint256 off = t.witOff[i];
        if (off == NO_WITNESS) return s;
        (s.n, off) = compactSize(raw, off);
        if (s.n > MAX_STACK) revert BadWitness(i);
        for (uint256 k; k < s.n; ++k) {
            (s.len[k], off) = compactSize(raw, off);
            s.off[k] = off;
            off += s.len[k];
        }
    }

    function item(bytes calldata raw, Stack memory s, uint256 k) internal pure returns (bytes calldata) {
        return raw[s.off[k]:s.off[k] + s.len[k]];
    }

    // ------------------------------------------------------------ hashes of the parts (BIP341 names)

    /// sha_prevouts: SHA256 of every input's outpoint.
    function shaPrevouts(bytes calldata raw, TxView memory t) internal pure returns (bytes32) {
        if (t.have & HAVE_PREVOUTS == 0) {
            t.shaPrevouts = sha256(_gather(raw, t.inStart, t.nIn, 36, 0));
            t.have |= HAVE_PREVOUTS;
        }
        return t.shaPrevouts;
    }

    /// sha_sequences: SHA256 of every input's sequence.
    function shaSequences(bytes calldata raw, TxView memory t) internal pure returns (bytes32) {
        if (t.have & HAVE_SEQUENCES == 0) {
            t.shaSequences = sha256(_gather(raw, t.inStart, t.nIn, 4, 37));
            t.have |= HAVE_SEQUENCES;
        }
        return t.shaSequences;
    }

    /// sha_outputs: SHA256 of the serialized outputs, which lie contiguous in the raw tx.
    function shaOutputs(bytes calldata raw, TxView memory t) internal pure returns (bytes32) {
        if (t.have & HAVE_OUTPUTS == 0) {
            t.shaOutputs = sha256(raw[t.outOff[0]:t.outOff[t.outOff.length - 1]]);
            t.have |= HAVE_OUTPUTS;
        }
        return t.shaOutputs;
    }

    /// sha_amounts: SHA256 of every spent amount (8 bytes LE each).
    function shaAmounts(TxView memory t, Prevout[] calldata pv) internal pure returns (bytes32) {
        if (t.have & HAVE_AMOUNTS == 0) {
            uint256 n = pv.length;
            bytes memory b = new bytes(8 * n + 32); // room for the last 32-byte store
            for (uint256 i; i < n; ++i) {
                bytes8 a = le64(pv[i].amount);
                assembly ("memory-safe") {
                    mstore(add(add(b, 32), mul(8, i)), a)
                }
            }
            assembly ("memory-safe") {
                mstore(b, mul(8, n))
            }
            t.shaAmounts = sha256(b);
            t.have |= HAVE_AMOUNTS;
        }
        return t.shaAmounts;
    }

    /// sha_scriptpubkeys: SHA256 of every spent scriptPubKey with its compact size.
    function shaScripts(TxView memory t, Prevout[] calldata pv) internal pure returns (bytes32) {
        if (t.have & HAVE_SCRIPTS == 0) {
            // one allocation (a loop of abi.encodePacked would copy the
            // buffer for every input: quadratic memory)
            uint256 total;
            for (uint256 i; i < pv.length; ++i) {
                total += compactSizeBytes(pv[i].scriptPubKey.length).length + pv[i].scriptPubKey.length;
            }
            bytes memory b = new bytes(total);
            uint256 at;
            for (uint256 i; i < pv.length; ++i) {
                bytes memory size = compactSizeBytes(pv[i].scriptPubKey.length);
                bytes calldata spk = pv[i].scriptPubKey;
                assembly ("memory-safe") {
                    let dst := add(add(b, 32), at)
                    mcopy(dst, add(size, 32), mload(size))
                    calldatacopy(add(dst, mload(size)), spk.offset, spk.length)
                }
                at += size.length + spk.length;
            }
            t.shaScripts = sha256(b);
            t.have |= HAVE_SCRIPTS;
        }
        return t.shaScripts;
    }

    /// n fields of `width` bytes, the first at raw[start + skip], one per 41-byte input.
    function _gather(bytes calldata raw, uint256 start, uint256 n, uint256 width, uint256 skip)
        private
        pure
        returns (bytes memory b)
    {
        b = new bytes(n * width);
        assembly ("memory-safe") {
            let dst := add(b, 32)
            let src := add(add(raw.offset, start), skip)
            for { let i := 0 } lt(i, n) { i := add(i, 1) } {
                calldatacopy(add(dst, mul(i, width)), add(src, mul(i, 41)), width)
            }
        }
    }

    // ------------------------------------------------------------ primitives

    function u8(bytes calldata raw, uint256 off) internal pure returns (uint256 v) {
        if (off >= raw.length) revert Truncated();
        assembly ("memory-safe") {
            v := byte(0, calldataload(add(raw.offset, off)))
        }
    }

    /// Little-endian unsigned integer of `len` (1 to 8) bytes at raw[off].
    function le(bytes calldata raw, uint256 off, uint256 len) internal pure returns (uint256 v) {
        if (off + len > raw.length) revert Truncated();
        uint64 w;
        assembly ("memory-safe") {
            // the 8 bytes at raw[off] as a big-endian uint64 (past the end of
            // calldata, calldataload reads zeros)
            w := shr(192, calldataload(add(raw.offset, off)))
        }
        // byte-reversed, the first byte is the lowest; keep the first `len`
        v = uint64(le64(w)) & ((1 << (8 * len)) - 1);
    }

    /// A canonical compact size at raw[off], and the offset after it.
    function compactSize(bytes calldata raw, uint256 off) internal pure returns (uint256 v, uint256 next) {
        uint256 first = u8(raw, off);
        if (first < 0xfd) return (first, off + 1);
        if (first == 0xfd) {
            v = le(raw, off + 1, 2);
            if (v < 0xfd) revert NonCanonicalCompactSize();
            return (v, off + 3);
        }
        if (first == 0xfe) {
            v = le(raw, off + 1, 4);
            if (v <= 0xffff) revert NonCanonicalCompactSize();
            return (v, off + 5);
        }
        v = le(raw, off + 1, 8);
        if (v <= 0xffffffff) revert NonCanonicalCompactSize();
        return (v, off + 9);
    }

    function compactSizeBytes(uint256 n) internal pure returns (bytes memory) {
        if (n < 0xfd) return abi.encodePacked(uint8(n));
        if (n <= 0xffff) return abi.encodePacked(uint8(0xfd), bytes2(le16(uint16(n))));
        if (n <= 0xffffffff) return abi.encodePacked(uint8(0xfe), le32(uint32(n)));
        return abi.encodePacked(uint8(0xff), le64(uint64(n)));
    }

    function le16(uint16 x) internal pure returns (bytes2) {
        return bytes2((x >> 8) | (x << 8));
    }

    function le32(uint32 x) internal pure returns (bytes4) {
        x = ((x & 0xff00ff00) >> 8) | ((x & 0x00ff00ff) << 8);
        x = (x >> 16) | (x << 16);
        return bytes4(x);
    }

    function le64(uint64 x) internal pure returns (bytes8) {
        x = ((x & 0xff00ff00ff00ff00) >> 8) | ((x & 0x00ff00ff00ff00ff) << 8);
        x = ((x & 0xffff0000ffff0000) >> 16) | ((x & 0x0000ffff0000ffff) << 16);
        x = (x >> 32) | (x << 32);
        return bytes8(x);
    }
}
