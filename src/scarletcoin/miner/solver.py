"""The proof-of-work search loop.

Mining is just this: hash the 80-byte block header, interpret the digest as a
little-endian integer, and keep changing the last four bytes (the nonce) until
the value drops below the target.  Everything else in the miner exists to feed
this loop with fresh headers.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from scarletcoin.core.block import BLOCK_HEADER_SIZE, Block, BlockHeader
from scarletcoin.core.pow import bits_to_target

__all__ = ["NONCE_LIMIT", "ScanResult", "compile_native", "scan_nonces", "solve_block"]

logger = logging.getLogger(__name__)

#: The nonce field is a uint32, so this many values exist.
NONCE_LIMIT = 1 << 32

_NONCE_OFFSET = BLOCK_HEADER_SIZE - 4
_SOURCE = "miner/_scan_nonces.c"


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

    native = _native_scan()
    began = time.perf_counter()
    if native is not None:
        nonce = native(header, target, start, count)
        elapsed = time.perf_counter() - began
        if nonce >= 0:
            return ScanResult(nonce, nonce - start + 1, elapsed)
        return ScanResult(None, count, elapsed)

    # The first 64 bytes of the header never change while we roll the nonce, so
    # the first SHA-256 compression can be computed once and copied.
    midstate = hashlib.sha256(header[:64])
    tail = header[64:_NONCE_OFFSET]
    sha256 = hashlib.sha256
    from_bytes = int.from_bytes

    stop = min(start + count, NONCE_LIMIT)
    nonce = start
    while nonce < stop:
        hasher = midstate.copy()
        hasher.update(tail + nonce.to_bytes(4, "little"))
        if from_bytes(sha256(hasher.digest()).digest(), "little") <= target:
            return ScanResult(nonce, nonce - start + 1, time.perf_counter() - began)
        nonce += 1
    return ScanResult(None, stop - start, time.perf_counter() - began)


# ---------------------------------------------------------------- native scan

_native_func = None
_native_tried = False


def _native_scan() -> Callable[[bytes, int, int, int], int] | None:
    """Return the compiled nonce scanner, or ``None`` if it is unavailable.

    Only *loads* a previously-compiled library; never compiles on its own, so it
    is safe to call from worker processes.  The main process should call
    :func:`compile_native` once before spawning workers.
    """
    global _native_func, _native_tried
    if _native_tried:
        return _native_func
    _native_tried = True
    try:
        library = _cache_path()
        if not library.exists():
            _native_func = None
            return None
        scanner = ctypes.CDLL(str(library)).scarlet_scan_nonces
        scanner.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint]
        scanner.restype = ctypes.c_longlong
        _native_func = _make_native_caller(scanner)
    except Exception as exc:  # pragma: no cover - depends on the build host
        logger.debug("native mining backend unavailable: %s", exc)
        _native_func = None
    return _native_func


def compile_native() -> None:
    """Build the native nonce scanner if a C compiler is available.

    Must be called in the main process before any worker pool is created.  The
    compiled library is placed in a directory shared by all workers; subsequent
    calls to :func:`_native_scan` load it from there.
    """
    cache = _cache_path()
    if cache.exists():
        return
    source = resources.files("scarletcoin").joinpath(_SOURCE)
    temp_src = None
    temp_out = None
    try:
        temp_src = Path(tempfile.mkstemp(suffix=".c", dir=cache.parent)[1])
        temp_src.write_bytes(source.read_bytes())
        temp_out = cache.with_suffix(".tmp.so")
        subprocess.run(
            ["gcc", "-O3", "-fPIC", "-shared", "-o", str(temp_out), str(temp_src)],
            check=True,
            capture_output=True,
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        temp_out.replace(cache)
    except Exception:
        logger.debug("native mining backend compilation failed", exc_info=True)
    finally:
        if temp_src is not None:
            temp_src.unlink(missing_ok=True)
        if temp_out is not None:
            temp_out.unlink(missing_ok=True)


def _cache_path() -> Path:
    return Path(tempfile.gettempdir()) / "scarletcoin-native" / "_scan_nonces.so"


def _make_native_caller(scanner) -> Callable[[bytes, int, int, int], int]:
    def call(header: bytes, target: int, start: int, count: int) -> int:
        header_buf = ctypes.create_string_buffer(bytes(header), BLOCK_HEADER_SIZE)
        target_buf = ctypes.create_string_buffer(target.to_bytes(32, "little"), 32)
        return int(scanner(header_buf, target_buf, start, count))

    return call


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
