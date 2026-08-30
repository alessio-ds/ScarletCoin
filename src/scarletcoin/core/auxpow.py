"""AuxPoW (merged mining) data structures and validation.

Implements Namecoin-style AuxPoW so that a Bitcoin SHA-256d miner can produce
proof-of-work that is valid for both Bitcoin and ScarletCoin without doing extra
hashing.  The ScarletCoin block hash is committed into the Bitcoin coinbase via
a merged-mining marker, and the parent Bitcoin block hash must satisfy the
ScarletCoin target.

Reference: :mod:`scarletcoin.core.auxpow`
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from scarletcoin.core.serialize import Reader, SerializationError, Writer
from scarletcoin.core.transaction import MAX_COINBASE_DATA, Transaction, TransactionError
from scarletcoin.crypto.hashing import hash256

__all__ = [
    "MAX_MERKLE_BRANCH_DEPTH",
    "MERGED_MINING_HEADER",
    "AuxPoW",
    "AuxPoWCommitment",
    "AuxPoWError",
    "ParentBlockHeader",
    "build_auxpow_commitment",
    "check_merkle_branch",
    "get_expected_index",
    "parse_auxpow_commitment",
    "validate_auxpow",
]

#: Magic marker bytes that signal a Namecoin-style merged-mining commitment.
MERGED_MINING_HEADER: Final[bytes] = bytes([0xFA, 0xBE, 0x6D, 0x6D])

#: Maximum depth of either Merkle branch (coinbase or auxiliary).
MAX_MERKLE_BRANCH_DEPTH: Final[int] = 30

#: Maximum serialised size of an AuxPoW structure.
_MAX_AUXPOW_SERIALIZED = 4 * 1024 * 1024  # 4 MiB


class AuxPoWError(ValueError):
    """Raised when an AuxPoW proof is structurally or consensus-invalid."""


# ------------------------------------------------------------------ Merkle helper


def check_merkle_branch(
    leaf: bytes,
    branch: tuple[bytes, ...],
    index: int,
) -> bytes:
    """Compute the Merkle root from ``leaf``, ``branch``, and ``index``.

    Semantics (Bitcoin standard, used by Namecoin AuxPoW):

    .. code-block:: text

        for sibling in branch:
            if index & 1:
                current = hash256(sibling + current)
            else:
                current = hash256(current + sibling)
            index >>= 1

    Args:
        leaf: The 32-byte hash whose membership is being proven.
        branch: The sibling hashes along the path from leaf to root (excluding the
            root itself), in order from the leaf upward.
        index: The 0-based position of ``leaf`` in the tree.

    Returns:
        The computed 32-byte Merkle root.

    Raises:
        AuxPoWError: if any hash is not 32 bytes or the branch is too deep.
    """
    if len(leaf) != 32:
        raise AuxPoWError("Merkle leaf must be 32 bytes")
    if len(branch) > MAX_MERKLE_BRANCH_DEPTH:
        raise AuxPoWError(
            f"Merkle branch is {len(branch)} levels deep, the limit is {MAX_MERKLE_BRANCH_DEPTH}"
        )
    current = leaf
    for sibling in branch:
        if len(sibling) != 32:
            raise AuxPoWError("Merkle branch hash must be 32 bytes")
        current = hash256(sibling + current) if index & 1 else hash256(current + sibling)
        index >>= 1
    return current


# ---------------------------------------------------------------- parent header


@dataclass(frozen=True, slots=True)
class ParentBlockHeader:
    """An 80-byte Bitcoin-style block header from the parent chain.

    This is a separate type from ScarletCoin's :class:`~scarletcoin.core.block.BlockHeader`
    to avoid accidental consensus coupling. Serialisation is little-endian, exactly
    as Bitcoin uses.
    """

    version: int
    prev_hash: bytes
    merkle_root: bytes
    timestamp: int
    bits: int
    nonce: int

    def __post_init__(self) -> None:
        if len(self.prev_hash) != 32:
            raise AuxPoWError("parent header previous hash must be 32 bytes")
        if len(self.merkle_root) != 32:
            raise AuxPoWError("parent header Merkle root must be 32 bytes")
        for name in ("version", "timestamp", "bits", "nonce"):
            value = getattr(self, name)
            if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
                raise AuxPoWError(f"parent header field {name} must be a uint32, got {value!r}")

    def serialize(self) -> bytes:
        """Return the canonical 80-byte little-endian encoding."""
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
    def deserialize(cls, data: bytes) -> ParentBlockHeader:
        """Parse an 80-byte parent header."""
        if len(data) != 80:
            raise SerializationError(f"parent block header must be 80 bytes, got {len(data)}")
        return cls.read(Reader(data))

    @classmethod
    def read(cls, reader: Reader) -> ParentBlockHeader:
        """Parse one parent header from ``reader``."""
        return cls(
            version=reader.uint32(),
            prev_hash=reader.hash32(),
            merkle_root=reader.hash32(),
            timestamp=reader.uint32(),
            bits=reader.uint32(),
            nonce=reader.uint32(),
        )

    def hash(self) -> bytes:
        """Return the double SHA-256 of the 80-byte header (internal byte order)."""
        return hash256(self.serialize())

    def hash_hex(self) -> str:
        """Return the hash in display (big-endian) order."""
        return self.hash()[::-1].hex()

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


# --------------------------------------------------------------- commitment


@dataclass(frozen=True, slots=True)
class AuxPoWCommitment:
    """Parsed AuxPoW commitment found in a parent coinbase's ``coinbase_data``."""

    aux_root: bytes
    """The auxiliary-chain Merkle root (32 bytes)."""
    tree_size: int
    """Number of entries in the auxiliary Merkle tree."""
    nonce: int
    """The nonce used to derive the deterministic chain index."""

    def __post_init__(self) -> None:
        if len(self.aux_root) != 32:
            raise AuxPoWError("aux root must be 32 bytes")
        if not 1 <= self.tree_size <= 0xFFFFFFFF:
            raise AuxPoWError(f"aux tree size out of range: {self.tree_size}")
        if not 0 <= self.nonce <= 0xFFFFFFFF:
            raise AuxPoWError(f"commitment nonce out of range: {self.nonce}")

    @property
    def aux_tree_height(self) -> int:
        """Return ``ceil(log2(tree_size))`` — the height of the tree."""
        # tree_size is a power of two for a balanced tree.
        size = self.tree_size
        height = 0
        while size > 1:
            size >>= 1
            height += 1
        return height


