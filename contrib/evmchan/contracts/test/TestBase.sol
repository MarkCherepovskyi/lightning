// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// The Foundry cheatcodes these tests use (no forge-std).
interface Vm {
    function warp(uint256 timestamp) external;
    function roll(uint256 blockNumber) external;
    function deal(address who, uint256 balance) external;
    function etch(address who, bytes calldata code) external;
    function prank(address sender) external;
    function chainId(uint256 id) external;
    function skip(bool skipTest) external;
    function expectRevert(bytes calldata revertData) external;
    function expectEmit(bool topic1, bool topic2, bool topic3, bool data, address emitter) external;
    function exists(string calldata path) external view returns (bool);
    function readLine(string calldata path) external view returns (string memory);
    function closeFile(string calldata path) external;
    function parseBytes(string calldata s) external pure returns (bytes memory);
    function parseUint(string calldata s) external pure returns (uint256);
    function toString(uint256 v) external pure returns (string memory);
}

/// Minimal assertions and the log events forge decodes (DSTest style).
abstract contract TestBase {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    event log_named_uint(string key, uint256 val);
    event log_named_string(string key, string val);
    event log_string(string val);

    function assertTrue(bool c, string memory what) internal pure {
        if (!c) revert(string.concat("assertTrue failed: ", what));
    }

    function assertEq(bytes32 a, bytes32 b, string memory what) internal pure {
        if (a != b) revert(string.concat("assertEq(bytes32) failed: ", what));
    }

    function assertEq(uint256 a, uint256 b, string memory what) internal pure {
        if (a != b) {
            revert(string.concat("assertEq(uint256) failed: ", what, ": ", str(a), " != ", str(b)));
        }
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
