// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChainBase} from "./ChainBase.sol";
import {Synthetic} from "./generated/Synthetic.sol";
import {Bad} from "./Types.sol";

/// Every tampered or invalid tx fails with the expected error. Each is run
/// against the UTXO set its case seeds, which is cleared afterwards, and the
/// cases are split in four for the per-test gas limit.
contract TamperedTest is ChainBase {
    function testTampered0() public {
        _range(0);
    }

    function testTampered1() public {
        _range(1);
    }

    function testTampered2() public {
        _range(2);
    }

    function testTampered3() public {
        _range(3);
    }

    function _range(uint256 part) internal {
        _deploy(Synthetic.GENESIS_TIME, Synthetic.INTERVAL);
        uint256 n = Synthetic.BAD_COUNT;
        for (uint256 i = n * part / 4; i < n * (part + 1) / 4; ++i) {
            Bad memory b = Synthetic.bad(i);
            for (uint256 k; k < b.seedPv.length; ++k) {
                _seed(b.seedTxids[k], b.seedVouts[k], b.seedPv[k]);
            }
            _at(b.height);
            (bool ok, bytes memory ret) = address(chain).call(abi.encodeCall(chain.submit, (b.rawTx, b.prevouts)));
            assertTrue(!ok, b.name);
            assertEq(ret, b.err, b.name);
            for (uint256 k; k < b.seedPv.length; ++k) {
                chain.unseed(b.seedTxids[k], b.seedVouts[k]);
            }
        }
    }
}
