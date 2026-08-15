"""secp256k1 keys, addresses and ECDSA signatures.

The heavy lifting is delegated to ``cryptography`` (OpenSSL), so the curve
arithmetic is constant-time and audited rather than hand-rolled.  This module
only adds the ScarletCoin encodings on top of it:

* private keys are 32 raw bytes, exported as Base58Check "WIF" strings;
* public keys are always the 33-byte *compressed* SEC1 form, so a signature can
  never be validated against two different encodings of the same point;
* signatures are 64-byte ``r || s`` pairs with a canonical low-``s`` value;
* an address is ``Base58Check(version || hash160(compressed_pubkey))``.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from scarletcoin.crypto.base58 import Base58Error, b58check_decode, b58check_encode
from scarletcoin.crypto.hashing import PUBKEY_HASH_LENGTH, hash160

__all__ = [
    "SIGNATURE_LENGTH",
    "Address",
    "InvalidKeyError",
    "InvalidSignatureError",
    "PrivateKey",
    "PublicKey",
]

#: Order of the secp256k1 group.
CURVE_ORDER: Final[int] = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_HALF_ORDER: Final[int] = CURVE_ORDER // 2
_CURVE: Final[ec.EllipticCurve] = ec.SECP256K1()
_PREHASHED: Final[ec.ECDSA] = ec.ECDSA(utils.Prehashed(hashes.SHA256()))

PRIVATE_KEY_LENGTH: Final[int] = 32
PUBLIC_KEY_LENGTH: Final[int] = 33
SIGNATURE_LENGTH: Final[int] = 64


class InvalidKeyError(ValueError):
    """Raised when key material is malformed or out of range."""


class InvalidSignatureError(ValueError):
    """Raised when a signature is malformed (not when it merely fails to verify)."""


def _check_digest(digest: bytes) -> bytes:
    if len(digest) != 32:
        raise ValueError(f"expected a 32-byte digest, got {len(digest)} bytes")
    return digest


@dataclass(frozen=True, slots=True)
class Address:
    """A pay-to-public-key-hash address: a network version byte plus a hash160."""

    version: int
    hash: bytes

    def __post_init__(self) -> None:
        if not 0 <= self.version <= 0xFF:
            raise InvalidKeyError(f"address version out of range: {self.version}")
        if len(self.hash) != PUBKEY_HASH_LENGTH:
            raise InvalidKeyError(
                f"address hash must be {PUBKEY_HASH_LENGTH} bytes, got {len(self.hash)}"
            )

    def __str__(self) -> str:
        return b58check_encode(self.version, self.hash)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Address({self!s})"

    @classmethod
    def decode(cls, text: str, *, expected_version: int | None = None) -> Address:
        """Parse an address string.

        Raises:
            InvalidKeyError: if the string is not a valid address (bad
                characters, bad checksum, wrong network or wrong length).
        """
        try:
            version, payload = b58check_decode(text.strip(), expected_version=expected_version)
        except Base58Error as exc:
            raise InvalidKeyError(f"invalid address {text!r}: {exc}") from exc
        if len(payload) != PUBKEY_HASH_LENGTH:
            raise InvalidKeyError(f"invalid address {text!r}: wrong payload length")
        return cls(version, payload)

    @classmethod
    def is_valid(cls, text: str, *, expected_version: int | None = None) -> bool:
        """Return ``True`` if ``text`` is a well-formed address."""
        try:
            cls.decode(text, expected_version=expected_version)
        except InvalidKeyError:
            return False
        return True


@dataclass(frozen=True, slots=True)
class PublicKey:
    """A compressed secp256k1 public key."""

    data: bytes

    def __post_init__(self) -> None:
        if len(self.data) != PUBLIC_KEY_LENGTH or self.data[0] not in (0x02, 0x03):
            raise InvalidKeyError("public key must be 33 bytes in compressed SEC1 form")
        # Validate that the bytes really are a point on the curve.
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, self.data)
        except ValueError as exc:
            raise InvalidKeyError(f"public key is not a valid curve point: {exc}") from exc

    @classmethod
    def from_bytes(cls, data: bytes) -> PublicKey:
        """Parse a 33-byte compressed public key."""
        return cls(bytes(data))

    def to_bytes(self) -> bytes:
        """Return the 33-byte compressed encoding."""
        return self.data

    def hash160(self) -> bytes:
        """Return the 20-byte digest committed to by addresses and outputs."""
        return hash160(self.data)

    def address(self, version: int) -> Address:
        """Return this key's address on the network identified by ``version``."""
        return Address(version, self.hash160())

    def verify(self, digest: bytes, signature: bytes) -> bool:
        """Return ``True`` if ``signature`` is a valid signature of ``digest``.

        Only canonical signatures are accepted: ``r`` and ``s`` must be in
        ``[1, n)`` and ``s`` must be in the lower half of the range.

        Raises:
            InvalidSignatureError: if ``signature`` is not 64 bytes long.
        """
        _check_digest(digest)
        if len(signature) != SIGNATURE_LENGTH:
            raise InvalidSignatureError(
                f"signature must be {SIGNATURE_LENGTH} bytes, got {len(signature)}"
            )
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        if not (1 <= r < CURVE_ORDER and 1 <= s <= _HALF_ORDER):
            return False
        key = ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, self.data)
        try:
            key.verify(utils.encode_dss_signature(r, s), digest, _PREHASHED)
        except InvalidSignature:
            return False
        return True


