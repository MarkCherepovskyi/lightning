// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChainBase} from "./ChainBase.sol";
import {RealVectors} from "./generated/RealVectors.sol";
import {Synthetic} from "./generated/Synthetic.sol";
import {Case} from "./Types.sol";

/// Gas per tx, logged as "gas: <kind> | <name> | exec <n> | total <n> | bytes <n>"
/// (run with -vv). "exec" is validation only (SPEC 4.3, no storage: the
/// harness's validateOnly); "total" is the whole submit, with the UTXO reads,
/// deletes and writes, the fee credit and the event. Both are the gas of the
/// external call (no 21,000 base or calldata cost) and before storage refunds.
contract GasTest is ChainBase {
    function testGasRealTxs() public {
        for (uint256 f; f < RealVectors.FILE_COUNT; ++f) {
            _deploy(RealVectors.GENESIS_TIME, RealVectors.INTERVAL);
            for (uint256 i; i < RealVectors.count(f); ++i) {
                _measure(RealVectors.get(f, i));
            }
        }
    }

    function testGasSyntheticTxs() public {
        for (uint256 i; i < Synthetic.VALID_COUNT; ++i) {
            _deploy(Synthetic.GENESIS_TIME, Synthetic.INTERVAL);
            _measure(Synthetic.valid(i));
        }
        _deploy(Synthetic.GENESIS_TIME, Synthetic.INTERVAL);
        _measure(Synthetic.worstCase());
    }

    function testGasDeposit() public {
        _deploy(Synthetic.GENESIS_TIME, Synthetic.INTERVAL);
        _at(1);
        chain.deposit{value: SAT}(hex"5120bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
        uint256 g = gasleft();
        chain.deposit{value: 1e18}(hex"5120aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        g -= gasleft();
        emit log_named_string("gas", string.concat("deposit | P2TR, second deposit | total ", str(g)));
    }

    function _measure(Case memory c) internal {
        for (uint256 i; i < c.prevouts.length; ++i) {
            if (c.seed[i]) _seed(c.inTxids[i], c.inVouts[i], c.prevouts[i]);
        }
        _at(c.height);
        uint256 g = gasleft();
        chain.validateOnly(c.rawTx, c.prevouts);
        uint256 exec = g - gasleft();
        g = gasleft();
        chain.submit(c.rawTx, c.prevouts);
        uint256 total = g - gasleft();
        emit log_named_string(
            "gas",
            string.concat(
                c.kind, " | ", c.name, " | exec ", str(exec), " | total ", str(total), " | bytes ", str(c.rawTx.length)
            )
        );
        _applied(c);
    }
}
