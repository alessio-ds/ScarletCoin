"""Blocks, block headers and Merkle trees."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from scarletcoin.core.pow import check_proof_of_work, hash_to_int
from scarletcoin.core.serialize import Reader, SerializationError, Writer
from scarletcoin.core.transaction import Transaction
from scarletcoin.crypto.hashing import hash256

if TYPE_CHECKING:  # pragma: no cover - avoids circular import at type-check time
    from scarletcoin.core.auxpow import AuxPoW

__all__ = [
    "BLOCK_HEADER_SIZE",
    "Block",
    "BlockError",
    "BlockHeader",
    "merkle_root",
]

BLOCK_HEADER_SIZE = 80


class BlockError(ValueError):
    """Raised when a block or header is structurally invalid."""


def merkle_root(txids: list[bytes]) -> bytes:
    """Return the Merkle root of ``txids``.

    Levels with an odd number of nodes duplicate the last node, as in Bitcoin.
    Blocks containing duplicate transaction ids are rejected elsewhere, which
    removes the ambiguity this duplication would otherwise allow.
    """
    if not txids:
        raise BlockError("cannot compute a Merkle root of zero transactions")
    level = list(txids)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hash256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


@dataclass(frozen=True, slots=True)
class BlockHeader:
    """The 80 bytes that proof of work is computed over."""

    version: int
    prev_hash: bytes
    merkle_root: bytes
    timestamp: int
    bits: int
    nonce: int

    def __post_init__(self) -> None:
        if len(self.prev_hash) != 32:
            raise BlockError("previous block hash must be 32 bytes")
        if len(self.merkle_root) != 32:
            raise BlockError("Merkle root must be 32 bytes")
        for name in ("version", "timestamp", "bits", "nonce"):
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
                raise BlockError(f"header field {name} must be a uint32, got {value!r}")

    def serialize(self) -> bytes:
        """Return the canonical 80-byte encoding."""
        return (
            Writer()
            .uint32(self.version)
            .hash32(self.prev_hash)
            .hash32(self.merkle_root)
            .uint32(self.timestamp)
            .uint32(self.bits)
            .uint32(self.nonce)
            .getvalue()
        )

    @classmethod
    def deserialize(cls, data: bytes) -> BlockHeader:
        """Parse an 80-byte header."""
        if len(data) != BLOCK_HEADER_SIZE:
            raise SerializationError(
                f"block header must be {BLOCK_HEADER_SIZE} bytes, got {len(data)}"
            )
        return cls.read(Reader(data))

    @classmethod
    def read(cls, reader: Reader) -> BlockHeader:
        """Parse one header from ``reader``."""
        return cls(
            version=reader.uint32(),
            prev_hash=reader.hash32(),
            merkle_root=reader.hash32(),
            timestamp=reader.uint32(),
            bits=reader.uint32(),
            nonce=reader.uint32(),
        )

    def hash(self) -> bytes:
        """Return the block hash (double SHA-256 of the header, internal order)."""
        return hash256(self.serialize())

    def hash_hex(self) -> str:
        """Return the block hash as a big-endian hex string (display order)."""
        return self.hash()[::-1].hex()

    def work_value(self) -> int:
        """Return the block hash as an integer, for comparison against a target."""
        return hash_to_int(self.hash())

    def check_proof_of_work(self, *, pow_limit: int) -> bool:
        """Return ``True`` if the header's hash meets its own claimed target."""
        return check_proof_of_work(self.hash(), self.bits, pow_limit=pow_limit)

    def with_nonce(self, nonce: int) -> BlockHeader:
        """Return a copy of the header with a different nonce."""
        return replace(self, nonce=nonce)

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation."""
        return {
            "hash": self.hash_hex(),
            "version": self.version,
            "previous_block": self.prev_hash[::-1].hex(),
            "merkle_root": self.merkle_root[::-1].hex(),
            "timestamp": self.timestamp,
            "bits": f"{self.bits:#010x}",
            "nonce": self.nonce,
        }


@dataclass(frozen=True, slots=True)
class Block:
    """A block header together with its transactions.

    May optionally carry an :class:`~scarletcoin.core.auxpow.AuxPoW` payload for
    merged-mined blocks.  The AuxPoW is stored separately from the header so that
    the ScarletCoin block hash is not affected by it.
    """

    header: BlockHeader
    transactions: tuple[Transaction, ...] = field(default_factory=tuple)
    auxpow: object | None = field(default=None)
    """An optional :class:`~scarletcoin.core.auxpow.AuxPoW` proof for merged mining.
    ``None`` for native-PoW blocks."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "transactions", tuple(self.transactions))

    @classmethod
    def create(
        cls,
        *,
        prev_hash: bytes,
        transactions: list[Transaction],
        bits: int,
        timestamp: int | None = None,
        version: int = 1,
        nonce: int = 0,
        auxpow: object | None = None,
    ) -> Block:
        """Assemble a block, computing its Merkle root from ``transactions``."""
        if not transactions:
            raise BlockError("a block must contain at least the coinbase transaction")
        header = BlockHeader(
            version=version,
            prev_hash=prev_hash,
            merkle_root=merkle_root([tx.txid() for tx in transactions]),
            timestamp=int(time.time()) if timestamp is None else timestamp,
            bits=bits,
            nonce=nonce,
        )
        return cls(header, tuple(transactions), auxpow=auxpow)

    # ---------------------------------------------------------------- helpers

    def hash(self) -> bytes:
        """Return the block hash."""
        return self.header.hash()

    def hash_hex(self) -> str:
        """Return the block hash in display order."""
        return self.header.hash_hex()

    @property
    def has_auxpow(self) -> bool:
        """``True`` if this block carries a merged-mining AuxPoW proof."""
        return self.auxpow is not None

    @property
    def coinbase(self) -> Transaction:
        """Return the coinbase transaction."""
        if not self.transactions:
            raise BlockError("block has no transactions")
        return self.transactions[0]

    def txids(self) -> list[bytes]:
        """Return the ids of all transactions, in order."""
        return [tx.txid() for tx in self.transactions]

    def computed_merkle_root(self) -> bytes:
        """Recompute the Merkle root from the block's transactions."""
        return merkle_root(self.txids())

    def with_header(self, header: BlockHeader) -> Block:
        """Return a copy of the block carrying a different header."""
        return replace(self, header=header)

    def with_auxpow(self, auxpow: object) -> Block:
        """Return a copy of the block carrying an AuxPoW proof."""
        return replace(self, auxpow=auxpow)

    # ---------------------------------------------------------- serialisation

    def serialize(self) -> bytes:
        """Return the canonical encoding of the whole block.

        Layout::

            [ScarletCoin header  (80 bytes)]
            [transaction count  (varint)]
            [transactions …]
            [AuxPoW marker]         0x00 = no AuxPoW, 0x01 = AuxPoW follows
            [AuxPoW payload]        only present when marker is 0x01
        """
        writer = Writer()
        writer.raw(self.header.serialize())
        writer.varint(len(self.transactions))
        for tx in self.transactions:
            writer.raw(tx.serialize())
        if self.auxpow is not None:
            auxpow: AuxPoW = self.auxpow  # type: ignore[no-redef]
            writer.uint8(0x01)
            aux_data = auxpow.serialize()
            writer.varbytes(aux_data)
        else:
            writer.uint8(0x00)
        return writer.getvalue()

    @classmethod
    def deserialize(cls, data: bytes) -> Block:
        """Parse a block from its wire format."""
        reader = Reader(data)
        header = BlockHeader.read(reader)
        count = reader.varint()
        if count == 0:
            raise SerializationError("block has no transactions")
        if count > reader.remaining:
            raise SerializationError("transaction count is larger than the remaining data")
        transactions = tuple(Transaction.read(reader) for _ in range(count))
        # Check for AuxPoW marker if any bytes remain
        auxpow = None
        if reader.remaining > 0:
            marker = reader.uint8()
            if marker == 0x01:
                from scarletcoin.core.auxpow import AuxPoW

                aux_data = reader.varbytes()
                auxpow = AuxPoW.deserialize(aux_data)
            elif marker != 0x00:
                raise SerializationError(f"unknown AuxPoW marker: {marker:#04x}")
        reader.expect_end()
        return cls(header, transactions, auxpow=auxpow)

    def size(self) -> int:
        """Serialised size in bytes."""
        return len(self.serialize())

    def check_sanity(
        self,
        *,
        pow_limit: int,
        max_block_size: int,
        min_output_value: int = 0,
        is_auxpow_active: bool = False,
    ) -> None:
        """Validate the block without consulting the chain.

        Checks proof of work (native or AuxPoW-aware), size, the Merkle root, that
        exactly one coinbase is present and in first position, and that every
        transaction is well formed.

        ``is_auxpow_active`` allows blocks carrying a valid AuxPoW to bypass the
        native header PoW check; the actual AuxPoW validation is done later by the
        chain so that it has the block hash and target already computed.

        Raises:
            BlockError: if any of those checks fails.
        """
        if not self.transactions:
            raise BlockError("block has no transactions")
        size = self.size()
        if size > max_block_size:
            raise BlockError(f"block is too large: {size} > {max_block_size} bytes")
        # For AuxPoW blocks the native header PoW is skipped — the parent Bitcoin
        # PoW is verified later by the chain with the full context.
        if (not is_auxpow_active or not self.has_auxpow) and not self.header.check_proof_of_work(
            pow_limit=pow_limit
        ):
            raise BlockError("block hash does not meet its proof-of-work target")
        if not self.transactions[0].is_coinbase:
            raise BlockError("first transaction in a block must be the coinbase")
        for tx in self.transactions[1:]:
            if tx.is_coinbase:
                raise BlockError("block contains more than one coinbase transaction")
        for tx in self.transactions:
            tx.check_sanity(min_output_value=min_output_value)
        txids = self.txids()
        if len(set(txids)) != len(txids):
            raise BlockError("block contains duplicate transactions")
        if merkle_root(txids) != self.header.merkle_root:
            raise BlockError("Merkle root does not match the block's transactions")

    def to_dict(self, address_version: int, *, verbose: bool = False) -> dict:
        """Return a JSON-friendly representation."""
        data = self.header.to_dict()
        data["size"] = self.size()
        data["transaction_count"] = len(self.transactions)
        data["proof_type"] = "auxpow" if self.has_auxpow else "native"
        if verbose:
            data["transactions"] = [tx.to_dict(address_version) for tx in self.transactions]
        else:
            data["transactions"] = [tx.txid_hex() for tx in self.transactions]
        if self.has_auxpow:
            auxpow: AuxPoW = self.auxpow  # type: ignore[no-redef]
            data["auxpow"] = auxpow.to_dict()
        return data
