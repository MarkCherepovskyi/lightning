// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {UtxoChainHarness, UtxoChainTokenHarness} from "./Harness.sol";
import {UtxoChainBase} from "../src/UtxoChainBase.sol";
import {MockERC20} from "./Tokens.sol";
import {Prevout} from "../src/BtcTx.sol";
import {Case} from "./Types.sol";

/// A harness chain plus an independent ledger of what it should hold, and the
/// SPEC 4.1 invariant checked against the contract's own state. In ETH mode
/// (`_deploy`) the chain is `chain`; in token mode (`_deployToken`) it is
/// `tchain`, and amounts are in token units. The helpers use `_c()`, either.
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

    UtxoChainHarness internal chain; // ETH mode
    UtxoChainTokenHarness internal tchain; // token mode
    MockERC20 internal token;
    uint256 internal unit; // wei or token units per sat
    Out[] internal outs; // every output seeded, deposited or created, spent or not
    address[] internal payees; // every address that may have a `withdrawable`
    mapping(address => mapping(address => uint256)) internal owed; // chain => payee => wei or units
    mapping(address => mapping(address => bool)) internal known;
    uint256 internal burned; // wei or units of OP_RETURN outputs

    receive() external payable {}

    function _deploy(uint256 genesis, uint256 interval) internal {
        vm.deal(address(this), 1e30);
        vm.warp(genesis);
        chain = new UtxoChainHarness(interval);
        tchain = UtxoChainTokenHarness(address(0));
        token = MockERC20(address(0));
        unit = SAT;
        _reset();
    }

    function _deployToken(uint256 genesis, uint256 interval, MockERC20 t, uint256 unitsPerSat) internal {
        vm.warp(genesis);
        tchain = new UtxoChainTokenHarness(interval, address(t), unitsPerSat);
        chain = UtxoChainHarness(payable(address(0)));
        token = t;
        unit = unitsPerSat;
        _reset();
    }

    function _reset() private {
        delete outs;
        delete payees;
        burned = 0;
        _payee(address(this));
    }

    function _tokenMode() internal view returns (bool) {
        return address(tchain) != address(0);
    }

    /// The chain under test, in either mode.
    function _c() internal view returns (UtxoChainBase) {
        return _tokenMode() ? UtxoChainBase(tchain) : UtxoChainBase(chain);
    }

    /// The asset the chain holds: its ETH, or its token balance.
    function _held() internal view returns (uint256) {
        return _tokenMode() ? token.balanceOf(address(tchain)) : address(chain).balance;
    }

    function _at(uint256 h) internal {
        vm.warp(_c().GENESIS_TIME() + h * _c().INTERVAL());
    }

    function _payee(address a) internal {
        if (!known[address(_c())][a]) {
            known[address(_c())][a] = true;
            payees.push(a);
        }
    }

    function _seed(bytes32 txid, uint32 vout, Prevout memory p) internal {
        if (_tokenMode()) {
            token.mint(address(tchain), uint256(p.amount) * unit);
            tchain.seed(txid, vout, p);
        } else {
            chain.seed{value: uint256(p.amount) * SAT}(txid, vout, p);
        }
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
            _c().submit(c.rawTx, c.prevouts);
        }
        _at(c.height);
        vm.expectEmit(true, true, false, true, address(_c()));
        emit TxApplied(c.height, c.txid, c.rawTx);
        bytes32 txid = _c().submit(c.rawTx, c.prevouts);
        assertEq(txid, c.txid, c.name);
        _applied(c);
    }

    /// The ledger after `c` was applied, checked against the contract.
    function _applied(Case memory c) internal {
        for (uint256 i; i < c.prevouts.length; ++i) {
            Prevout memory p = c.prevouts[i];
            assertTrue(
                !_c().isUnspent(c.inTxids[i], c.inVouts[i], p.amount, p.scriptPubKey, p.height),
                string.concat(c.name, ": input still unspent")
            );
        }
        owed[address(_c())][address(this)] += uint256(c.fee) * unit;
        for (uint256 j; j < c.outAmounts.length; ++j) {
            bytes memory spk = c.outScripts[j];
            uint64 a = c.outAmounts[j];
            if (_isExit(spk)) {
                address to = _exitAddress(spk);
                _payee(to);
                owed[address(_c())][to] += uint256(a) * unit;
                assertTrue(!_c().isUnspent(c.txid, uint32(j), a, spk, c.height), "exit stored");
            } else if (spk.length != 0 && spk[0] == 0x6a) {
                burned += uint256(a) * unit;
                assertTrue(!_c().isUnspent(c.txid, uint32(j), a, spk, c.height), "OP_RETURN stored");
            } else {
                assertTrue(_c().isUnspent(c.txid, uint32(j), a, spk, c.height), string.concat(c.name, ": output"));
                outs.push(Out(c.txid, uint32(j), a, spk, c.height));
            }
        }
        _checkInvariant();
    }

    /// SPEC 4.1: balance >= sum(UTXO amounts) * unit + sum(withdrawable), and
    /// with the ledger exactly: balance == that + the OP_RETURN amounts burned.
    /// The balance is the chain's ETH, or its token balance in token mode.
    function _checkInvariant() internal view {
        uint256 live;
        for (uint256 k; k < outs.length; ++k) {
            Out storage o = outs[k];
            if (_c().isUnspent(o.txid, o.vout, o.amount, o.spk, o.height)) live += o.amount;
        }
        uint256 owedSum;
        for (uint256 k; k < payees.length; ++k) {
            uint256 w = _c().withdrawable(payees[k]);
            assertEq(w, owed[address(_c())][payees[k]], "withdrawable");
            owedSum += w;
        }
        uint256 bal = _held();
        assertTrue(bal >= live * unit + owedSum, "SPEC 4.1 invariant");
        assertEq(bal, live * unit + owedSum + burned, "balance == UTXOs + withdrawable + burned");
    }
}
