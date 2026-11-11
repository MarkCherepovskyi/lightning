// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28; // transient storage

import {UtxoChainBase} from "./UtxoChainBase.sol";
import {BadToken, BadUnitsPerSat, TokenTransferFailed, TokenAmountMismatch, Reentrancy} from "./Errors.sol";

interface IERC20 {
    function balanceOf(address who) external view returns (uint256);
    function decimals() external view returns (uint8);
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @title UtxoChainBase holding one ERC-20 token (SPEC 4.6): 1 sat =
/// UNITS_PER_SAT token units, one instance per token.
///
/// - `deposit(amount, script)` takes `amount` units with `transferFrom` (the
///   depositor approves first); it is not payable.
/// - `withdraw()` pays with `transfer`.
/// - Tokens that return no bool (USDT) are accepted; a call that reverts or
///   returns false fails with TokenTransferFailed.
/// - Every token call must move exactly the amount: the contract's balance
///   is read before and after, and TokenAmountMismatch refuses
///   fee-on-transfer tokens and balances that change during the call.
/// - No deposit or withdraw can run inside another's token call (Reentrancy):
///   a token with transfer hooks can't interleave them.
/// Rebasing tokens break the invariant between calls, where no check can see
/// it, so they MUST NOT be used (SPEC 4.6).
contract UtxoChainToken is UtxoChainBase {
    /// The token, and its units per sat.
    address public immutable TOKEN;
    uint256 public immutable UNITS_PER_SAT;

    bool private transient entered;

    /// @param interval seconds per virtual block (2 on anvil, 60 on Base Sepolia)
    /// @param token the ERC-20: it must have code and answer balanceOf
    /// @param unitsPerSat token units per sat (SPEC 4.6): 10^(decimals - 8)
    ///        for a token with at least 8 decimals, else 1. A token without
    ///        decimals() may take any power of ten up to 10^18.
    constructor(uint256 interval, address token, uint256 unitsPerSat) UtxoChainBase(interval) {
        if (token.code.length == 0) revert BadToken();
        try IERC20(token).balanceOf(address(this)) returns (uint256) {}
        catch {
            revert BadToken();
        }
        try IERC20(token).decimals() returns (uint8 d) {
            if (unitsPerSat != 10 ** (d > 8 ? d - 8 : 0)) revert BadUnitsPerSat();
        } catch {
            uint256 u = unitsPerSat;
            while (u >= 10 && u % 10 == 0) u /= 10;
            if (u != 1) revert BadUnitsPerSat();
        }
        if (unitsPerSat > 1e18) revert BadUnitsPerSat();
        TOKEN = token;
        UNITS_PER_SAT = unitsPerSat;
    }

    function _unit() internal view override returns (uint256) {
        return UNITS_PER_SAT;
    }

    /// @notice Takes `amount` token units from msg.sender (approved first) and
    /// creates one output of amount / UNITS_PER_SAT sats paying
    /// `scriptPubKey`, as the output 0 of a synthetic deposit tx (SPEC 3.3).
    function deposit(uint256 amount, bytes calldata scriptPubKey) external returns (bytes32 txid) {
        return _deposit(amount, scriptPubKey);
    }

    function _collect(uint256 value) internal override {
        _tokenCall(abi.encodeCall(IERC20.transferFrom, (msg.sender, address(this), value)), value, true);
    }

    function _pay(address to, uint256 amount) internal override {
        _tokenCall(abi.encodeCall(IERC20.transfer, (to, amount)), amount, false);
    }

    /// Calls the token, under the lock, and checks the call moved exactly
    /// `amount` into (`incoming`) or out of this contract.
    function _tokenCall(bytes memory data, uint256 amount, bool incoming) private {
        if (entered) revert Reentrancy();
        entered = true;
        uint256 before = IERC20(TOKEN).balanceOf(address(this));
        (bool ok, bytes memory ret) = TOKEN.call(data);
        // No return data is a success (USDT); else it must be a true bool.
        if (!ok || (ret.length != 0 && (ret.length != 32 || abi.decode(ret, (uint256)) != 1))) {
            revert TokenTransferFailed();
        }
        uint256 now_ = IERC20(TOKEN).balanceOf(address(this));
        if (incoming ? now_ < before || now_ - before != amount : now_ > before || before - now_ != amount) {
            revert TokenAmountMismatch();
        }
        entered = false;
    }
}
