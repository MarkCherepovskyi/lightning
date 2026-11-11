// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChainBase} from "./ChainBase.sol";
import {RealVectors} from "./generated/RealVectors.sol";
import {Case} from "./Types.sol";

/// SPEC 4.1/4.4: txCount and logHash follow every TxApplied, deposits and
/// submits alike, in order. The expected values are built here with
/// bytes.concat from the cases' own heights and txids (the contract uses
/// abi.encodePacked); the Python index checks them independently on anvil
/// (tests/test_index_anvil.py).
contract LogHashTest is ChainBase {
    uint64 internal count;
    bytes32 internal hash;

    function _expect(uint32 h, bytes32 txid) internal {
        count += 1;
        hash = keccak256(bytes.concat(hash, bytes4(h), txid));
        assertEq(uint256(chain.txCount()), uint256(count), "txCount");
        assertEq(chain.logHash(), hash, "logHash");
    }

    function testStartsAtZero() public {
        _deploy(RealVectors.GENESIS_TIME, RealVectors.INTERVAL);
        assertEq(uint256(chain.txCount()), 0, "txCount");
        assertEq(chain.logHash(), bytes32(0), "logHash");
    }

    /// Every real vector file replayed, with a deposit before each tx, and
    /// rejected submits (one block early) leaving both unchanged.
    function testReplayRealVectorsWithDeposits() public {
        uint256 total;
        for (uint256 f; f < RealVectors.FILE_COUNT; ++f) {
            _deploy(RealVectors.GENESIS_TIME, RealVectors.INTERVAL);
            count = 0;
            hash = bytes32(0);
            uint256 n = RealVectors.count(f);
            for (uint256 i; i < n; ++i) {
                Case memory c = RealVectors.get(f, i);
                _at(c.height);
                bytes memory spk = abi.encodePacked(hex"0014", bytes20(uint160(i)));
                bytes32 dtxid = chain.deposit{value: SAT * (i + 1)}(spk);
                outs.push(Out(dtxid, 0, uint64(i + 1), spk, c.height)); // for the invariant check
                _expect(c.height, dtxid);
                _submitValid(c); // checks the early revert too, then submits at c.height
                _expect(c.height, c.txid);
            }
            total += n;
        }
        assertTrue(total > 0, "no real vectors");
    }
}
