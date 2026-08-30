"""Block templates.

A node hands a miner everything needed to build the next block *except* the
coinbase: the miner supplies its own payout address and extra nonce, assembles
the coinbase locally and computes the Merkle root itself.  That way the node
never has to know the miner's keys and the miner is free to roll the extra nonce
without asking for a new template.

Also provides :class:`AuxBlockCandidate` for merged-mining (AuxPoW) work.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from scarletcoin.core.block import Block
from scarletcoin.core.chain import Blockchain
from scarletcoin.core.coinbase import build_coinbase
from scarletcoin.core.mempool import Mempool
from scarletcoin.core.pow import bits_to_target
from scarletcoin.core.transaction import Transaction

__all__ = [
    "AuxBlockCandidate",
    "BlockTemplate",
    "create_aux_block",
    "create_block_template",
]

#: Bytes reserved in every block for the coinbase transaction.
COINBASE_RESERVE = 1_000

#: Maximum number of active AuxPoW candidates to retain.
_MAX_AUX_CANDIDATES = 64


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
    min_time = chain.median_time_past(tip)
    now = int(time.time()) if timestamp is None else timestamp
    current_time = max(now, min_time + 1)
    bits = chain.next_bits(timestamp=current_time)

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
        current_time=current_time,
        coinbase_value=params.subsidy(height) + fees,
        transactions=tuple(transactions),
    )


# ----------------------------------------------------------------- AuxPoW candidates


@dataclass(frozen=True)
class AuxBlockCandidate:
    """A frozen ScarletCoin block template for merged-mining (AuxPoW).

    Once frozen, the candidate represents a specific ScarletCoin block header/hash
    that must be committed into the parent Bitcoin coinbase.  The candidate is
    invalidated when the underlying ScarletCoin tip changes.
    """

    height: int
    """The ScarletCoin block height this candidate targets."""
    prev_hash: bytes
    """Hash of the previous ScarletCoin block (internal byte order)."""
    bits: int
    """Compact target for ScarletCoin."""
    coinbase_value: int
    """Subsidy plus fees."""
    coinbase_hash: bytes
    """The coinbase transaction hash — this is what the parent coinbase must
    commit to through the auxiliary Merkle tree."""
    aux_block_hash: bytes
    """The full ScarletCoin block hash (double SHA-256d of the 80-byte header,
    internal byte order)."""
    chain_id: int
    """The AuxPoW chain ID of this network."""
    target: int
    """The integer target the parent PoW must not exceed."""
    commitment_nonce: int
    """Random 32-bit nonce for the deterministic index calculation."""
    pubkey_hash: bytes
    """The 20-byte pubkey hash that will receive the block reward."""
    transactions: tuple[Transaction, ...] = field(default_factory=tuple)
    """The non-coinbase transactions to include in the ScarletCoin block."""

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation, for RPC."""
        return {
            "hash": self.aux_block_hash[::-1].hex(),
            "chainid": self.chain_id,
            "target": f"{self.target:064x}",
            "bits": f"{self.bits:#010x}",
            "height": self.height,
            "previousblock": self.prev_hash[::-1].hex(),
            "coinbasevalue": self.coinbase_value,
            "coinbasehash": self.coinbase_hash[::-1].hex(),
            "tree_size": 1,
            "nonce": self.commitment_nonce,
            "pubkey_hash": self.pubkey_hash.hex(),
        }

    def build_block(self) -> Block:
        """Reconstruct the full ScarletCoin block from this candidate."""
        from scarletcoin.core.block import Block
        from scarletcoin.core.coinbase import build_coinbase

        coinbase = build_coinbase(
            height=self.height,
            reward=self.coinbase_value,
            pubkey_hash=self.pubkey_hash,
            extra=b"auxpow",
        )
        return Block.create(
            prev_hash=self.prev_hash,
            transactions=[coinbase, *self.transactions],
            bits=self.bits,
            timestamp=int(time.time()),
            version=1,
            nonce=0,
        )


def create_aux_block(
    chain: Blockchain,
    mempool: Mempool | None = None,
    *,
    pubkey_hash: bytes,
    timestamp: int | None = None,
) -> AuxBlockCandidate:
    """Create a frozen AuxPoW candidate for the current ScarletCoin tip.

    The caller (a Bitcoin pool/proxy) must embed the returned commitment
    information into the parent Bitcoin coinbase so that the ScarletCoin block
    hash can be proved.

    Args:
        chain: The ScarletCoin blockchain.
        mempool: Optional mempool for including pending transactions.
        pubkey_hash: The ScarletCoin address that will receive the SCT reward.
        timestamp: Override timestamp; defaults to current time.

    Returns:
        An :class:`AuxBlockCandidate` that can be serialised and handed to a
        merged-mining coordinator.
    """
    params = chain.params
    if params.auxpow_chain_id == 0:
        raise ValueError(f"AuxPoW is not configured for network {params.name}")

    tip = chain.tip
    height = tip.height + 1
    min_time = chain.median_time_past(tip)
    now = int(time.time()) if timestamp is None else timestamp
    current_time = max(now, min_time + 1)
    bits = chain.next_bits(timestamp=current_time)

    # Collect transactions and build the coinbase.
    transactions: list[Transaction] = []
    fees = 0
    if mempool is not None:
        transactions, fees = mempool.collect(
            max_bytes=params.max_block_size - COINBASE_RESERVE, height=height
        )
    coinbase = build_coinbase(
        height=height,
        reward=params.subsidy(height) + fees,
        pubkey_hash=pubkey_hash,
        extra=b"auxpow",
    )

    # Assemble the candidate ScarletCoin block.
    candidate_block = Block.create(
        prev_hash=tip.hash,
        transactions=[coinbase, *transactions],
        bits=bits,
        timestamp=current_time,
        version=1,
        nonce=0,  # nonce is irrelevant for AuxPoW; the parent header provides the proof
    )

    # Generate a random nonce for deterministic index calculation.
    commitment_nonce = int.from_bytes(os.urandom(4), "little") & 0xFFFFFFFF

    return AuxBlockCandidate(
        height=height,
        prev_hash=tip.hash,
        bits=bits,
        coinbase_value=params.subsidy(height) + fees,
        coinbase_hash=coinbase.txid(),
        aux_block_hash=candidate_block.hash(),
        chain_id=params.auxpow_chain_id,
        target=bits_to_target(bits),
        commitment_nonce=commitment_nonce,
        pubkey_hash=pubkey_hash,
        transactions=tuple(transactions),
    )
