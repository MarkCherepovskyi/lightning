// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {TestBase} from "../TestBase.sol";
import {UtxoChainHarness} from "../Harness.sol";
import {Prevout} from "../../src/BtcTx.sol";
import {UnknownWitnessScript} from "../../src/Errors.sol";

/// Review regressions (template matching, SPEC 4.3.7 draft 3). Txs built by gen_review.py (pyref + coincurve);
/// each spends a P2WSH prevout at height 1, submitted at height 5.
contract TemplateDivergenceTest is TestBase {
    uint256 constant G = 1_700_000_000;
    uint256 constant I = 2;
    bytes32 constant PREV = 0x1111111111111111111111111111111111111111111111111111111111111111;

    bytes internal constant toLocalBadDelayedKey_RAW = hex"020000000001011111111111111111111111111111111111111111111111111111111111111111000000000000000000012823000000000000160014222222222222222222222222222222222222222203483045022100f329ce15702f643b0b2d234c9042afb29c909b7e636f9f69de5fb5d950cf8ec9022010c656cbeeb0f91d5011691a84bbc1cd61835f40d1dc0aff6a55e8e3ee7470580101014d6321031b84c5567b126440995d3ed5aaba0565d71e1834604819ff9c17f5e9d5dd078f67029000b2752102000000000000000000000000000000000000000000000000000000000000000568ac00000000";
    bytes internal constant toLocalBadDelayedKey_SPK = hex"0020ef0070522615d4eaa7f3c0e823c7fa44b4156daf778ad23a1a5ae8781be54ba7";
    bytes internal constant toLocalDelay2pow32_RAW = hex"020000000001011111111111111111111111111111111111111111111111111111111111111111000000000000000000012823000000000000160014222222222222222222222222222222222222222203483045022100b24517b2cc5ee9e003a0dceaa4db92a561a8996147704a6aab2dd86860e40d6a0220294199cb78cbf4c27cedd5f47a6581ad6a4bd862624e864beaff542e50dc1d83010101506321031b84c5567b126440995d3ed5aaba0565d71e1834604819ff9c17f5e9d5dd078f67050000000001b27521024d4b6cd1361032ca9bd2aeb9d900aa4d45d9ead80ac9423374c451a7254d076668ac00000000";
    bytes internal constant toLocalDelay2pow32_SPK = hex"00200c2a2c99c25fc50e10c872dd305f8952986c119be612432804d5bf9ed4e429cf";

    UtxoChainHarness chain;

    function _setup(bytes memory spk) internal returns (Prevout[] memory pv) {
        vm.warp(G);
        chain = new UtxoChainHarness(I);
        pv = new Prevout[](1);
        pv[0] = Prevout(10000, spk, 1);
        vm.deal(address(this), 1 ether);
        chain.seed{value: 10000 * 1e10}(PREV, 0, pv[0]);
        vm.warp(G + 5 * I);
    }

    /// SPEC 4.3.7 (draft 3, "Keys"): keys are checked when a signature uses
    /// them, plus the anchor's on its <> path (Bitcoin's behaviour). So the
    /// revocation spend of a to_local whose delayed key is off the curve is
    /// valid: that key is never used on that path.
    function testAcceptsTemplateWithOffCurveKeyOnUnusedPath() public {
        Prevout[] memory pv = _setup(toLocalBadDelayedKey_SPK);
        chain.submit(toLocalBadDelayedKey_RAW, pv);
    }

    /// Script numbers are u32 (bitcoin/script.c add_number): a to_local with
    /// delay 2^32 is not a template, so even its revocation spend fails.
    function testRejectsToLocalWithDelayAboveU32() public {
        Prevout[] memory pv = _setup(toLocalDelay2pow32_SPK);
        vm.expectRevert(abi.encodeWithSelector(UnknownWitnessScript.selector, 0));
        chain.submit(toLocalDelay2pow32_RAW, pv);
    }
}
