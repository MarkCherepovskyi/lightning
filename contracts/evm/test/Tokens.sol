// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// Mock ERC-20s for UtxoChainToken's tests (no dependencies). Each mints
/// freely. `MockERC20` is a standard token with configurable decimals; the
/// others change one thing about it.
contract MockERC20 {
    uint8 public immutable decimals;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(uint8 decimals_) {
        decimals = decimals_;
    }

    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external virtual returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external virtual returns (bool) {
        _spend(from, amount);
        _move(from, to, amount);
        return true;
    }

    function _spend(address from, uint256 amount) internal {
        uint256 a = allowance[from][msg.sender];
        require(a >= amount, "allowance");
        if (a != type(uint256).max) allowance[from][msg.sender] = a - amount;
    }

    function _move(address from, address to, uint256 amount) internal virtual {
        require(balanceOf[from] >= amount, "balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }
}

/// USDT-style: transfer and transferFrom return nothing.
contract NoBoolToken is MockERC20 {
    constructor() MockERC20(6) {}

    function transfer(address to, uint256 amount) external override returns (bool) {
        _move(msg.sender, to, amount);
        assembly ("memory-safe") {
            return(0, 0)
        }
    }

    function transferFrom(address from, address to, uint256 amount) external override returns (bool) {
        _spend(from, amount);
        _move(from, to, amount);
        assembly ("memory-safe") {
            return(0, 0)
        }
    }
}

/// Returns false (moving nothing) once `failing` is set; or a malformed
/// return value (two words) once `malformed` is set.
contract FalseToken is MockERC20 {
    bool public failing;
    bool public malformed;

    constructor() MockERC20(6) {}

    function set(bool failing_, bool malformed_) external {
        failing = failing_;
        malformed = malformed_;
    }

    function transfer(address to, uint256 amount) external override returns (bool) {
        return _maybe(msg.sender, to, amount);
    }

    function transferFrom(address from, address to, uint256 amount) external override returns (bool) {
        if (failing) return false;
        _spend(from, amount);
        return _maybe(from, to, amount);
    }

    function _maybe(address from, address to, uint256 amount) internal returns (bool) {
        if (failing) return false;
        _move(from, to, amount);
        if (malformed) {
            assembly ("memory-safe") {
                mstore(0, 1)
                mstore(32, 1)
                return(0, 64)
            }
        }
        return true;
    }
}

/// Fee on transfer of `feeBps`/10,000 (at least 1 unit), burned: taken from
/// what the recipient gets, or (`fromSender`) charged to the sender on top.
contract FeeToken is MockERC20 {
    uint256 public feeBps;
    bool public fromSender;

    constructor() MockERC20(6) {}

    function setFee(uint256 bps, bool fromSender_) external {
        feeBps = bps;
        fromSender = fromSender_;
    }

    function _move(address from, address to, uint256 amount) internal override {
        uint256 fee = feeBps == 0 ? 0 : amount * feeBps / 10_000 + 1;
        uint256 debit = fromSender ? amount + fee : amount;
        require(balanceOf[from] >= debit, "balance");
        balanceOf[from] -= debit;
        balanceOf[to] += debit - fee;
        totalSupply -= fee;
        emit Transfer(from, to, debit - fee);
    }
}

/// ERC-777-style hooks: after moving the balance, calls
/// onTokenTransfer(from, to, amount) on the sender (for transferFrom) and on
/// the recipient, when they have code. A hook that reverts with data
/// reverts the transfer; one without the function is ignored.
contract HookToken is MockERC20 {
    constructor() MockERC20(18) {}

    function transfer(address to, uint256 amount) external override returns (bool) {
        _move(msg.sender, to, amount);
        _hook(to, msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external override returns (bool) {
        _spend(from, amount);
        _move(from, to, amount);
        _hook(from, from, to, amount);
        _hook(to, from, to, amount);
        return true;
    }

    function _hook(address who, address from, address to, uint256 amount) internal {
        if (who.code.length == 0) return;
        (bool ok, bytes memory r) =
            who.call(abi.encodeWithSignature("onTokenTransfer(address,address,uint256)", from, to, amount));
        if (!ok && r.length != 0) {
            assembly ("memory-safe") {
                revert(add(r, 32), mload(r))
            }
        }
    }
}

/// A token without decimals() (it is optional in ERC-20): only balanceOf.
contract NoDecimalsToken {
    function balanceOf(address) external pure returns (uint256) {
        return 0;
    }
}
