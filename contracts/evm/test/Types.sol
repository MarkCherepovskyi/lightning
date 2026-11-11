// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Prevout} from "../src/BtcTx.sol";

/// A valid tx, with what the tests check after submitting it.
struct Case {
    string name;
    string kind;
    bytes rawTx;
    Prevout[] prevouts;
    bytes32[] inTxids; // the inputs' outpoints (txid in internal byte order)
    uint32[] inVouts;
    bool[] seed; // whether the test puts the prevout in the UTXO set (else an earlier case created it)
    uint32 height; // the height it is submitted at
    uint32 minHeight; // the lowest height its lock time and BIP68 locks allow
    bytes earlyError; // the revert at minHeight - 1 (empty when minHeight < 2)
    bytes32 txid;
    uint64 fee;
    uint64[] outAmounts;
    bytes[] outScripts;
}

/// A tx the contract must reject with `err` at `height`, after seeding the
/// UTXO set with `seed*`.
struct Bad {
    string name;
    bytes rawTx;
    Prevout[] prevouts;
    Prevout[] seedPv;
    bytes32[] seedTxids;
    uint32[] seedVouts;
    uint32 height;
    bytes err;
}

/// A differential case from the independent reference (read from
/// test/generated/reference-txs.hex).
struct Ref {
    string name;
    bytes rawTx;
    Prevout[] prevouts;
    Prevout[] seedPv;
    bytes32[] seedTxids;
    uint32[] seedVouts;
    uint32 height;
    bool valid;
    uint256 genesis;
    uint256 interval;
    string err; // the reference's error name
    bool ambiguous; // its origin depends on a reading of the SPEC (reference/README.md)
}

/// A deposit case from the reference (test/generated/reference-deposits.hex).
struct RefDeposit {
    string name;
    uint256 chainId;
    address target;
    uint256 nonce;
    uint256 value;
    bytes script;
    uint32 height;
    bool valid;
    bytes32 txid; // internal byte order
}

library Slice {
    function cut(bytes memory b, uint256 off, uint256 len) internal pure returns (bytes memory out) {
        require(off + len <= b.length, "slice");
        out = new bytes(len);
        assembly ("memory-safe") {
            mcopy(add(out, 32), add(add(b, 32), off), len)
        }
    }
}
