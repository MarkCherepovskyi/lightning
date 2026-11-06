// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {UtxoChainHarness} from "./Harness.sol";
import {Prevout} from "../src/BtcTx.sol";
import {Case} from "./Types.sol";

/// A harness chain plus an independent ledger of what it should hold, and the
/// SPEC 4.1 invariant checked against the contract's own state.
abstract contract ChainBase is TestBase {
    event TxApplied(uint32 indexed height, bytes32 indexed txid, bytes rawTx);

    uint256 internal constant SAT = 1e10;

    struct Out {
        bytes32 txid;
        uint32 vout;
        uint64 amount;
        bytes spk;
        uint32 height;
    }

    UtxoChainHarness internal chain;
    Out[] internal outs; // every output seeded, deposited or created, spent or not
    address[] internal payees; // every address that may have a `withdrawable`
    mapping(address => mapping(address => uint256)) internal owed; // chain => payee => wei
    mapping(address => mapping(address => bool)) internal known;
    uint256 internal burned; // wei of OP_RETURN outputs

    receive() external payable {}

    function _deploy(uint256 genesis, uint256 interval) internal {
        vm.deal(address(this), 1e30);
        vm.warp(genesis);
        chain = new UtxoChainHarness(interval);
        delete outs;
        delete payees;
        burned = 0;
        _payee(address(this));
    }

    function _at(uint256 h) internal {
        vm.warp(chain.GENESIS_TIME() + h * chain.INTERVAL());
    }

    function _payee(address a) internal {
        if (!known[address(chain)][a]) {
            known[address(chain)][a] = true;
            payees.push(a);
        }
    }

    function _seed(bytes32 txid, uint32 vout, Prevout memory p) internal {
        chain.seed{value: uint256(p.amount) * SAT}(txid, vout, p);
        outs.push(Out(txid, vout, p.amount, p.scriptPubKey, p.height));
    }

    function _isExit(bytes memory spk) internal pure returns (bool) {
        return spk.length == 22 && spk[0] == 0x60 && spk[1] == 0x14;
    }

    function _exitAddress(bytes memory spk) internal pure returns (address a) {
        assembly ("memory-safe") {
            a := shr(96, mload(add(spk, 34)))
        }
    }

    /// Submits a valid case, after checking it fails one block before its
    /// earliest valid height, and checks the event and the state after it.
    function _submitValid(Case memory c) internal {
        for (uint256 i; i < c.prevouts.length; ++i) {
            if (c.seed[i]) _seed(c.inTxids[i], c.inVouts[i], c.prevouts[i]);
        }
        if (c.earlyError.length != 0) {
            _at(c.minHeight - 1);
            vm.expectRevert(c.earlyError);
            chain.submit(c.rawTx, c.prevouts);
        }
        _at(c.height);
        vm.expectEmit(true, true, false, true, address(chain));
        emit TxApplied(c.height, c.txid, c.rawTx);
        bytes32 txid = chain.submit(c.rawTx, c.prevouts);
        assertEq(txid, c.txid, c.name);
        _applied(c);
    }

    /// The ledger after `c` was applied, checked against the contract.
    function _applied(Case memory c) internal {
        for (uint256 i; i < c.prevouts.length; ++i) {
            Prevout memory p = c.prevouts[i];
            assertTrue(
                !chain.isUnspent(c.inTxids[i], c.inVouts[i], p.amount, p.scriptPubKey, p.height),
                string.concat(c.name, ": input still unspent")
            );
        }
        owed[address(chain)][address(this)] += uint256(c.fee) * SAT;
        for (uint256 j; j < c.outAmounts.length; ++j) {
            bytes memory spk = c.outScripts[j];
            uint64 a = c.outAmounts[j];
            if (_isExit(spk)) {
                address to = _exitAddress(spk);
                _payee(to);
                owed[address(chain)][to] += uint256(a) * SAT;
                assertTrue(!chain.isUnspent(c.txid, uint32(j), a, spk, c.height), "exit stored");
            } else if (spk.length != 0 && spk[0] == 0x6a) {
                burned += uint256(a) * SAT;
                assertTrue(!chain.isUnspent(c.txid, uint32(j), a, spk, c.height), "OP_RETURN stored");
            } else {
                assertTrue(chain.isUnspent(c.txid, uint32(j), a, spk, c.height), string.concat(c.name, ": output"));
                outs.push(Out(c.txid, uint32(j), a, spk, c.height));
            }
        }
        _checkInvariant();
    }

    /// SPEC 4.1: balance >= sum(UTXO amounts) * 10^10 + sum(withdrawable), and
    /// with the ledger exactly: balance == that + the OP_RETURN amounts burned.
    function _checkInvariant() internal view {
        uint256 live;
        for (uint256 k; k < outs.length; ++k) {
            Out storage o = outs[k];
            if (chain.isUnspent(o.txid, o.vout, o.amount, o.spk, o.height)) live += o.amount;
        }
        uint256 owedSum;
        for (uint256 k; k < payees.length; ++k) {
            uint256 w = chain.withdrawable(payees[k]);
            assertEq(w, owed[address(chain)][payees[k]], "withdrawable");
            owedSum += w;
        }
        uint256 bal = address(chain).balance;
        assertTrue(bal >= live * SAT + owedSum, "SPEC 4.1 invariant");
        assertEq(bal, live * SAT + owedSum + burned, "balance == UTXOs + withdrawable + burned");
    }
}