@dataclass(frozen=True, slots=True)
class PrivateKey:
    """A secp256k1 private key (32 bytes) able to sign 32-byte digests."""

    secret: bytes

    def __post_init__(self) -> None:
        if len(self.secret) != PRIVATE_KEY_LENGTH:
            raise InvalidKeyError(
                f"private key must be {PRIVATE_KEY_LENGTH} bytes, got {len(self.secret)}"
            )
        value = int.from_bytes(self.secret, "big")
        if not 1 <= value < CURVE_ORDER:
            raise InvalidKeyError("private key is out of the valid secp256k1 range")

    @classmethod
    def generate(cls) -> PrivateKey:
        """Create a new key from the operating system's CSPRNG."""
        while True:
            candidate = os.urandom(PRIVATE_KEY_LENGTH)
            value = int.from_bytes(candidate, "big")
            if 1 <= value < CURVE_ORDER:
                return cls(candidate)

    @classmethod
    def from_bytes(cls, data: bytes) -> PrivateKey:
        """Wrap 32 raw bytes as a private key."""
        return cls(bytes(data))

    @classmethod
    def from_wif(cls, wif: str, *, expected_version: int | None = None) -> PrivateKey:
        """Import a key from its Base58Check "wallet import format" string.

        Raises:
            InvalidKeyError: if the string is malformed or belongs to another network.
        """
        try:
            _, payload = b58check_decode(wif.strip(), expected_version=expected_version)
        except Base58Error as exc:
            raise InvalidKeyError(f"invalid private key: {exc}") from exc
        # A trailing 0x01 marks "the public key is compressed", as in Bitcoin WIF.
        if len(payload) == PRIVATE_KEY_LENGTH + 1 and payload[-1] == 0x01:
            payload = payload[:-1]
        if len(payload) != PRIVATE_KEY_LENGTH:
            raise InvalidKeyError("invalid private key: wrong payload length")
        return cls(payload)

    def to_bytes(self) -> bytes:
        """Return the raw 32-byte secret."""
        return self.secret

    def to_wif(self, version: int) -> str:
        """Export the key as a Base58Check string for the given network version."""
        return b58check_encode(version, self.secret + b"\x01")

    def public_key(self) -> PublicKey:
        """Derive the matching compressed public key."""
        key = ec.derive_private_key(int.from_bytes(self.secret, "big"), _CURVE)
        return PublicKey(key.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint))

    def address(self, version: int) -> Address:
        """Derive this key's address for the given network version."""
        return self.public_key().address(version)

    def sign(self, digest: bytes) -> bytes:
        """Sign a 32-byte ``digest``, returning a canonical 64-byte signature."""
        _check_digest(digest)
        key = ec.derive_private_key(int.from_bytes(self.secret, "big"), _CURVE)
        r, s = utils.decode_dss_signature(key.sign(digest, _PREHASHED))
        if s > _HALF_ORDER:  # enforce the canonical low-s form
            s = CURVE_ORDER - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PrivateKey):
            return NotImplemented
        return hmac.compare_digest(self.secret, other.secret)

    def __hash__(self) -> int:  # pragma: no cover - keys are rarely hashed
        return hash(self.secret)

    def __repr__(self) -> str:  # pragma: no cover - never leak secrets in logs
        return "PrivateKey(<redacted>)"
