// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ChainBase} from "./ChainBase.sol";
import {UtxoChainHarness} from "./Harness.sol";
import {Ref, RefDeposit} from "./Types.sol";

/// Differential test against the independent Python reference
/// (contrib/evmchan/reference): the contract accepts exactly the cases the
/// reference says are valid. Cases whose origin is marked ambiguous depend on
/// a reading of the SPEC; for them the contract may only be stricter (reject a
/// case the reference accepts), never more lenient.
///
/// contrib/evmchan/gen_vectors.py writes the cases to test/generated/reference-*.hex, in
/// 16 parts; without them (no reference directory) every test here skips.
contract ReferenceTest is ChainBase {
    string internal constant DIR = "test/generated/";

    function testReference00() public { _part(0); }
    function testReference01() public { _part(1); }
    function testReference02() public { _part(2); }
    function testReference03() public { _part(3); }
    function testReference04() public { _part(4); }
    function testReference05() public { _part(5); }
    function testReference06() public { _part(6); }
    function testReference07() public { _part(7); }
    function testReference08() public { _part(8); }
    function testReference09() public { _part(9); }
    function testReference10() public { _part(10); }
    function testReference11() public { _part(11); }
    function testReference12() public { _part(12); }
    function testReference13() public { _part(13); }
    function testReference14() public { _part(14); }
    function testReference15() public { _part(15); }

    function _part(uint256 k) internal {
        string memory path = string.concat(DIR, "reference-txs-", k < 10 ? "0" : "", vm.toString(k), ".hex");
        if (!vm.exists(path)) {
            vm.skip(true);
            return;
        }
        uint256 n = vm.parseUint(vm.readLine(path));
        uint256 genesis;
        uint256 interval;
        uint256 accepted;
        uint256 stricter;
        for (uint256 i; i < n; ++i) {
            Ref memory r = abi.decode(vm.parseBytes(vm.readLine(path)), (Ref));
            if (r.genesis != genesis || r.interval != interval) {
                _deploy(r.genesis, r.interval);
                (genesis, interval) = (r.genesis, r.interval);
            }
            for (uint256 j; j < r.seedPv.length; ++j) {
                _seed(r.seedTxids[j], r.seedVouts[j], r.seedPv[j]);
            }
            _at(r.height);
            (bool ok, bytes memory ret) = address(chain).call(abi.encodeCall(chain.submit, (r.rawTx, r.prevouts)));
            if (ok != r.valid) {
                if (r.ambiguous && !ok) {
                    ++stricter;
                } else {
                    emit log_named_string("reference error", r.err);
                    emit log_named_uint("contract error selector", ret.length >= 4 ? uint32(bytes4(ret)) : 0);
                    revert(string.concat("differs from the reference: ", r.name, ok ? " (accepted)" : " (rejected)"));
                }
            }
            if (ok) ++accepted;
            for (uint256 j; j < r.seedPv.length; ++j) {
                chain.unseed(r.seedTxids[j], r.seedVouts[j]);
            }
        }
        vm.closeFile(path);
        emit log_named_uint("cases", n);
        emit log_named_uint("accepted", accepted);
        emit log_named_uint("ambiguous, rejected by the contract only", stricter);
    }

    /// The reference's deposit cases: the same verdict, and for valid ones the
    /// same txid (it builds the deposit tx itself).
    function testReferenceDeposits() public {
        string memory path = string.concat(DIR, "reference-deposits.hex");
        if (!vm.exists(path)) {
            vm.skip(true);
            return;
        }
        uint256 n = vm.parseUint(vm.readLine(path));
        for (uint256 i; i < n; ++i) {
            RefDeposit memory d = abi.decode(vm.parseBytes(vm.readLine(path)), (RefDeposit));
            _deploy(1_700_000_000, 600);
            vm.chainId(d.chainId);
            vm.etch(d.target, address(chain).code);
            UtxoChainHarness c = UtxoChainHarness(d.target);
            c.setDepositNonce(d.nonce);
            vm.warp(1_700_000_000 + uint256(d.height) * 600);
            (bool ok, bytes memory ret) = address(c).call{value: d.value}(abi.encodeCall(c.deposit, (d.script)));
            assertTrue(ok == d.valid, d.name);
            if (ok) assertEq(abi.decode(ret, (bytes32)), d.txid, d.name);
        }
        vm.closeFile(path);
        emit log_named_uint("deposit cases", n);
    }
}
