// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {UtxoChainBase} from "./UtxoChainBase.sol";
import {WithdrawFailed} from "./Errors.sol";

/// @title UtxoChainBase holding ETH (SPEC 4): 1 sat = 10^10 wei.
/// `deposit` takes msg.value; `withdraw` pays with a plain call. The ABI and
/// the behaviour are those of M1's UtxoChain.
contract UtxoChain is UtxoChainBase {
    uint256 internal constant SAT = 1e10; // wei per sat

    /// @param interval seconds per virtual block (2 on anvil, 60 on Base Sepolia)
    constructor(uint256 interval) UtxoChainBase(interval) {}

    function _unit() internal pure override returns (uint256) {
        return SAT;
    }

    /// @notice Creates one output of msg.value / 10^10 sats paying
    /// `scriptPubKey`, as the output 0 of a synthetic deposit tx (SPEC 3.3).
    function deposit(bytes calldata scriptPubKey) external payable returns (bytes32 txid) {
        return _deposit(msg.value, scriptPubKey);
    }

    /// The value came with the call, and `_deposit` checked it.
    function _collect(uint256) internal pure override {}

    /// A recipient that reverts gets WithdrawFailed and keeps its balance.
    function _pay(address to, uint256 amount) internal override {
        (bool ok,) = to.call{value: amount}("");
        if (!ok) revert WithdrawFailed();
    }
}
