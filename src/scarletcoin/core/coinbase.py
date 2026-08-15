"""Coinbase transactions.

The coinbase is the first transaction of every block; it has no real inputs and
mints the block subsidy plus the fees paid by the other transactions.

Its ``coinbase_data`` field starts with the block height as a little-endian
uint32.  Committing to the height makes every coinbase — and therefore every
block — unique, so two blocks at different heights can never share a
transaction id.  Anything after those four bytes is free space the miner uses as
an extra nonce (and, historically, for messages).
"""

from __future__ import annotations

from scarletcoin.core.transaction import (
    COINBASE_OUTPOINT,
    MAX_COINBASE_DATA,
    Transaction,
    TransactionError,
    TxInput,
    TxOutput,
)

__all__ = ["HEIGHT_PREFIX_SIZE", "build_coinbase", "coinbase_height", "encode_coinbase_data"]

HEIGHT_PREFIX_SIZE = 4


def encode_coinbase_data(height: int, extra: bytes = b"") -> bytes:
    """Return the coinbase data field for ``height`` followed by ``extra``."""
    if not 0 <= height <= 0xFFFFFFFF:
        raise TransactionError(f"block height out of range: {height}")
    data = height.to_bytes(HEIGHT_PREFIX_SIZE, "little") + bytes(extra)
    if len(data) > MAX_COINBASE_DATA:
        raise TransactionError(
            f"coinbase data is {len(data)} bytes, the limit is {MAX_COINBASE_DATA}"
        )
    return data


def coinbase_height(transaction: Transaction) -> int:
    """Return the height committed to by a coinbase transaction.

    Raises:
        TransactionError: if the transaction is not a coinbase or its data is too short.
    """
    if not transaction.is_coinbase:
        raise TransactionError("transaction is not a coinbase")
    data = transaction.coinbase_data
    if len(data) < HEIGHT_PREFIX_SIZE:
        raise TransactionError("coinbase data does not start with a block height")
    return int.from_bytes(data[:HEIGHT_PREFIX_SIZE], "little")


def build_coinbase(
    *,
    height: int,
    reward: int,
    pubkey_hash: bytes,
    extra: bytes = b"",
    lock_time: int = 0,
) -> Transaction:
    """Build a coinbase transaction paying ``reward`` to ``pubkey_hash``."""
    return Transaction(
        version=1,
        inputs=(TxInput(COINBASE_OUTPOINT),),
        outputs=(TxOutput(reward, pubkey_hash),),
        lock_time=lock_time,
        coinbase_data=encode_coinbase_data(height, extra),
    )
