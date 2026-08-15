"""Block templates.

A node hands a miner everything needed to build the next block *except* the
coinbase: the miner supplies its own payout address and extra nonce, assembles
the coinbase locally and computes the Merkle root itself.  That way the node
never has to know the miner's keys and the miner is free to roll the extra nonce
without asking for a new template.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from scarletcoin.core.block import Block
from scarletcoin.core.chain import Blockchain
from scarletcoin.core.coinbase import build_coinbase
from scarletcoin.core.mempool import Mempool
from scarletcoin.core.pow import bits_to_target
from scarletcoin.core.transaction import Transaction

__all__ = ["BlockTemplate", "create_block_template"]

#: Bytes reserved in every block for the coinbase transaction.
COINBASE_RESERVE = 1_000


@dataclass(frozen=True)
class BlockTemplate:
    """Instructions for building the next block."""

    height: int
    prev_hash: bytes
    bits: int
    min_time: int
    """The block timestamp must be strictly greater than this."""
    current_time: int
    coinbase_value: int
    """Subsidy plus the fees of the included transactions."""
    transactions: tuple[Transaction, ...] = field(default_factory=tuple)
    version: int = 1

    @property
    def target(self) -> int:
        """The target the block hash must not exceed."""
        return bits_to_target(self.bits)

    def build_block(
        self,
        *,
        pubkey_hash: bytes,
        extra: bytes = b"",
        timestamp: int | None = None,
        nonce: int = 0,
    ) -> Block:
        """Assemble a candidate block paying the reward to ``pubkey_hash``."""
        coinbase = build_coinbase(
            height=self.height,
            reward=self.coinbase_value,
            pubkey_hash=pubkey_hash,
            extra=extra,
        )
        chosen = self.current_time if timestamp is None else timestamp
        return Block.create(
            prev_hash=self.prev_hash,
            transactions=[coinbase, *self.transactions],
            bits=self.bits,
            timestamp=max(chosen, self.min_time + 1),
            version=self.version,
            nonce=nonce,
        )

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation (used by the RPC interface)."""
        return {
            "height": self.height,
            "previous_block": self.prev_hash[::-1].hex(),
            "bits": f"{self.bits:#010x}",
            "target": f"{self.target:064x}",
            "min_time": self.min_time,
            "current_time": self.current_time,
            "coinbase_value": self.coinbase_value,
            "version": self.version,
            "transactions": [tx.serialize().hex() for tx in self.transactions],
        }

    @classmethod
    def from_dict(cls, data: dict) -> BlockTemplate:
        """Rebuild a template from its JSON representation."""
        return cls(
            height=int(data["height"]),
            prev_hash=bytes.fromhex(data["previous_block"])[::-1],
            bits=int(str(data["bits"]), 16),
            min_time=int(data["min_time"]),
            current_time=int(data["current_time"]),
            coinbase_value=int(data["coinbase_value"]),
            transactions=tuple(
                Transaction.deserialize(bytes.fromhex(raw)) for raw in data["transactions"]
            ),
            version=int(data.get("version", 1)),
        )


def create_block_template(
    chain: Blockchain,
    mempool: Mempool | None = None,
    *,
    timestamp: int | None = None,
) -> BlockTemplate:
    """Build a template for the block that would extend the current tip."""
    params = chain.params
    tip = chain.tip
    height = tip.height + 1
    bits = chain.next_bits()
    min_time = chain.median_time_past(tip)
    now = int(time.time()) if timestamp is None else timestamp

    transactions: list[Transaction] = []
    fees = 0
    if mempool is not None:
        transactions, fees = mempool.collect(
            max_bytes=params.max_block_size - COINBASE_RESERVE, height=height
        )
    return BlockTemplate(
        height=height,
        prev_hash=tip.hash,
        bits=bits,
        min_time=min_time,
        current_time=max(now, min_time + 1),
        coinbase_value=params.subsidy(height) + fees,
        transactions=tuple(transactions),
    )
