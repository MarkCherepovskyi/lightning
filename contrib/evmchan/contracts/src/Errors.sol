// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

// Every revert of UtxoChain (SPEC 4.2). Errors about one input carry its index.

// Contract
error TooEarly(); // height 0 holds only its coinbase (SPEC 3.1)
error BadInterval();
error HeightOverflow(); // past 2^32 - 1 virtual blocks
error BadDepositValue(); // not a whole number of sats, below 1 sat, or above MAX_MONEY
error BadDepositScript(); // over 10,000 bytes, an exit script, or OP_RETURN
error NothingToWithdraw();
error WithdrawFailed();

// 4.3.1 Encoding
error TxTooLarge();
error Truncated();
error NonCanonicalCompactSize();
error BadSegwitFlag();
error EmptyWitnesses(); // segwit marker with every witness empty
error NoInputs();
error NoOutputs();
error TrailingBytes();

// 4.3.2 Inputs
error PrevoutCountMismatch();
error ScriptSigNotEmpty(uint256 input);
error NullPrevout(uint256 input);
error DuplicateInput(uint256 input);
error UnknownUtxo(uint256 input); // utxo[key] is not the prevout's commitment

// 4.3.3 Amounts, 4.3.4 sigops
error AmountTooLarge();
error OutputsExceedInputs();
error TooManySigops();

// 4.3.5 lock time, 4.3.6 relative lock
error NonFinal();
error TimeRelativeLockUnsupported(uint256 input);
error RelativeLockNotMet(uint256 input);

// 4.3.7 - 4.3.10 scripts and signatures
error UnsupportedScriptPubKey(uint256 input);
error BadWitness(uint256 input);
error KeyHashMismatch(uint256 input);
error WitnessScriptMismatch(uint256 input);
error UnknownWitnessScript(uint256 input);
error BadPreimage(uint256 input);
error CsvNotMet(uint256 input);
error CltvNotMet(uint256 input);
error BadSignatureEncoding(uint256 input);
error BadSignature(uint256 input);
error BadPublicKey(uint256 input); // a key a CHECKSIG uses is not a compressed point on the curve
error NoMatchingOutput(uint256 input); // taproot SINGLE without an output of the same index
