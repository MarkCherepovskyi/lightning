// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {Vectors, Case} from "./Vectors.sol";
import {SegwitValidator} from "../src/SegwitValidator.sol";
import {BtcTx} from "s2/BtcTx.sol";

/// Gas of validateTx per vector. Run with -vv to see the logs, and with
/// --gas-report for the external call statistics. "exec" is the gas of the
/// external call measured with gasleft (calldata decoding included; the 21000
/// base and calldata intrinsic gas excluded). "calldata" is the intrinsic cost
/// of the call's calldata (4 per zero byte, 16 per other byte, pre-EIP-7623).
contract GasTest is TestBase {
    SegwitValidator v = new SegwitValidator(Vectors.GENESIS_TIME, Vectors.INTERVAL);

    function testGasReal() public {
        for (uint256 i; i < Vectors.REAL_COUNT; ++i) {
            _report(Vectors.real(i));
        }
    }

    function testGasSynthetic() public {
        for (uint256 i; i < Vectors.SYNTHETIC_COUNT; ++i) {
            _report(Vectors.synthetic(i));
        }
    }

    /// The share of BtcTx.parse (strict parsing and the txid) in the above.
    function testGasParseOnly() public {
        for (uint256 i; i < Vectors.REAL_COUNT; ++i) {
            Case memory c = Vectors.real(i);
            uint256 g = gasleft();
            BtcTx.parse(c.rawTx);
            g -= gasleft();
            emit log_named_uint(string.concat(c.name, " parse gas"), g);
        }
    }

    function _report(Case memory c) internal {
        uint256 g = gasleft();
        v.validateTx(c.rawTx, c.prevouts, c.hints, c.height);
        g -= gasleft();
        bytes memory data = abi.encodeCall(v.validateTx, (c.rawTx, c.prevouts, c.hints, c.height));
        uint256 cd;
        for (uint256 i; i < data.length; ++i) {
            cd += data[i] == 0 ? 4 : 16;
        }
        emit log_named_string("tx", string.concat(c.name, " [", c.kind, "]"));
        emit log_named_uint("  raw bytes", c.rawTx.length);
        emit log_named_uint("  exec gas", g);
        emit log_named_uint("  calldata gas", cd);
    }
}