def build_auxpow_commitment(
    aux_root: bytes,
    tree_size: int,
    nonce: int,
) -> bytes:
    """Build the serialised merged-mining commitment to embed in a coinbase.

    Format (Namecoin-style):

    .. code-block:: text

        fa be 6d 6d || aux_root (32) || tree_size (u32 LE) || nonce (u32 LE)
    """
    if len(aux_root) != 32:
        raise AuxPoWError("aux root must be 32 bytes")
    if not 1 <= tree_size <= 0xFFFFFFFF:
        raise AuxPoWError(f"aux tree size out of range: {tree_size}")
    if not 0 <= nonce <= 0xFFFFFFFF:
        raise AuxPoWError(f"commitment nonce out of range: {nonce}")
    return (
        MERGED_MINING_HEADER
        + aux_root
        + tree_size.to_bytes(4, "little")
        + nonce.to_bytes(4, "little")
    )


def parse_auxpow_commitment(coinbase_data: bytes) -> AuxPoWCommitment | None:
    """Search ``coinbase_data`` for a merged-mining commitment.

    The commitment is located by scanning for the merged-mining magic marker.
    If multiple markers are found the parse fails (ambiguity is a consensus
    rejection).

    Args:
        coinbase_data: The raw ``coinbase_data`` field of a coinbase transaction.

    Returns:
        An :class:`AuxPoWCommitment` if exactly one valid commitment is found,
        or ``None`` if none is found.

    Raises:
        AuxPoWError: if multiple markers or malformed commitments are found.
    """
    if not coinbase_data:
        return None

    # Find all occurrences of the merged-mining marker.
    results: list[AuxPoWCommitment] = []
    offset = 0
    while True:
        idx = coinbase_data.find(MERGED_MINING_HEADER, offset)
        if idx == -1:
            break
        offset = idx + len(MERGED_MINING_HEADER)
        remaining = len(coinbase_data) - offset
        if remaining < 40:  # 32 (aux_root) + 4 (tree_size) + 4 (nonce)
            # Marker found but not enough data after it; skip
            continue
        aux_root = coinbase_data[offset : offset + 32]
        tree_size = int.from_bytes(coinbase_data[offset + 32 : offset + 36], "little")
        nonce = int.from_bytes(coinbase_data[offset + 36 : offset + 40], "little")
        try:
            commitment = AuxPoWCommitment(aux_root, tree_size, nonce)
        except AuxPoWError:
            # Malformed commitment, search for another
            continue
        results.append(commitment)

    if len(results) > 1:
        raise AuxPoWError(
            f"found {len(results)} AuxPoW commitments in the parent coinbase;"
            " exactly one is required"
        )
    return results[0] if results else None


