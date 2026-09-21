"""Parent coinbase construction carrying a ScarletCoin AuxPoW commitment.

The "parent" coinbase in an AuxPoW proof is represented by the consensus code
as an ordinary :class:`~scarletcoin.core.transaction.Transaction` whose
``coinbase_data`` field holds the merged-mining commitment.  That is the format
:class:`~scarletcoin.core.auxpow.AuxPoW` serialises and validates, so the pool
must build the same thing — a real ``bitcoind`` coinbase would not parse.

The coinbase body looks like this::

    uint32   version
    varint   1                      (one input)
    bytes32  null prevout hash
    uint32   0xFFFFFFFF             (null prevout index)
    uint32   0xFFFFFFFF             (final sequence)
    varint   1                      (one output)
    uint8    output type            (0 = P2PKH)
    uint64   value
    bytes20  payout hash
    uint32   lock time
    varbytes coinbase_data          = height (uint32 LE) || extranonces || commitment

Stratum splits it so the miner can vary the two extranonces::

    body = coinbase1 || extranonce1 || extranonce2 || coinbase2

The transaction id — and therefore the Merkle leaf — is ``hash256(body)``, so
the pool and the miner hash exactly the bytes between the two halves.

All 32-byte hashes are in **internal** (little-endian) byte order, which is the
order they occupy in a serialised header and the order Stratum puts on the
wire.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from scarletcoin.core.serialize import Writer
from scarletcoin.core.transaction import (
    MAX_COINBASE_DATA,
    OUTPUT_P2PKH,
    SEQUENCE_FINAL,
    Transaction,
)
from scarletcoin.crypto.hashing import PUBKEY_HASH_LENGTH, hash256

__all__ = [
    "CoinbaseBuilder",
    "ParentCoinbase",
    "coinbase_merkle_branch",
    "compute_merkle_root",
    "parse_coinbase_body",
]

#: The null outpoint every coinbase input spends.
_NULL_HASH: Final[bytes] = b"\x00" * 32
_NULL_INDEX: Final[int] = 0xFFFFFFFF

#: Default payout hash for the **parent** coinbase's single output.
#:
#: When the parent chain is simulated this output is never spendable and its
#: value is irrelevant — only the commitment inside ``coinbase_data`` matters.
#: It is a placeholder, not a burn address anyone should send to.
DEFAULT_PARENT_PAYOUT_HASH: Final[bytes] = b"\x00" * PUBKEY_HASH_LENGTH


@dataclass(frozen=True)
class ParentCoinbase:
    """A parent coinbase body split for Stratum."""

    coinbase1: str
    """Hex prefix, ending just before ``extranonce1``."""
    coinbase2: str
    """Hex suffix, starting just after ``extranonce2`` (the commitment)."""
    coinbase_value: int
    """Output value in satoshis."""
    extranonce1_size: int
    """Bytes ``mining.subscribe`` hands the miner."""
    extranonce2_size: int
    """Bytes the miner may choose for ``extranonce2``."""
    coinbase_data_size: int
    """Total length of the ``coinbase_data`` field this layout produces."""


def compute_merkle_root(coinbase_hash: bytes, txids: Sequence[bytes]) -> bytes:
    """Compute a Merkle root from *coinbase_hash* and *txids* (internal order)."""
    from scarletcoin.core.block import merkle_root

    return merkle_root([coinbase_hash, *txids])


def coinbase_merkle_branch(txids: Sequence[bytes]) -> list[bytes]:
    """Return the Merkle branch proving the coinbase (leaf 0) is in the tree.

    The coinbase is always the leftmost leaf, so every sibling on the path to
    the root is on the right-hand side of the tree and is built purely from
    *txids*.  The branch can therefore be computed before the coinbase exists,
    which is what a pool needs: the coinbase hash depends on the miner's
    extranonces.

    Args:
        txids: The non-coinbase transaction ids, in internal byte order.

    Returns:
        The sibling hashes, nearest first, in internal byte order.
    """
    # A placeholder stands in for the coinbase: it only ever feeds index 0 of
    # each level, and index 0 is never selected as a sibling.
    level: list[bytes] = [b"\x00" * 32, *txids]
    branches: list[bytes] = []
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        branches.append(level[1])
        level = [hash256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return branches


def parse_coinbase_body(body: bytes) -> Transaction:
    """Parse a coinbase *body* into a :class:`Transaction`.

    ``Transaction.serialize_body`` omits the per-input witness counts that the
    wire format carries, so a trailing zero byte is appended to make the body a
    complete, parseable transaction.
    """
    return Transaction.deserialize(body + b"\x00")


class CoinbaseBuilder:
    """Builds parent coinbases containing ScarletCoin AuxPoW commitments."""

    def __init__(
        self,
        *,
        extranonce1_size: int = 4,
        extranonce2_size: int = 4,
    ) -> None:
        self.extranonce1_size = max(1, min(extranonce1_size, 16))
        self.extranonce2_size = max(1, min(extranonce2_size, 16))

    def build(
        self,
        *,
        coinbase_value: int,
        block_height: int,
        payout_hash: bytes,
        aux_commitment: bytes,
    ) -> ParentCoinbase:
        """Build a parent coinbase carrying an AuxPoW commitment.

        Args:
            coinbase_value: Output value in satoshis.
            block_height: Height committed to at the start of ``coinbase_data``.
            payout_hash: The 20-byte payout hash for the single output.
            aux_commitment: The serialised AuxPoW commitment bytes.

        Returns:
            A :class:`ParentCoinbase` ready for Stratum job assembly.
        """
        if not 0 <= block_height <= 0xFFFFFFFF:
            raise ValueError(f"block height out of range: {block_height}")
        if len(payout_hash) != PUBKEY_HASH_LENGTH:
            raise ValueError(
                f"payout hash must be {PUBKEY_HASH_LENGTH} bytes, got {len(payout_hash)}"
            )

        data_size = 4 + self.extranonce1_size + self.extranonce2_size + len(aux_commitment)
        if data_size > MAX_COINBASE_DATA:
            raise ValueError(
                f"coinbase_data would be {data_size} bytes, over the {MAX_COINBASE_DATA}-byte limit"
            )

        w = Writer()
        w.uint32(1)  # version
        w.varint(1)  # input count
        w.hash32(_NULL_HASH)  # prevout hash
        w.uint32(_NULL_INDEX)  # prevout index
        w.uint32(SEQUENCE_FINAL)  # sequence
        w.varint(1)  # output count
        w.uint8(OUTPUT_P2PKH)  # output type
        w.uint64(coinbase_value)  # output value
        w.raw(payout_hash)  # output payload
        w.uint32(0)  # lock time
        w.varint(data_size)  # coinbase_data length
        w.raw(block_height.to_bytes(4, "little"))  # height prefix
        coinbase1 = w.getvalue().hex()

        # Everything after the two extranonces: the commitment.
        coinbase2 = aux_commitment.hex()

        return ParentCoinbase(
            coinbase1=coinbase1,
            coinbase2=coinbase2,
            coinbase_value=coinbase_value,
            extranonce1_size=self.extranonce1_size,
            extranonce2_size=self.extranonce2_size,
            coinbase_data_size=data_size,
        )

    @staticmethod
    def stratum_prevhash(prev_hash_internal: str) -> str:
        """Return ``prev_hash_internal`` as Stratum sends it.

        The header stores the previous block hash in internal byte order, but
        Stratum puts it on the wire with **every 32-bit word byte-swapped**.
        A miner copies the field into its header and the swap cancels out, so
        sending it verbatim makes the miner hash a different header than the
        pool rebuilds - which silently reduces share checking to "did the
        pool's own header happen to beat the target", and on a hard target
        rejects every share a real miner ever submits.
        """
        raw = bytes.fromhex(prev_hash_internal)
        swapped = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
        return swapped.hex()

    @staticmethod
    def reconstruct_header(
        coinbase1_hex: str,
        extranonce1_hex: str,
        extranonce2_hex: str,
        coinbase2_hex: str,
        merkle_branches_hex: Sequence[str],
        prev_hash_hex: str,
        version: int,
        nbits: int,
        ntime: int,
        nonce: int,
    ) -> bytes:
        """Reconstruct the 80-byte parent header a miner says it hashed.

        Everything is in internal byte order, exactly as it went out in
        ``mining.notify``: the miner assembled the coinbase, hashed it, folded
        in the branch and filled the header.

        Returns:
            The 80-byte serialised parent block header (internal order).
        """
        parts = (coinbase1_hex, extranonce1_hex, extranonce2_hex, coinbase2_hex)
        coinbase = b"".join(bytes.fromhex(part) for part in parts)

        root = hash256(coinbase)
        for sibling_hex in merkle_branches_hex:
            root = hash256(root + bytes.fromhex(sibling_hex))

        w = Writer()
        w.uint32(version)
        w.hash32(bytes.fromhex(prev_hash_hex))
        w.hash32(root)
        w.uint32(ntime)
        w.uint32(nbits)
        w.uint32(nonce)
        return w.getvalue()
