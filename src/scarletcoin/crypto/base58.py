"""Base58 and Base58Check encoding.

Base58 is the classic Bitcoin alphabet: all alphanumeric characters except
``0``, ``O``, ``I`` and ``l``, which are easy to confuse when typed by hand.
"""

from __future__ import annotations

from scarletcoin.crypto.hashing import hash256

__all__ = [
    "ALPHABET",
    "Base58Error",
    "b58check_decode",
    "b58check_encode",
    "b58decode",
    "b58encode",
]

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: value for value, char in enumerate(ALPHABET)}
_CHECKSUM_LENGTH = 4


class Base58Error(ValueError):
    """Raised when a Base58 or Base58Check string is malformed."""


def b58encode(data: bytes) -> str:
    """Encode ``data`` as a Base58 string."""
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    digits: list[str] = []
    while number > 0:
        number, remainder = divmod(number, 58)
        digits.append(ALPHABET[remainder])
    return ALPHABET[0] * leading_zeros + "".join(reversed(digits))


def b58decode(text: str) -> bytes:
    """Decode a Base58 string into bytes.

    Raises:
        Base58Error: if ``text`` contains characters outside the alphabet.
    """
    if not text:
        return b""
    number = 0
    for char in text:
        try:
            number = number * 58 + _INDEX[char]
        except KeyError:
            raise Base58Error(f"invalid Base58 character {char!r}") from None
    leading_zeros = len(text) - len(text.lstrip(ALPHABET[0]))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * leading_zeros + body


def b58check_encode(version: int, payload: bytes) -> str:
    """Encode ``payload`` with a one-byte ``version`` prefix and a checksum."""
    if not 0 <= version <= 0xFF:
        raise Base58Error(f"version byte out of range: {version}")
    body = bytes([version]) + payload
    return b58encode(body + hash256(body)[:_CHECKSUM_LENGTH])


def b58check_decode(text: str, *, expected_version: int | None = None) -> tuple[int, bytes]:
    """Decode a Base58Check string into its ``(version, payload)`` pair.

    Raises:
        Base58Error: if the string is too short, the checksum does not match, or
            the version byte differs from ``expected_version``.
    """
    raw = b58decode(text)
    if len(raw) < 1 + _CHECKSUM_LENGTH:
        raise Base58Error("Base58Check string is too short")
    body, checksum = raw[:-_CHECKSUM_LENGTH], raw[-_CHECKSUM_LENGTH:]
    if hash256(body)[:_CHECKSUM_LENGTH] != checksum:
        raise Base58Error("bad checksum: the string was mistyped or corrupted")
    version, payload = body[0], body[1:]
    if expected_version is not None and version != expected_version:
        raise Base58Error(f"unexpected version byte {version:#04x}, wanted {expected_version:#04x}")
    return version, payload