# ------------------------------------------------------- deterministic index


def get_expected_index(nonce: int, chain_id: int, aux_tree_height: int) -> int:
    """Compute the deterministic chain index for a given nonce and chain ID.

    This is the Namecoin-style calculation: the index is derived from
    ``nonce``, ``chain_id``, and the auxiliary Merkle tree height so that a
    miner cannot place the ScarletCoin commitment at an arbitrary slot.

    The formula (from Namecoin's ``getExpectedIndex``):

    .. code-block:: text

        rand = nonce
        rand = rand * 1103515245 + 12345
        rand += chain_id
        rand = rand * 1103515245 + 12345
        index = rand % (1 << aux_tree_height)

    For a single-child tree (height 0) the result is always 0.

    Args:
        nonce: The 32-bit nonce from the AuxPoW commitment.
        chain_id: The chain's unique AuxPoW identifier.
        aux_tree_height: The height of the auxiliary Merkle tree.

    Returns:
        The expected slot index (0 <= index < 2**aux_tree_height).
    """
    rand = nonce & 0xFFFFFFFF
    rand = (rand * 1103515245 + 12345) & 0xFFFFFFFFFFFFFFFF
    rand = (rand + chain_id) & 0xFFFFFFFFFFFFFFFF
    rand = (rand * 1103515245 + 12345) & 0xFFFFFFFFFFFFFFFF
    if aux_tree_height <= 0:
        return 0
    return int(rand) % (1 << aux_tree_height)


# ------------------------------------------------------------------ AuxPoW data


