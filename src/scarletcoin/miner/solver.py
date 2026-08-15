"""The proof-of-work search loop.

Mining is just this: hash the 80-byte block header, interpret the digest as a
little-endian integer, and keep changing the last four bytes (the nonce) until
the value drops below the target.  Everything else in the miner exists to feed
this loop with fresh headers.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from scarletcoin.core.block import BLOCK_HEADER_SIZE, Block, BlockHeader
from scarletcoin.core.pow import bits_to_target

__all__ = ["NONCE_LIMIT", "ScanResult", "scan_nonces", "solve_block"]

#: The nonce field is a uint32, so this many values exist.
NONCE_LIMIT = 1 << 32

_NONCE_OFFSET = BLOCK_HEADER_SIZE - 4


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Outcome of scanning a range of nonces."""

    nonce: int | None
    hashes: int
    seconds: float

    @property
    def found(self) -> bool:
        """``True`` if a nonce satisfying the target was found."""
        return self.nonce is not None

    @property
    def hash_rate(self) -> float:
        """Hashes per second achieved during the scan."""
        return self.hashes / self.seconds if self.seconds > 0 else 0.0


def scan_nonces(header: bytes, target: int, *, start: int = 0, count: int = 1 << 20) -> ScanResult:
    """Try ``count`` nonces starting at ``start`` on a serialised ``header``.

    Args:
        header: The 80-byte header; its last four bytes are overwritten.
        target: The largest acceptable hash value.
        start: First nonce to try.
        count: How many nonces to try.

    Returns:
        A :class:`ScanResult`; ``nonce`` is ``None`` when the range is exhausted.
    """
    if len(header) != BLOCK_HEADER_SIZE:
        raise ValueError(f"header must be {BLOCK_HEADER_SIZE} bytes, got {len(header)}")
    if start < 0 or count < 0:
        raise ValueError("nonce range must not be negative")

    # The first 64 bytes of the header never change while we roll the nonce, so
    # the first SHA-256 compression can be computed once and copied.
    midstate = hashlib.sha256(header[:64])
    tail = header[64:_NONCE_OFFSET]
    sha256 = hashlib.sha256
    from_bytes = int.from_bytes

    stop = min(start + count, NONCE_LIMIT)
    began = time.perf_counter()
    nonce = start
    while nonce < stop:
        hasher = midstate.copy()
        hasher.update(tail + nonce.to_bytes(4, "little"))
        if from_bytes(sha256(hasher.digest()).digest(), "little") <= target:
            return ScanResult(nonce, nonce - start + 1, time.perf_counter() - began)
        nonce += 1
    return ScanResult(None, stop - start, time.perf_counter() - began)


def solve_block(
    block: Block,
    *,
    start: int = 0,
    batch: int = 1 << 16,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int, float], None] | None = None,
) -> Block | None:
    """Search for a nonce that makes ``block`` valid.

    Args:
        block: The candidate block; its header is rolled, nothing else changes.
        start: First nonce to try.
        batch: Nonces per batch between stop checks and progress reports.
        should_stop: Called between batches; return ``True`` to abandon the search.
        on_progress: Called with ``(hashes, seconds)`` after every batch.

    Returns:
        The solved block, or ``None`` if the nonce space ran out or the search was
        stopped.
    """
    target = bits_to_target(block.header.bits)
    header = block.header.serialize()
    nonce = start
    while nonce < NONCE_LIMIT:
        if should_stop is not None and should_stop():
            return None
        result = scan_nonces(header, target, start=nonce, count=batch)
        if on_progress is not None:
            on_progress(result.hashes, result.seconds)
        if result.nonce is not None:
            return block.with_header(BlockHeader.deserialize(header).with_nonce(result.nonce))
        nonce += batch
    return None
