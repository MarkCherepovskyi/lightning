// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "./TestBase.sol";
import {Harness} from "./Harness.sol";
import {Vectors, SchnorrVector, SignedSpend} from "./Vectors.sol";
import {Schnorr} from "../src/Schnorr.sol";

/// Gas of typical P2TR key-path spends. Run with -vv to see the logs, and
/// with --gas-report for the external-call totals (those include calldata
/// decoding of the structs, which the gasleft() numbers here do not).
contract GasTest is TestBase {
    Harness h = new Harness();

    function testGasSpend1In() public {
        _report("1in", Vectors.spend1In());
    }

    function testGasSpend2In() public {
        _report("2in", Vectors.spend2In());
    }

    function testGasSchnorrOnly() public {
        SchnorrVector memory v = Vectors.random(0);
        bytes32 m = bytes32(v.message);
        (uint256 rx, uint256 s) = Schnorr.split(v.sig);
        uint256 g = gasleft();
        bool ok = Schnorr.verify(v.pubkey, m, rx, s, v.ry);
        g -= gasleft();
        assertTrue(ok, "verify");
        emit log_named_uint("schnorr verify (internal call)", g);
        assertTrue(h.verify(v.pubkey, m, v.sig, v.ry), "verify via harness");
    }

    function _report(string memory name, SignedSpend memory sp) internal {
        (bool ok, uint256[] memory gas) = h.measureTx(sp.rawTx, sp.spent, sp.ry);
        assertTrue(ok, name);
        emit log_named_uint(string.concat(name, " raw tx bytes"), sp.rawTx.length);
        emit log_named_uint(string.concat(name, " parse"), gas[0]);
        emit log_named_uint(string.concat(name, " precompute"), gas[1]);
        uint256 total = gas[0] + gas[1];
        for (uint256 i; i < sp.spent.length; ++i) {
            emit log_named_uint(string.concat(name, " input ", str(i), " sighash"), gas[2 + 2 * i]);
            emit log_named_uint(string.concat(name, " input ", str(i), " schnorr"), gas[3 + 2 * i]);
            total += gas[2 + 2 * i] + gas[3 + 2 * i];
        }
        emit log_named_uint(string.concat(name, " total (excl. calldata decode)"), total);
        assertTrue(h.verifyTx(sp.rawTx, sp.spent, sp.ry), name);
    }
}