@dataclass(frozen=True, slots=True)
class AuxPoW:
    """A complete merged-mining proof.

    Contains all the data needed to validate that a parent Bitcoin block's
    proof of work also satisfies ScarletCoin's target.  Stored alongside (but
    separate from) the ScarletCoin block so that the ScarletCoin block hash
    is not affected by the AuxPoW payload.
    """

    coinbase_tx: Transaction
    """The parent Bitcoin coinbase transaction that carries the commitment."""
    coinbase_merkle_branch: tuple[bytes, ...]
    """Sibling hashes proving the coinbase is part of the parent block."""
    coinbase_index: int
    """The coinbase's 0-based position in the parent block's Merkle tree."""
    aux_merkle_branch: tuple[bytes, ...]
    """Sibling hashes proving the ScarletCoin block hash is part of the
    auxiliary Merkle tree."""
    aux_chain_index: int
    """The 0-based index of ScarletCoin in the auxiliary Merkle tree."""
    parent_header: ParentBlockHeader
    """The 80-byte Bitcoin parent block header."""

    # -- convenience ----------------------------------------------------------

    @property
    def coinbase_hash(self) -> bytes:
        """The transaction id of the parent coinbase."""
        return self.coinbase_tx.txid()

    @property
    def parent_hash(self) -> bytes:
        """The double SHA-256d hash of the parent block header."""
        return self.parent_header.hash()

    # -- serialisation --------------------------------------------------------

    def serialize(self) -> bytes:
        """Return the canonical deterministic serialisation.

        Binary layout:

        .. code-block:: text

            varint  coinbase_merkle_branch_count
            [32-byte branch hash] * count
            uint32  coinbase_index
            varint  aux_merkle_branch_count
            [32-byte branch hash] * count
            uint32  aux_chain_index
            serialized coinbase transaction (varbytes)
            serialized parent header (80 bytes raw)
        """
        writer = Writer()
        # Coinbase Merkle branch
        writer.varint(len(self.coinbase_merkle_branch))
        for sibling in self.coinbase_merkle_branch:
            writer.hash32(sibling)
        writer.uint32(self.coinbase_index)
        # Aux Merkle branch
        writer.varint(len(self.aux_merkle_branch))
        for sibling in self.aux_merkle_branch:
            writer.hash32(sibling)
        writer.uint32(self.aux_chain_index)
        # Coinbase transaction
        writer.varbytes(self.coinbase_tx.serialize())
        # Parent header (80 bytes)
        writer.raw(self.parent_header.serialize())
        return writer.getvalue()

    @classmethod
    def deserialize(cls, data: bytes) -> AuxPoW:
        """Parse an AuxPoW from its wire format."""
        reader = Reader(data)
        result = cls.read(reader)
        reader.expect_end()
        return result

    @classmethod
    def read(cls, reader: Reader) -> AuxPoW:
        """Parse one AuxPoW from ``reader``."""
        # Coinbase Merkle branch
        cb_branch_count = reader.varint()
        if cb_branch_count > MAX_MERKLE_BRANCH_DEPTH:
            raise SerializationError(
                f"coinbase Merkle branch too deep: {cb_branch_count} > {MAX_MERKLE_BRANCH_DEPTH}"
            )
        cb_branch = tuple(reader.hash32() for _ in range(cb_branch_count))
        coinbase_index = reader.uint32()

        # Aux Merkle branch
        aux_branch_count = reader.varint()
        if aux_branch_count > MAX_MERKLE_BRANCH_DEPTH:
            raise SerializationError(
                f"aux Merkle branch too deep: {aux_branch_count} > {MAX_MERKLE_BRANCH_DEPTH}"
            )
        aux_branch = tuple(reader.hash32() for _ in range(aux_branch_count))
        aux_chain_index = reader.uint32()

        # Coinbase transaction
        coinbase_bytes = reader.varbytes(max_length=MAX_COINBASE_DATA + 2000)
        try:
            coinbase_tx = Transaction.deserialize(coinbase_bytes)
        except (SerializationError, TransactionError) as exc:
            raise SerializationError(f"invalid parent coinbase transaction: {exc}") from exc

        # Parent header (80 bytes)
        parent_header_bytes = reader.raw(80)
        try:
            parent_header = ParentBlockHeader.deserialize(parent_header_bytes)
        except (SerializationError, AuxPoWError) as exc:
            raise SerializationError(f"invalid parent block header: {exc}") from exc

        return cls(
            coinbase_tx=coinbase_tx,
            coinbase_merkle_branch=cb_branch,
            coinbase_index=coinbase_index,
            aux_merkle_branch=aux_branch,
            aux_chain_index=aux_chain_index,
            parent_header=parent_header,
        )

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation."""
        return {
            "parent_block": self.parent_header.to_dict(),
            "coinbase_txid": self.coinbase_hash[::-1].hex(),
            "coinbase_merkle_branch": [h[::-1].hex() for h in self.coinbase_merkle_branch],
            "coinbase_index": self.coinbase_index,
            "aux_merkle_branch": [h[::-1].hex() for h in self.aux_merkle_branch],
            "aux_chain_index": self.aux_chain_index,
        }


# ----------------------------------------------------------------- validation


def validate_auxpow(
    auxpow: AuxPoW,
    aux_block_hash: bytes,
    aux_target: int,
    *,
    chain_id: int,
) -> None:
    """Validate an AuxPoW proof for a ScarletCoin block.

    This is the single authoritative function that decides whether a
    merged-mined proof is valid.  It follows the Namecoin validation model.

    Args:
        auxpow: The parsed AuxPoW structure.
        aux_block_hash: The 32-byte ScarletCoin block header hash (internal byte
            order) that must be proved.
        aux_target: The integer target the parent PoW must satisfy (from
            ScarletCoin's own ``bits``).
        chain_id: The configured AuxPoW chain ID of this network.

    Raises:
        AuxPoWError: with a precise reason if any validation step fails.
    """
    if chain_id == 0:
        raise AuxPoWError("AuxPoW is not configured for this network (chain_id=0)")

    # Step A — structural validation -----------------------------------------

    if len(auxpow.coinbase_merkle_branch) > MAX_MERKLE_BRANCH_DEPTH:
        raise AuxPoWError("coinbase Merkle branch exceeds maximum depth")
    if len(auxpow.aux_merkle_branch) > MAX_MERKLE_BRANCH_DEPTH:
        raise AuxPoWError("aux Merkle branch exceeds maximum depth")

    for sibling in auxpow.coinbase_merkle_branch:
        if len(sibling) != 32:
            raise AuxPoWError("coinbase Merkle branch hash is not 32 bytes")
    for sibling in auxpow.aux_merkle_branch:
        if len(sibling) != 32:
            raise AuxPoWError("aux Merkle branch hash is not 32 bytes")

    if not auxpow.coinbase_tx.is_coinbase:
        raise AuxPoWError("parent coinbase transaction is not a coinbase")

    # Step B — aux block hash is already provided by the caller --------------
    # (the ScarletCoin block hash is the value being proved)

    if len(aux_block_hash) != 32:
        raise AuxPoWError("aux block hash must be 32 bytes")

    # Step C — compute auxiliary Merkle root ----------------------------------

    aux_root = check_merkle_branch(
        aux_block_hash,
        auxpow.aux_merkle_branch,
        auxpow.aux_chain_index,
    )

    # Step D — locate the auxiliary root in parent coinbase -------------------

    commitment = parse_auxpow_commitment(auxpow.coinbase_tx.coinbase_data)
    if commitment is None:
        raise AuxPoWError("the parent coinbase does not contain a merged-mining commitment")

    if commitment.aux_root != aux_root:
        raise AuxPoWError(
            "the auxiliary Merkle root in the coinbase commitment does not match the computed root"
        )

    # Step E — validate deterministic auxiliary index -------------------------

    tree_size = commitment.tree_size
    if tree_size < 1:
        raise AuxPoWError("aux tree size must be at least 1")
    # Tree size must be a power of two
    if (tree_size & (tree_size - 1)) != 0:
        raise AuxPoWError(f"aux tree size {tree_size} is not a power of two")

    expected_index = get_expected_index(commitment.nonce, chain_id, commitment.aux_tree_height)
    if auxpow.aux_chain_index != expected_index:
        raise AuxPoWError(
            f"aux chain index is {auxpow.aux_chain_index},"
            f" expected {expected_index} for nonce {commitment.nonce}"
            f" and chain_id {chain_id}"
        )

    # Step F — prove parent coinbase belongs to parent block ------------------

    coinbase_root = check_merkle_branch(
        auxpow.coinbase_hash,
        auxpow.coinbase_merkle_branch,
        auxpow.coinbase_index,
    )
    if coinbase_root != auxpow.parent_header.merkle_root:
        raise AuxPoWError("the coinbase Merkle root does not match the parent block's Merkle root")

    # Step G — verify parent PoW against ScarletCoin target -------------------

    parent_hash_int = int.from_bytes(auxpow.parent_hash, "little")
    if parent_hash_int > aux_target:
        raise AuxPoWError("parent block hash does not satisfy the ScarletCoin target")

    # If we made it here, the proof is valid.
