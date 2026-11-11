// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChainBase} from "./ChainBase.sol";
import {RealVectors} from "./generated/RealVectors.sol";
import {Synthetic} from "./generated/Synthetic.sol";
import {Case} from "./Types.sol";

/// Every real CLN tx of every vector file, replayed in order through the real
/// contract: outputs of earlier txs in the file are spent from the UTXO set the
/// contract built; the toy chain's deposits are seeded. Each tx is checked to
/// fail one block before its earliest valid height, then submitted at the
/// height it was applied at, with its event, the spent inputs, the new outputs,
/// the fee and the invariant checked.
contract RealTxsTest is ChainBase {
    function testReplayEveryVectorFile() public {
        uint256 total;
        for (uint256 f; f < RealVectors.FILE_COUNT; ++f) {
            _deploy(RealVectors.GENESIS_TIME, RealVectors.INTERVAL);
            uint256 n = RealVectors.count(f);
            for (uint256 i; i < n; ++i) {
                _submitValid(RealVectors.get(f, i));
            }
            emit log_named_uint(RealVectors.fileName(f), n);
            total += n;
        }
        assertTrue(total > 0, "no real vectors");
    }

    /// Synthetic spends (coincurve) for the paths and hash types the real
    /// txs lack, each on a fresh chain.
    function testSyntheticTxs() public {
        for (uint256 i; i < Synthetic.VALID_COUNT; ++i) {
            _deploy(Synthetic.GENESIS_TIME, Synthetic.INTERVAL);
            _submitValid(Synthetic.valid(i));
        }
    }

    /// The worst-case commitment (SPEC 4.5): 30 + 30 HTLCs and 4 more outputs,
    /// with storage, under 12M gas.
    function testWorstCaseCommitment() public {
        _deploy(Synthetic.GENESIS_TIME, Synthetic.INTERVAL);
        Case memory c = Synthetic.worstCase();
        _seed(c.inTxids[0], c.inVouts[0], c.prevouts[0]);
        _at(c.height);
        uint256 g = gasleft();
        chain.submit(c.rawTx, c.prevouts);
        g -= gasleft();
        emit log_named_uint("worst-case commitment gas, with storage", g);
        assertTrue(g < 12_000_000, "worst case over 12M gas");
        _applied(c);
    }
}
