"""Proof of work: compact targets, work accounting and difficulty retargeting."""

from __future__ import annotations

__all__ = [
    "MAX_TARGET_LIMIT",
    "bits_to_target",
    "block_work",
    "check_proof_of_work",
    "difficulty",
    "hash_to_int",
    "next_bits",
    "target_to_bits",
]

#: Highest possible target, i.e. difficulty 1 for the easiest possible network.
MAX_TARGET_LIMIT = 2**256 - 1


def bits_to_target(bits: int) -> int:
    """Expand the 32-bit compact representation of a target.

    Raises:
        ValueError: if ``bits`` is negative, overflows, or encodes a negative target.
    """
    if not 0 <= bits <= 0xFFFFFFFF:
        raise ValueError(f"compact target out of range: {bits:#x}")
    exponent = bits >> 24
    mantissa = bits & 0x007FFFFF
    if bits & 0x00800000:
        raise ValueError("negative compact target")
    if mantissa == 0:
        return 0
    if exponent <= 3:
        target = mantissa >> (8 * (3 - exponent))
    else:
        if exponent > 32:
            raise ValueError(f"compact target overflows 256 bits: {bits:#x}")
        target = mantissa << (8 * (exponent - 3))
    if target > MAX_TARGET_LIMIT:
        raise ValueError(f"compact target overflows 256 bits: {bits:#x}")
    return target


def target_to_bits(target: int) -> int:
    """Compress a target into its canonical 32-bit representation."""
    if target < 0:
        raise ValueError("target must not be negative")
    if target == 0:
        return 0
    raw = target.to_bytes((target.bit_length() + 7) // 8, "big")
    if raw[0] & 0x80:  # keep the sign bit clear
        raw = b"\x00" + raw
    exponent = len(raw)
    mantissa = int.from_bytes(raw[:3].ljust(3, b"\x00"), "big")
    return (exponent << 24) | mantissa


def hash_to_int(block_hash: bytes) -> int:
    """Interpret a block hash as the little-endian integer used for PoW checks."""
    if len(block_hash) != 32:
        raise ValueError("block hash must be 32 bytes")
    return int.from_bytes(block_hash, "little")


def check_proof_of_work(block_hash: bytes, bits: int, *, pow_limit: int) -> bool:
    """Return ``True`` if ``block_hash`` satisfies the target encoded in ``bits``.

    ``bits`` is also rejected when it is easier than the network's ``pow_limit``,
    which stops a peer from claiming work with a trivially easy target.
    """
    try:
        target = bits_to_target(bits)
    except ValueError:
        return False
    if target == 0 or target > pow_limit:
        return False
    return hash_to_int(block_hash) <= target


def block_work(bits: int) -> int:
    """Return the expected number of hashes needed to solve a block with ``bits``."""
    target = bits_to_target(bits)
    if target <= 0:
        return 0
    return (2**256) // (target + 1)


def difficulty(bits: int, *, pow_limit: int) -> float:
    """Return the difficulty of ``bits`` relative to the network's easiest target."""
    target = bits_to_target(bits)
    if target <= 0:
        return float("inf")
    return pow_limit / target


def next_bits(
    current_bits: int,
    actual_timespan: int,
    *,
    target_timespan: int,
    pow_limit: int,
    max_adjustment_factor: int = 4,
) -> int:
    """Compute the compact target for the next retargeting period.

    The observed ``actual_timespan`` is clamped to
    ``[target_timespan / factor, target_timespan * factor]`` so that a single
    period can never change the difficulty by more than ``max_adjustment_factor``.
    """
    if target_timespan <= 0:
        raise ValueError("target timespan must be positive")
    low = target_timespan // max_adjustment_factor
    high = target_timespan * max_adjustment_factor
    clamped = min(max(actual_timespan, low), high)
    target = bits_to_target(current_bits) * clamped // target_timespan
    return target_to_bits(min(target, pow_limit))
