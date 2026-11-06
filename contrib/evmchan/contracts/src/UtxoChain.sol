// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BtcTx, TxView, Prevout} from "./BtcTx.sol";
import {Validation} from "./Validation.sol";
import "./Errors.sol";

/// @title A Bitcoin-style UTXO set of ETH, spent by Bitcoin transactions
/// (SPEC 4). 1 sat = 10^10 wei. Heights follow the wall clock:
/// H(t) = (t - GENESIS_TIME) / INTERVAL.
///
/// Errors (Errors.sol): TooEarly, BadInterval, HeightOverflow, BadDepositValue,
/// BadDepositScript, NothingToWithdraw, WithdrawFailed; and for `submit`
/// TxTooLarge, Truncated, NonCanonicalCompactSize, BadSegwitFlag,
/// EmptyWitnesses, NoInputs, NoOutputs, TrailingBytes, PrevoutCountMismatch,
/// ScriptSigNotEmpty, NullPrevout, DuplicateInput, UnknownUtxo,
/// AmountTooLarge, OutputsExceedInputs, TooManySigops, NonFinal,
/// TimeRelativeLockUnsupported, RelativeLockNotMet, UnsupportedScriptPubKey,
/// BadWitness, KeyHashMismatch, WitnessScriptMismatch, UnknownWitnessScript,
/// BadPreimage, CsvNotMet, CltvNotMet, BadSignatureEncoding, BadSignature,
/// BadPublicKey, NoMatchingOutput.
///
/// Invariant (SPEC 4.1): address(this).balance >= the sum of all UTXO amounts
/// * 10^10 + the sum of `withdrawable`. A deposit adds msg.value to both
/// sides; a submit moves value from spent UTXOs to new UTXOs, exits and the
/// fee (inputs >= outputs + fee exactly, OP_RETURN amounts leaving the
/// right-hand side); a withdraw lowers both by the same amount.
contract UtxoChain {
    uint256 internal constant SAT = 1e10; // wei per sat
    uint256 internal constant MAX_DEPOSIT_SCRIPT = 10_000;

    event TxApplied(uint32 indexed height, bytes32 indexed txid, bytes rawTx);

    uint256 public immutable GENESIS_TIME;
    uint256 public immutable INTERVAL;
    uint256 public immutable DEPLOY_BLOCK;

    /// keccak256(txid, uint32 vout) => keccak256(uint64 amount, scriptPubKey,
    /// uint32 height); zero when there is no such unspent output.
    mapping(bytes32 => bytes32) public utxo;
    /// Exits and fees, in wei, pulled with `withdraw`.
    mapping(address => uint256) public withdrawable;
    uint256 public depositNonce;
    /// The number of TxApplied events emitted so far, and a hash chain over
    /// them: logHash = keccak256(logHash, uint32 height, txid) per event, in
    /// order (SPEC 4.1). Read at a block (EIP-1898), they let an index prove
    /// it has every log up to that block.
    uint64 public txCount;
    bytes32 public logHash;

    /// @param interval seconds per virtual block (2 on anvil, 60 on Base Sepolia)
    constructor(uint256 interval) {
        if (interval == 0) revert BadInterval();
        GENESIS_TIME = block.timestamp;
        INTERVAL = interval;
        DEPLOY_BLOCK = block.number;
    }

    // ------------------------------------------------------------ SPEC 4.2

    /// @notice Creates one output of msg.value / 10^10 sats paying
    /// `scriptPubKey`, as the output 0 of a synthetic deposit tx (SPEC 3.3).
    function deposit(bytes calldata scriptPubKey) external payable returns (bytes32 txid) {
        uint32 h = _applyHeight();
        if (msg.value % SAT != 0 || msg.value < SAT || msg.value / SAT > Validation.MAX_MONEY) {
            revert BadDepositValue();
        }
        if (scriptPubKey.length > MAX_DEPOSIT_SCRIPT || _isExit(scriptPubKey) || _isOpReturn(scriptPubKey)) {
            revert BadDepositScript();
        }
        uint64 amount = uint64(msg.value / SAT);
        // A prevout that is never in the UTXO set (so the tx cannot be
        // replayed) and is not the null prevout (so it is not a coinbase).
        bytes32 prev = keccak256(abi.encodePacked("evmchan deposit", block.chainid, address(this), depositNonce));
        // version 2, 1 input (prev:0, empty scriptSig, sequence 0xffffffff),
        // 1 output, locktime 0; no witness
        bytes memory rawTx = abi.encodePacked(
            hex"0200000001",
            prev,
            hex"0000000000ffffffff01",
            BtcTx.le64(amount),
            BtcTx.compactSizeBytes(scriptPubKey.length),
            scriptPubKey,
            hex"00000000"
        );
        txid = sha256(abi.encodePacked(sha256(rawTx)));
        depositNonce += 1;
        utxo[utxoKey(txid, 0)] = _commitment(amount, scriptPubKey, h);
        _chainLog(h, txid);
        emit TxApplied(h, txid, rawTx);
    }

    /// @notice Validates `rawTx` against `prevouts` at the current height
    /// (SPEC 4.3) and applies it (SPEC 4.4). Anyone may submit any valid tx;
    /// the fee goes to the submitter.
    function submit(bytes calldata rawTx, Prevout[] calldata prevouts) external returns (bytes32 txid) {
        uint32 h = _applyHeight();
        TxView memory t = Validation.parse(rawTx, prevouts);
        // The UTXO set first: a wrong or spent prevout fails before any
        // signature is checked. The call reverts as a whole on any failure.
        _spendInputs(rawTx, t, prevouts);
        uint256 fee = Validation.check(rawTx, t, prevouts, h, _mtp(h));
        txid = t.txid;
        _createOutputs(rawTx, t, h);
        if (fee != 0) withdrawable[msg.sender] += fee * SAT;
        _chainLog(h, txid);
        emit TxApplied(h, txid, rawTx);
    }

    /// @notice Pays msg.sender its `withdrawable` (checks-effects-interactions:
    /// the balance is zeroed before the call).
    function withdraw() external {
        uint256 amount = withdrawable[msg.sender];
        if (amount == 0) revert NothingToWithdraw();
        withdrawable[msg.sender] = 0;
        (bool ok,) = msg.sender.call{value: amount}("");
        if (!ok) revert WithdrawFailed();
    }

    /// @notice H(block.timestamp).
    function height() external view returns (uint32) {
        return _height();
    }

    function isUnspent(bytes32 txid, uint32 vout, uint64 amount, bytes calldata scriptPubKey, uint32 height_)
        external
        view
        returns (bool)
    {
        return utxo[utxoKey(txid, vout)] == _commitment(amount, scriptPubKey, height_);
    }

    function utxoKey(bytes32 txid, uint32 vout) public pure returns (bytes32) {
        return keccak256(abi.encodePacked(txid, vout));
    }

    // ------------------------------------------------------------ SPEC 4.4

    /// Deletes each input's UTXO, after checking it is the prevout's
    /// commitment (SPEC 4.3.2). Deleting as we go also rejects an outpoint
    /// spent twice in the tx: its second occurrence finds no UTXO. That is
    /// O(n), and the whole call reverts on any failure.
    function _spendInputs(bytes calldata raw, TxView memory t, Prevout[] calldata pv) internal {
        for (uint256 i; i < t.nIn; ++i) {
            bytes32 txid = BtcTx.prevTxid(raw, t, i);
            uint32 vout = BtcTx.prevVout(raw, t, i);
            bytes32 key = utxoKey(txid, vout);
            if (utxo[key] != _commitment(pv[i].amount, pv[i].scriptPubKey, pv[i].height)) {
                for (uint256 j; j < i; ++j) {
                    if (BtcTx.prevTxid(raw, t, j) == txid && BtcTx.prevVout(raw, t, j) == vout) {
                        revert DuplicateInput(i);
                    }
                }
                revert UnknownUtxo(i);
            }
            delete utxo[key];
        }
    }

    /// Stores each output, except exits (credited to `withdrawable`) and
    /// OP_RETURN outputs (whose amounts are burned).
    /// @dev An output's key cannot already be in use: txids are unique, since a
    /// tx spends at least one UTXO, which no other valid tx can spend again,
    /// and a deposit's txid commits to a fresh nonce.
    function _createOutputs(bytes calldata raw, TxView memory t, uint32 h) internal {
        uint256 n = BtcTx.nOut(t);
        for (uint256 j; j < n; ++j) {
            uint64 amount = BtcTx.outAmount(raw, t, j);
            bytes calldata spk = BtcTx.outScript(raw, t, j);
            if (_isExit(spk)) {
                withdrawable[address(bytes20(spk[2:22]))] += amount * SAT;
            } else if (!_isOpReturn(spk)) {
                utxo[utxoKey(t.txid, uint32(j))] = _commitment(amount, spk, h);
            }
        }
    }

    // ------------------------------------------------------------ helpers

    /// SPEC 4.1/4.4: count the event and extend the log hash, just before
    /// every TxApplied.
    function _chainLog(uint32 h, bytes32 txid) internal {
        txCount += 1;
        logHash = keccak256(abi.encodePacked(logHash, h, txid));
    }

    function _commitment(uint64 amount, bytes calldata scriptPubKey, uint32 h) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked(amount, scriptPubKey, h));
    }

    /// Segwit v16 with a 20-byte program: `6014<20 bytes>` (SPEC 4.4).
    function _isExit(bytes calldata spk) internal pure returns (bool) {
        return spk.length == 22 && spk[0] == 0x60 && spk[1] == 0x14;
    }

    function _isOpReturn(bytes calldata spk) internal pure returns (bool) {
        return spk.length != 0 && spk[0] == 0x6a;
    }

    function _height() internal view returns (uint32) {
        uint256 h = (block.timestamp - GENESIS_TIME) / INTERVAL;
        if (h > type(uint32).max) revert HeightOverflow();
        return uint32(h);
    }

    /// The height a deposit or submit applies at; nothing is applied at 0.
    function _applyHeight() internal view returns (uint32 h) {
        h = _height();
        if (h == 0) revert TooEarly();
    }

    /// MTP(h) = T(max(h - 6, 0)), as SPEC 4.3.5 defines it. From h = 11 on it
    /// is the median of the 11 block times before h (they increase by
    /// INTERVAL); below that it is lower than Bitcoin's median of the blocks
    /// there are, so time locks there are met later, never earlier.
    function _mtp(uint32 h) internal view returns (uint256) {
        return GENESIS_TIME + (h > 6 ? uint256(h) - 6 : 0) * INTERVAL;
    }
}
