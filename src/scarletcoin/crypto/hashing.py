"""Hash helpers.

ScarletCoin uses SHA-256 as its only hash primitive so that every implementation
can be reproduced with nothing but the Python standard library:

``hash256``
    Double SHA-256, used for block hashes, transaction ids, Merkle trees and the
    Base58Check checksum.
``hash160``
    A 160-bit public-key digest, defined as the first 20 bytes of a double
    SHA-256.  RIPEMD-160 (used by Bitcoin) is deliberately avoided because it is
    not available in default OpenSSL 3 builds.
"""

from __future__ import annotations

import hashlib

__all__ = ["hash160", "hash256", "sha256"]

PUBKEY_HASH_LENGTH = 20


def sha256(data: bytes) -> bytes:
    """Return the SHA-256 digest of ``data``."""
    return hashlib.sha256(data).digest()


def hash256(data: bytes) -> bytes:
    """Return the double SHA-256 digest of ``data`` (32 bytes)."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def hash160(data: bytes) -> bytes:
    """Return the 20-byte public-key digest of ``data``."""
    return hash256(data)[:PUBKEY_HASH_LENGTH]
