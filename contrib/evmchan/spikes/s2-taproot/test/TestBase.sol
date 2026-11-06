// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// The few Foundry cheatcodes these tests use (no forge-std).
interface Vm {
    function expectRevert(bytes4 selector) external;
    function expectRevert(bytes calldata revertData) external;
}

/// Minimal assertions and log events forge decodes (DSTest style).
abstract contract TestBase {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    event log_named_uint(string key, uint256 val);

    function assertTrue(bool c, string memory what) internal pure {
        if (!c) revert(string.concat("assertTrue failed: ", what));
    }

    function assertFalse(bool c, string memory what) internal pure {
        if (c) revert(string.concat("assertFalse failed: ", what));
    }

    function assertEq(bytes32 a, bytes32 b, string memory what) internal pure {
        if (a != b) revert(string.concat("assertEq(bytes32) failed: ", what));
    }

    function assertEq(uint256 a, uint256 b, string memory what) internal pure {
        if (a != b) revert(string.concat("assertEq(uint256) failed: ", what));
    }

    function assertEq(bytes memory a, bytes memory b, string memory what) internal pure {
        if (keccak256(a) != keccak256(b)) revert(string.concat("assertEq(bytes) failed: ", what));
    }

    function str(uint256 x) internal pure returns (string memory) {
        if (x == 0) return "0";
        bytes memory b;
        while (x != 0) {
            b = abi.encodePacked(bytes1(uint8(48 + x % 10)), b);
            x /= 10;
        }
        return string(b);
    }
}
