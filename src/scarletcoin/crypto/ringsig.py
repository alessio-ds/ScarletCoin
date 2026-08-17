"""Linkable ring signatures (LSAG) on secp256k1.

A ring signature proves that the signer knows the discrete logarithm of *one* of
the public keys in a ring, without revealing which. A *linkable* ring signature
additionally produces a key image ``K`` derived from the spent output, so
spending the same output twice yields the same key image and is rejected — this
is what prevents double spends on an anonymous chain.

Construction (Liu, Wei, Wong — "Linkable Spontaneous Anonymous Group
Signature"):

* Ring ``{P_0, ..., P_{n-1}}``, secret index ``s``, secret scalar ``x_s`` with
  ``P_s = x_s*G``.
* Key image ``K = x_s * H_p(P_s)`` where ``H_p`` is hash-to-point.
* The signature is ``(c_0, r_0, ..., r_{n-1}, K)``.

Serialized form (in :func:`ring_sign` / :func:`ring_verify`):

* ``varint`` ring size ``n``
* ``c_0``            — 32 bytes
* ``r_0 ... r_{n-1}`` — n x 32 bytes
* ``K``              — 33 bytes (compressed key image)
"""

from __future__ import annotations

import os
from typing import Final

from ecdsa import SECP256k1
from ecdsa.ellipticcurve import INFINITY, PointJacobi

from scarletcoin.core.serialize import Reader, SerializationError, Writer
from scarletcoin.crypto.hash_to_point import hash_to_point, hash_to_scalar
from scarletcoin.crypto.schnorr import (
    POINT_SIZE,
    SCALAR_SIZE,
    SchnorrError,
    point_from_bytes,
    schnorr_point_to_bytes,
)

__all__ = [
    "RingSignatureError",
    "extract_key_image",
    "ring_sign",
    "ring_verify",
]

GRP = SECP256k1
G: Final[PointJacobi] = GRP.generator
N = GRP.order
KEY_IMAGE_SIZE: Final[int] = POINT_SIZE
_CHALLENGE_TAG: Final[bytes] = b"ScarletCoin/ringsig/1"


class RingSignatureError(ValueError):
    """Raised when a ring signature is malformed or out of range."""


def _challenge(message: bytes, *points: PointJacobi) -> int:
    digest = _CHALLENGE_TAG + message
    for point in points:
        digest += schnorr_point_to_bytes(point)
    return hash_to_scalar(digest)


def ring_sign(
    ring: list[bytes],
    secret_index: int,
    secret_scalar: int,
    message: bytes,
) -> bytes:
    """Produce an LSAG signature spending the output at ``secret_index``.

    Args:
        ring: The one-time public keys (33-byte compressed each) that form the
            ring, in order. ``ring[secret_index]`` must be the output actually
            being spent.
        secret_index: Position of the real spent output in ``ring``.
        secret_scalar: Private spending key of ``ring[secret_index]`` (the scalar
            ``x`` such that ``x*G == ring[secret_index]``).
        message: The transaction's signature hash (32 bytes).

    Returns:
        The serialized signature: varint ring size + c_0 + r_0..r_{n-1} + K.
    """
    n = len(ring)
    if n < 2:
        raise RingSignatureError("a ring needs at least two members")
    if not 0 <= secret_index < n:
        raise RingSignatureError("secret index is outside the ring")

    points = [point_from_bytes(raw) for raw in ring]
    P_s = points[secret_index]
    K = secret_scalar * hash_to_point(schnorr_point_to_bytes(P_s))
    K_bytes = schnorr_point_to_bytes(K)

    u = int.from_bytes(os.urandom(32), "big") % (N - 1) + 1

    c: list[int] = [0] * n
    r: list[int] = [0] * n

    c[(secret_index + 1) % n] = _challenge(
        message, u * G, u * hash_to_point(schnorr_point_to_bytes(P_s))
    )

    idx = (secret_index + 1) % n
    while idx != secret_index:
        r[idx] = int.from_bytes(os.urandom(32), "big") % (N - 1) + 1
        Hp = hash_to_point(schnorr_point_to_bytes(points[idx]))
        L = r[idx] * G + c[idx] * points[idx]
        t = r[idx] * Hp + c[idx] * K
        c[(idx + 1) % n] = _challenge(message, L, t)
        idx = (idx + 1) % n

    r[secret_index] = (u - c[secret_index] * secret_scalar) % N

    writer = Writer()
    writer.varint(n)
    writer.raw((c[0] % N).to_bytes(SCALAR_SIZE, "big"))
    for value in r:
        writer.raw((value % N).to_bytes(SCALAR_SIZE, "big"))
    writer.raw(K_bytes)
    return writer.getvalue()


def ring_verify(ring: list[bytes], message: bytes, full_signature: bytes) -> bool:
    """Verify an LSAG signature.

    Args:
        ring: The ring's one-time public keys (33-byte compressed each), in order.
        message: The transaction's signature hash (32 bytes).
        full_signature: The serialized signature from :func:`ring_sign`.

    Returns:
        ``True`` if the signature is valid.
    """
    try:
        reader = Reader(full_signature)
        n = reader.varint()
        if n < 2 or n != len(ring):
            return False
        c0 = int.from_bytes(reader.raw(SCALAR_SIZE), "big")
        r = [int.from_bytes(reader.raw(SCALAR_SIZE), "big") for _ in range(n)]
        K_bytes = reader.raw(KEY_IMAGE_SIZE)
        reader.expect_end()
    except (SerializationError, RingSignatureError):
        return False

    if any(not (0 <= value < N) for value in [c0, *r]):
        return False
    try:
        K = point_from_bytes(K_bytes)
        points = [point_from_bytes(raw) for raw in ring]
    except SchnorrError:
        return False
    if K == INFINITY:
        return False

    c_chain = c0
    for idx in range(n):
        Hp = hash_to_point(schnorr_point_to_bytes(points[idx]))
        L = r[idx] * G + c_chain * points[idx]
        t = r[idx] * Hp + c_chain * K
        c_chain = _challenge(message, L, t)

    return c_chain == c0


def extract_key_image(full_signature: bytes) -> bytes | None:
    """Extract the 33-byte key image from a serialized LSAG signature.

    Returns ``None`` if the signature is malformed.
    """
    try:
        reader = Reader(full_signature)
        n = reader.varint()
        total_scalars = (1 + n) * SCALAR_SIZE
        reader.raw(total_scalars)
        k = reader.raw(KEY_IMAGE_SIZE)
        return k
    except (SerializationError, RingSignatureError):
        return None