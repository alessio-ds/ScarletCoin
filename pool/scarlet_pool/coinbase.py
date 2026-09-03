"""Parent (Bitcoin) coinbase construction with ScarletCoin AuxPoW commitment.

The pool constructs a Bitcoin coinbase that:
1. Follows standard Bitcoin coinbase layout
2. Includes the ScarletCoin merged-mining commitment (fa be 6d 6d || aux_root || ...)
3. Leaves room for the ASIC's extranonce1/extranonce2

The coinbase is split into ``coinbase1`` (prefix) and ``coinbase2`` (suffix)
for the Stratum protocol, where ``extranonce1 + extranonce2`` are inserted
between them by the miner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from scarletcoin.core.serialize import Writer
from scarletcoin.crypto.hashing import hash256

__all__ = ["CoinbaseBuilder", "ParentCoinbase", "compute_merkle_root"]

#: Standard coinbase input outpoint (null hash, max index).
_COINBASE_OUTPOINT_TXID: Final[bytes] = b"\x00" * 32
_COINBASE_OUTPOINT_INDEX: Final[int] = 0xFFFFFFFF
#: Coinbase input sequence number.
_COINBASE_SEQUENCE: Final[int] = 0xFFFFFFFF

#: Maximum coinbase script size Bitcoin Core accepts by default.
MAX_COINBASE_SCRIPTSIG = 100


@dataclass(frozen=True)
class ParentCoinbase:
    """A pre-constructed parent (Bitcoin) coinbase split for Stratum."""

    coinbase1: str
    """Hex-encoded prefix: version, inputs, script up to extranonce1."""
    coinbase2: str
    """Hex-encoded suffix: rest of script after extranonce2, outputs, locktime."""
    coinbase_value: int
    """Total output value in satoshis (subsidy + fees)."""
    extranonce1: str
    """Hex-encoded extranonce1 assigned to this connection."""
    extranonce2_size: int
    """Number of bytes the miner may append for extranonce2."""


def compute_merkle_root(coinbase_hash: bytes, txids: list[bytes]) -> bytes:
    """Compute the Merkle root from *coinbase_hash* and *txids*.

    Uses the same duplication rule as Bitcoin/ScarletCoin.
    """
    from scarletcoin.core.block import merkle_root

    return merkle_root([coinbase_hash, *txids])


class CoinbaseBuilder:
    """Builds parent Bitcoin coinbases containing ScarletCoin AuxPoW commitments.

    The builder is initialised once with pool-wide settings; each call to
    :meth:`build` produces a fresh coinbase for a new job.
    """

    def __init__(
        self,
        *,
        pool_tag: bytes = b"/scarlet-pool/",
        extranonce1_size: int = 4,
        extranonce2_size: int = 4,
    ) -> None:
        self.pool_tag = pool_tag[:80]  # keep scriptSig small
        self.extranonce1_size = max(1, min(extranonce1_size, 16))
        self.extranonce2_size = max(1, min(extranonce2_size, 16))

    def build(
        self,
        *,
        coinbase_value: int,
        block_height: int,
        payout_script: bytes,
        aux_commitment: bytes,
        extranonce1: bytes,
    ) -> ParentCoinbase:
        """Build a parent coinbase carrying an AuxPoW commitment.

        Args:
            coinbase_value: Total output value in satoshis (subsidy + fees).
            block_height: Bitcoin block height (BIP-34).
            payout_script: The pool's payout output script.
            aux_commitment: The serialised AuxPoW commitment bytes.
            extranonce1: Unique bytes for this connection (4-16 bytes).

        Returns:
            A :class:`ParentCoinbase` ready for Stratum job assembly.
        """
        # -- Build the coinbase input script ----------------------------------
        # BIP-34 height prefix
        height_prefix = self._bip34_height(block_height)
        # Script:  height_prefix || pool_tag || extranonce1 || <extranonce2> || aux_commitment
        script_prefix = height_prefix + self.pool_tag
        script_suffix = aux_commitment

        # Build coinbase1 = everything up to (and including) extranonce1
        w = Writer()
        w.uint32(1)  # version
        w.varint(1)  # input count
        w.hash32(_COINBASE_OUTPOINT_TXID)
        w.uint32(_COINBASE_OUTPOINT_INDEX)
        # scriptSig: placeholder length that covers prefix + extranonce1 + extranonce2 + suffix
        total_script_len = (
            len(script_prefix) + self.extranonce1_size + self.extranonce2_size + len(script_suffix)
        )
        w.varint(total_script_len)
        w.raw(script_prefix)
        w.raw(extranonce1)
        coinbase1 = w.getvalue().hex()

        # Build coinbase2 = everything after extranonce2
        w2 = Writer()
        w2.raw(script_suffix)  # the AuxPoW commitment (goes after extranonce2)
        w2.uint32(_COINBASE_SEQUENCE)
        # Outputs
        w2.varint(1)  # one output
        w2.raw(payout_script)
        w2.uint32(0)  # lock_time
        coinbase2 = w2.getvalue().hex()

        return ParentCoinbase(
            coinbase1=coinbase1,
            coinbase2=coinbase2,
            coinbase_value=coinbase_value,
            extranonce1=extranonce1.hex(),
            extranonce2_size=self.extranonce2_size,
        )

    @staticmethod
    def _bip34_height(height: int) -> bytes:
        """BIP-34 height prefix for the coinbase script."""
        if height <= 16:
            return bytes([0x51 + height])
        if height <= 127:
            return bytes([1, height])
        if height <= 0x7FFF:
            return bytes([2]) + height.to_bytes(2, "little")
        return bytes([3]) + height.to_bytes(4, "little")

    @staticmethod
    def reconstruct_header(
        coinbase1_hex: str,
        extranonce2_hex: str,
        coinbase2_hex: str,
        merkle_branches: list[str],
        prev_hash_hex: str,
        version: int,
        nbits: int,
        ntime: int,
        nonce: int,
    ) -> bytes:
        """Reconstruct the 80-byte parent header from Stratum submission data.

        This is the exact header the ASIC hashed.  We rebuild the full coinbase,
        compute its txid, calculate the Merkle root, and assemble the header.

        Returns:
            The 80-byte little-endian serialised block header (internal order).
        """
        # Reconstruct full coinbase
        parts = [bytes.fromhex(h) for h in (coinbase1_hex, extranonce2_hex, coinbase2_hex)]
        coinbase = b"".join(parts)
        coinbase_hash = hash256(coinbase)

        # Compute Merkle root
        branches = [bytes.fromhex(b)[::-1] for b in merkle_branches]  # display → internal
        root = coinbase_hash
        for sibling in branches:
            root = hash256(root + sibling)

        # Build header
        prev_hash = bytes.fromhex(prev_hash_hex)[::-1]  # display → internal
        w = Writer()
        w.uint32(version)
        w.hash32(prev_hash)
        w.hash32(root)
        w.uint32(ntime)
        w.uint32(nbits)
        w.uint32(nonce)
        return w.getvalue()
