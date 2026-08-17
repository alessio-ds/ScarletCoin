"""Schnorr signatures on secp256k1.

Uses the ``ecdsa`` library for point arithmetic and deterministic nonces
derived with HMAC-SHA256. Signatures are 65 bytes: 33-byte compressed R
plus 32-byte big-endian s.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

from ecdsa import SECP256k1
from ecdsa.ellipticcurve import INFINITY, PointJacobi

__all__ = [
    "POINT_SIZE",
    "SCALAR_SIZE",
    "SchnorrError",
    "point_from_bytes",
    "schnorr_point_to_bytes",
    "schnorr_sign",
    "schnorr_verify",
]

GRP = SECP256k1
G: Final[PointJacobi] = GRP.generator
N = GRP.order
POINT_SIZE: Final[int] = 33
SCALAR_SIZE: Final[int] = 32
SIGNATURE_SIZE: Final[int] = POINT_SIZE + SCALAR_SIZE


class SchnorrError(ValueError):
    """Raised when a Schnorr input is malformed or out of range."""


def _scalar_from_bytes(data: bytes) -> int:
    if len(data) != SCALAR_SIZE:
        raise SchnorrError(f"scalar must be {SCALAR_SIZE} bytes, got {len(data)}")
    return int.from_bytes(data, "big") % N


def _scalar_to_bytes(value: int) -> bytes:
    return (value % N).to_bytes(SCALAR_SIZE, "big")


def _lift_x(x_int: int) -> PointJacobi | None:
    p = GRP.curve.p()
    if not 0 <= x_int < p:
        return None
    y_sq = (pow(x_int, 3, p) + 7) % p
    if pow(y_sq, (p - 1) // 2, p) != 1:
        return None
    y = pow(y_sq, (p + 1) // 4, p)
    if y % 2 == 0:
        return PointJacobi(GRP.curve, x_int, y, 1, N)
    return PointJacobi(GRP.curve, x_int, (p - y) % p, 1, N)


def schnorr_point_to_bytes(P: PointJacobi) -> bytes:
    """Serialize a point to 33 compressed bytes (02/03 prefix)."""
    if P == INFINITY:
        raise SchnorrError("cannot serialize the point at infinity")
    pa = P.to_affine()
    part = pa.y() & 1
    return ((0x02 + part).to_bytes(1, "big")) + pa.x().to_bytes(32, "big")


def _point_from_bytes(data: bytes) -> PointJacobi:
    if len(data) != POINT_SIZE or data[0] not in (0x02, 0x03):
        raise SchnorrError(f"point must be {POINT_SIZE} bytes starting with 02 or 03")
    x_int = int.from_bytes(data[1:], "big")
    y_even = data[0] == 0x02
    P = _lift_x(x_int)
    if P is None:
        raise SchnorrError("x coordinate does not correspond to a valid curve point")
    pa = P.to_affine()
    actual_even = (pa.y() & 1) == 0
    if y_even != actual_even:
        P = -P
    return P


def point_from_bytes(data: bytes) -> PointJacobi:
    """Parse a 33-byte compressed point.

    Raises:
        SchnorrError: if ``data`` is not a valid compressed encoding.
    """
    return _point_from_bytes(data)


def _derive_nonce(secret_bytes: bytes, message: bytes) -> int:
    h = hmac.digest(secret_bytes, message, hashlib.sha256)
    k = int.from_bytes(h, "big")
    while k == 0 or k >= N:
        h = hashlib.sha256(h).digest()
        k = int.from_bytes(h, "big")
    return k


def _schnorr_challenge(R_compressed: bytes, X_compressed: bytes, message: bytes) -> int:
    return int.from_bytes(hashlib.sha256(R_compressed + X_compressed + message).digest(), "big") % N


def schnorr_sign(secret_bytes: bytes, message: bytes) -> bytes:
    """Sign ``message`` with ``secret_bytes`` (32-byte private key).

    Returns a 65-byte signature: compressed R (33) || s (32, big-endian).
    """
    k = _scalar_from_bytes(secret_bytes)
    X = k * G
    X_c = schnorr_point_to_bytes(X)
    r = _derive_nonce(secret_bytes, message)
    R = r * G
    R_c = schnorr_point_to_bytes(R)
    e = _schnorr_challenge(R_c, X_c, message)
    s = (r + e * k) % N
    if s == 0:
        raise SchnorrError("zero s — this should be astronomically rare")
    return R_c + _scalar_to_bytes(s)


def schnorr_verify(public_key_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a Schnorr signature.

    Args:
        public_key_bytes: 33-byte compressed public key.
        message: The signed message (bytes).
        signature: 65-byte signature (R || s).

    Returns:
        ``True`` if the signature is valid.
    """
    if len(signature) != SIGNATURE_SIZE:
        return False
    if len(public_key_bytes) != POINT_SIZE:
        return False
    R_c = signature[:POINT_SIZE]
    s_bytes = signature[POINT_SIZE:]
    s = _scalar_from_bytes(s_bytes)
    if s == 0:
        return False
    try:
        X = _point_from_bytes(public_key_bytes)
        R = _point_from_bytes(R_c)
    except SchnorrError:
        return False
    e = _schnorr_challenge(R_c, public_key_bytes, message)
    return s * G == R + e * X