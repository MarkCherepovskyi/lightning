// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// A transaction input as the contract needs it. `prevTxid` is in the byte
/// order it has on the wire (internal order, the reverse of the hex a block
/// explorer shows).
struct TxIn {
    bytes32 prevTxid;
    uint32 prevVout;
    uint32 sequence;
    /// Not covered by BIP341 sighashes; a native segwit spend needs it empty,
    /// which the script template checks, not the parser.
    bytes scriptSig;
}

/// A transaction output, also used for the outputs an input spends.
struct TxOut {
    uint64 amount;
    bytes scriptPubKey;
}

struct Tx {
    uint32 version;
    uint32 locktime;
    TxIn[] inputs;
    TxOut[] outputs;
}

/// @title Minimal strict parser for Bitcoin transactions (legacy or BIP144).
/// @notice Rejects non-canonical compact sizes, trailing bytes, empty input or
/// output lists, a segwit flag other than 1, and the segwit serialization when
/// every witness is empty.
library BtcTx {
    error Truncated();
    error NonCanonicalCompactSize();
    error BadSegwitFlag();
    error EmptyWitnesses();
    error NoInputs();
    error NoOutputs();
    error TrailingBytes();

    /// @return t the transaction
    /// @return witnesses witnesses[i] is input i's stack (all empty for a legacy tx)
    /// @return txid double-SHA256 of the non-witness serialization (wire order)
    function parse(bytes memory raw)
        internal
        pure
        returns (Tx memory t, bytes[][] memory witnesses, bytes32 txid)
    {
        uint256 off;
        t.version = uint32(_le(raw, 0, 4));
        off = 4;

        bool segwit;
        if (_byte(raw, off) == 0) {
            if (_byte(raw, off + 1) != 1) revert BadSegwitFlag();
            segwit = true;
            off += 2;
        }
        uint256 bodyStart = off;

        uint256 n;
        (n, off) = _compactSize(raw, off);
        if (n == 0) revert NoInputs();
        if (n > (raw.length - off) / 41) revert Truncated();
        t.inputs = new TxIn[](n);
        for (uint256 i; i < n; ++i) {
            TxIn memory input = t.inputs[i];
            input.prevTxid = _word(raw, off);
            input.prevVout = uint32(_le(raw, off + 32, 4));
            (input.scriptSig, off) = _varBytes(raw, off + 36);
            input.sequence = uint32(_le(raw, off, 4));
            off += 4;
        }

        (n, off) = _compactSize(raw, off);
        if (n == 0) revert NoOutputs();
        if (n > (raw.length - off) / 9) revert Truncated();
        t.outputs = new TxOut[](n);
        for (uint256 i; i < n; ++i) {
            TxOut memory output = t.outputs[i];
            output.amount = uint64(_le(raw, off, 8));
            (output.scriptPubKey, off) = _varBytes(raw, off + 8);
        }
        uint256 bodyEnd = off;

        witnesses = new bytes[][](t.inputs.length);
        if (segwit) {
            bool any;
            for (uint256 i; i < t.inputs.length; ++i) {
                (n, off) = _compactSize(raw, off);
                if (n > raw.length - off) revert Truncated();
                bytes[] memory stack = new bytes[](n);
                for (uint256 j; j < n; ++j) {
                    (stack[j], off) = _varBytes(raw, off);
                }
                witnesses[i] = stack;
                if (n != 0) any = true;
            }
            if (!any) revert EmptyWitnesses();
        }

        t.locktime = uint32(_le(raw, off, 4));
        off += 4;
        if (off != raw.length) revert TrailingBytes();

        bytes memory stripped = raw;
        if (segwit) {
            stripped = abi.encodePacked(_slice(raw, 0, 4), _slice(raw, bodyStart, bodyEnd - bodyStart), _slice(raw, off - 4, 4));
        }
        txid = sha256(abi.encodePacked(sha256(stripped)));
    }

    function _byte(bytes memory b, uint256 off) private pure returns (uint8 v) {
        if (off >= b.length) revert Truncated();
        v = uint8(b[off]);
    }

    function _word(bytes memory b, uint256 off) private pure returns (bytes32 w) {
        if (off + 32 > b.length) revert Truncated();
        assembly ("memory-safe") {
            w := mload(add(add(b, 32), off))
        }
    }

    /// Little-endian unsigned integer of `len` (<= 8) bytes.
    function _le(bytes memory b, uint256 off, uint256 len) private pure returns (uint256 v) {
        if (off + len > b.length) revert Truncated();
        assembly ("memory-safe") {
            let w := mload(add(add(b, 32), off))
            for { let i := 0 } lt(i, len) { i := add(i, 1) } {
                v := or(v, shl(mul(8, i), byte(i, w)))
            }
        }
    }

    function _compactSize(bytes memory b, uint256 off) private pure returns (uint256 v, uint256 next) {
        uint8 first = _byte(b, off);
        if (first < 0xfd) return (first, off + 1);
        if (first == 0xfd) {
            v = _le(b, off + 1, 2);
            if (v < 0xfd) revert NonCanonicalCompactSize();
            return (v, off + 3);
        }
        if (first == 0xfe) {
            v = _le(b, off + 1, 4);
            if (v <= 0xffff) revert NonCanonicalCompactSize();
            return (v, off + 5);
        }
        v = _le(b, off + 1, 8);
        if (v <= 0xffffffff) revert NonCanonicalCompactSize();
        return (v, off + 9);
    }

    function _varBytes(bytes memory b, uint256 off) private pure returns (bytes memory out, uint256 next) {
        uint256 len;
        (len, off) = _compactSize(b, off);
        if (len > b.length - off) revert Truncated();
        out = _slice(b, off, len);
        next = off + len;
    }

    function _slice(bytes memory b, uint256 off, uint256 len) private pure returns (bytes memory out) {
        if (off + len > b.length) revert Truncated();
        out = new bytes(len);
        assembly ("memory-safe") {
            mcopy(add(out, 32), add(add(b, 32), off), len)
        }
    }
}
