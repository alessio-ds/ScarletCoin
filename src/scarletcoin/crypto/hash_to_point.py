"""Hash-to-point on secp256k1.

Maps an arbitrary byte string to a valid point on secp256k1 using
try-and-increment: hash a domain-separated counter, treat the result as an x
coordinate, and try successive x values until one lies on the curve. The point
is even-y, so it can be serialized as 33 compressed bytes.

Used to derive key images in linkable ring signatures and to close stealth
address shared secrets.
"""

from __future__ import annotations

from typing import Final

from ecdsa import SECP256k1
from ecdsa.ellipticcurve import PointJacobi

from scarletcoin.crypto.hashing import sha256
from scarletcoin.crypto.schnorr import POINT_SIZE, schnorr_point_to_bytes

__all__ = ["POINT_SIZE", "hash_to_point", "hash_to_point_bytes", "hash_to_scalar"]

_GRP = SECP256k1
_P = _GRP.curve.p()
_N = _GRP.order
#: Try-and-increment: the point has an even y coordinate (compressed byte 0x02).
_MASK: Final[bytes] = (0x02).to_bytes(1, "big") + (0xFF).to_bytes(32, "big")


def hash_to_point(data: bytes) -> PointJacobi:
    """Return a deterministic curve point for ``data``.

    The output is guaranteed to be a valid, non-infinity point with even y.
    """
    counter = 0
    while True:
        digest = sha256(data + counter.to_bytes(4, "big"))
        x_int = int.from_bytes(digest, "big") % _P
        y_sq = (pow(x_int, 3, _P) + 7) % _P
        if pow(y_sq, (_P - 1) // 2, _P) != 1:
            counter += 1
            continue
        y = pow(y_sq, (_P + 1) // 4, _P)
        if y % 2 == 1:
            y = _P - y
        return PointJacobi(_GRP.curve, x_int, y, 1, _N)


def hash_to_point_bytes(data: bytes) -> bytes:
    """Return the 33-byte compressed encoding of ``hash_to_point(data)``."""
    return schnorr_point_to_bytes(hash_to_point(data))


def hash_to_scalar(data: bytes) -> int:
    """Reduce ``data`` to a scalar in ``[1, n)``."""
    value = int.from_bytes(sha256(data), "big") % _N
    return value or 1