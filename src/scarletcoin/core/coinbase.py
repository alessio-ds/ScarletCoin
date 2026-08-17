"""Coinbase transactions (v2).

The coinbase has exactly one input with an empty ring and no key image.
It pays the block subsidy plus fees to a one-time public key derived
from the miner's stealth address.
"""

from __future__ import annotations

from scarletcoin.core.transaction import (
    MAX_COINBASE_DATA,
    Transaction,
    TransactionError,
    TxInput,
    TxOutput,
)

__all__ = ["HEIGHT_PREFIX_SIZE", "build_coinbase", "coinbase_height", "encode_coinbase_data"]

HEIGHT_PREFIX_SIZE = 4


def encode_coinbase_data(height: int, extra: bytes = b"") -> bytes:
    if not 0 <= height <= 0xFFFFFFFF:
        raise TransactionError(f"block height out of range: {height}")
    data = height.to_bytes(HEIGHT_PREFIX_SIZE, "little") + bytes(extra)
    if len(data) > MAX_COINBASE_DATA:
        raise TransactionError(
            f"coinbase data is {len(data)} bytes, the limit is {MAX_COINBASE_DATA}"
        )
    return data


def coinbase_height(transaction: Transaction) -> int:
    if not transaction.is_coinbase:
        raise TransactionError("transaction is not a coinbase")
    data = transaction.extra
    if len(data) < HEIGHT_PREFIX_SIZE:
        raise TransactionError("coinbase data does not start with a block height")
    return int.from_bytes(data[:HEIGHT_PREFIX_SIZE], "little")


def build_coinbase(
    *,
    height: int,
    reward: int,
    one_time_key: bytes,
    tx_public_key: bytes,
    extra: bytes = b"",
    lock_time: int = 0,
) -> Transaction:
    """Build a coinbase transaction paying ``reward`` to ``one_time_key``."""
    return Transaction(
        version=2,
        inputs=(TxInput(ring=(), key_image=b""),),
        outputs=(TxOutput(reward, one_time_key),),
        lock_time=lock_time,
        tx_public_key=tx_public_key,
        extra=encode_coinbase_data(height, extra),
    )