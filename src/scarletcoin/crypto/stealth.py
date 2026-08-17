"""Dual-key stealth addresses.

A recipient shares a public *address* made of two curve points::

    A = a*G   (view key)
    B = b*G   (spend key)

A sender picks a random ephemeral scalar ``r`` and produces a one-time public
key ``P`` that only the recipient can recognize and spend::

    R = r*G
    P = H_s(r*A)*G + B

The output carries ``P`` and the transaction carries ``R``. The recipient, who
holds the private view key ``a``, scans every transaction by computing
``P' = H_s(a*R)*G + B`` and checking ``P' == P``. When there is a match, the
private spending key of the one-time output is ``H_s(a*R) + b``.

The address is encoded as ``Base58Check(version || A || B)``: two 33-byte
compressed public keys plus a checksum.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ecdsa import SECP256k1
from ecdsa.ellipticcurve import INFINITY, PointJacobi

from scarletcoin.crypto.base58 import Base58Error, b58check_decode, b58check_encode
from scarletcoin.crypto.hash_to_point import hash_to_scalar
from scarletcoin.crypto.schnorr import (
    POINT_SIZE,
    SchnorrError,
    point_from_bytes,
    schnorr_point_to_bytes,
)

__all__ = [
    "ADDRESS_PUBKEY_COUNT",
    "StealthAddress",
    "StealthError",
    "a_scalar",
    "b_scalar",
    "derive_one_time_public",
    "derive_shared_secret",
    "recognize_output",
    "spend_key_for_output",
]

GRP = SECP256k1
G: Final[PointJacobi] = GRP.generator
N = GRP.order

ADDRESS_PUBKEY_COUNT: Final[int] = 2

_TAG: Final[bytes] = b"ScarletCoin/stealth/shared/1"


class StealthError(ValueError):
    """Raised when stealth-address material is malformed or from another network."""


def _scalar(secret: bytes) -> int:
    if len(secret) != 32:
        raise StealthError(f"a secret scalar must be 32 bytes, got {len(secret)}")
    value = int.from_bytes(secret, "big") % N
    return value or 1


def a_scalar(secret: bytes) -> int:
    """Return the private view scalar ``a`` for a 32-byte view secret."""
    return _scalar(secret)


def b_scalar(secret: bytes) -> int:
    """Return the private spend scalar ``b`` for a 32-byte spend secret."""
    return _scalar(secret)


@dataclass(frozen=True, slots=True)
class StealthAddress:
    """A dual-key public address: a view key ``A`` and a spend key ``B``."""

    version: int
    A: PointJacobi
    B: PointJacobi

    def __post_init__(self) -> None:
        if not 0 <= self.version <= 0xFF:
            raise StealthError(f"address version out of range: {self.version}")
        if self.A == INFINITY or self.B == INFINITY:
            raise StealthError("address keys must be valid curve points")

    def encode(self) -> str:
        """Return the Base58Check string ``version || A || B``."""
        payload = schnorr_point_to_bytes(self.A) + schnorr_point_to_bytes(self.B)
        return b58check_encode(self.version, payload)

    @classmethod
    def decode(cls, text: str, *, expected_version: int | None = None) -> StealthAddress:
        """Parse a Base58Check stealth address.

        Raises:
            StealthError: if the string is malformed, has a bad checksum, or the
                version byte does not match ``expected_version``.
        """
        try:
            version, payload = b58check_decode(text.strip(), expected_version=expected_version)
        except Base58Error as exc:
            raise StealthError(f"invalid address {text!r}: {exc}") from exc
        if len(payload) != ADDRESS_PUBKEY_COUNT * POINT_SIZE:
            raise StealthError("invalid address: wrong payload length")
        try:
            A = point_from_bytes(payload[:POINT_SIZE])
            B = point_from_bytes(payload[POINT_SIZE:])
        except SchnorrError as exc:
            raise StealthError(f"invalid address: {exc}") from exc
        return cls(version, A, B)

    def __str__(self) -> str:
        return self.encode()


def derive_ephemeral(seed: bytes) -> tuple[PointJacobi, int]:
    """Return ``(R, r)``: a fresh ephemeral public point and its private scalar.

    ``seed`` should be 32 bytes of high-entropy randomness (e.g. ``os.urandom(32)``)
    so different payments get different one-time keys.
    """
    r = hash_to_scalar(b"ScarletCoin/stealth/ephemeral/1" + seed)
    return r * G, r


def derive_shared_secret(r: int, A: PointJacobi) -> bytes:
    """Return the shared secret point's encoding, ``r*A``."""
    return schnorr_point_to_bytes(r * A)


def _shared_scalar(shared: bytes) -> int:
    return hash_to_scalar(_TAG + shared)


def derive_one_time_public(r: int, address: StealthAddress) -> PointJacobi:
    """Return the one-time public key ``P = H_s(r*A)*G + B`` for ``address``."""
    x = _shared_scalar(derive_shared_secret(r, address.A))
    return x * G + address.B


def recognize_output(
    r_point: PointJacobi, P: PointJacobi, a_secret: bytes, address: StealthAddress
) -> bool:
    """Return ``True`` if one-time output ``P`` (from ephemeral ``R``) belongs to ``address``.

    ``a_secret`` is the recipient's 32-byte private view key.
    """
    shared = derive_shared_secret(_scalar(a_secret), r_point)
    return _shared_scalar(shared) * G + address.B == P


def spend_key_for_output(r_point: PointJacobi, a_secret: bytes, b_secret: bytes) -> int:
    """Return the private scalar that spends a one-time output.

    ``a_secret`` and ``b_secret`` are the recipient's 32-byte view and spend keys.
    """
    shared = derive_shared_secret(_scalar(a_secret), r_point)
    return (_shared_scalar(shared) + _scalar(b_secret)) % N