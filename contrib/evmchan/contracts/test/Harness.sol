// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {UtxoChain} from "../src/UtxoChain.sol";
import {Prevout, TxView, BtcTx} from "../src/BtcTx.sol";
import {Validation} from "../src/Validation.sol";
import {TaprootSighash} from "../src/TaprootSighash.sol";

/// Test-only: UtxoChain plus a way to put outputs in the UTXO set directly
/// (the real vectors spend outputs of the toy chain's deposits, which have
/// other txids than this contract's deposits), and validation without
/// storage for the gas measurements. Never deployed outside the tests.
contract UtxoChainHarness is UtxoChain {
    constructor(uint256 interval) UtxoChain(interval) {}

    /// Seeds an output, paid for with msg.value so the balance invariant holds.
    function seed(bytes32 txid, uint32 vout, Prevout calldata p) external payable {
        require(msg.value == uint256(p.amount) * SAT, "seed value");
        utxo[utxoKey(txid, vout)] = _commitment(p.amount, p.scriptPubKey, p.height);
    }

    function setDepositNonce(uint256 n) external {
        depositNonce = n;
    }

    function unseed(bytes32 txid, uint32 vout) external {
        delete utxo[utxoKey(txid, vout)];
    }

    /// SPEC 4.3 without the UTXO set (no storage read or written).
    function validateOnly(bytes calldata rawTx, Prevout[] calldata prevouts)
        external
        view
        returns (bytes32 txid, uint256 fee)
    {
        uint32 h = _applyHeight();
        TxView memory t;
        (t, fee) = Validation.validate(rawTx, prevouts, h, _mtp(h));
        txid = t.txid;
    }

    function parseOnly(bytes calldata rawTx) external pure returns (bytes32) {
        return BtcTx.parse(rawTx).txid;
    }
}

/// The BIP341 sighash on calldata, for the official vectors.
contract SighashHarness {
    function sigMsg(bytes calldata rawTx, Prevout[] calldata pv, uint256 index, uint8 hashType)
        external
        pure
        returns (bytes memory)
    {
        TxView memory t = BtcTx.parse(rawTx);
        return TaprootSighash.sigMsg(rawTx, t, pv, index, hashType);
    }

    function hashes(bytes calldata rawTx, Prevout[] calldata pv) external pure returns (bytes32[5] memory h) {
        TxView memory t = BtcTx.parse(rawTx);
        h[0] = BtcTx.shaAmounts(t, pv);
        h[1] = BtcTx.shaOutputs(rawTx, t);
        h[2] = BtcTx.shaPrevouts(rawTx, t);
        h[3] = BtcTx.shaScripts(t, pv);
        h[4] = BtcTx.shaSequences(rawTx, t);
    }
}
