"""Deterministic binary serialisation helpers.

Every consensus object is encoded with these primitives, which mirror Bitcoin's
conventions: little-endian fixed-width integers and compact-size (varint)
lengths.  Encoding must be canonical, so :class:`Reader` rejects over-long
varints, and :class:`Writer` never emits them.
"""

from __future__ import annotations

import struct

__all__ = ["Reader", "SerializationError", "Writer"]


class SerializationError(ValueError):
    """Raised when a byte stream is malformed, truncated or non-canonical."""


class Writer:
    """Accumulates a byte string from consensus primitives."""

    __slots__ = ("_parts",)

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def raw(self, data: bytes) -> Writer:
        """Append raw bytes."""
        self._parts.append(bytes(data))
        return self

    def uint8(self, value: int) -> Writer:
        self._parts.append(struct.pack("<B", value))
        return self

    def uint16(self, value: int) -> Writer:
        self._parts.append(struct.pack("<H", value))
        return self

    def uint32(self, value: int) -> Writer:
        self._parts.append(struct.pack("<I", value))
        return self

    def uint64(self, value: int) -> Writer:
        self._parts.append(struct.pack("<Q", value))
        return self

    def int32(self, value: int) -> Writer:
        self._parts.append(struct.pack("<i", value))
        return self

    def varint(self, value: int) -> Writer:
        """Append a compact-size integer."""
        if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
            raise SerializationError(f"varint out of range: {value}")
        if value < 0xFD:
            self._parts.append(struct.pack("<B", value))
        elif value <= 0xFFFF:
            self._parts.append(b"\xfd" + struct.pack("<H", value))
        elif value <= 0xFFFFFFFF:
            self._parts.append(b"\xfe" + struct.pack("<I", value))
        else:
            self._parts.append(b"\xff" + struct.pack("<Q", value))
        return self

    def varbytes(self, data: bytes) -> Writer:
        """Append a length-prefixed byte string."""
        self.varint(len(data))
        self._parts.append(bytes(data))
        return self

    def varstr(self, text: str) -> Writer:
        """Append a length-prefixed UTF-8 string."""
        return self.varbytes(text.encode("utf-8"))

    def hash32(self, digest: bytes) -> Writer:
        """Append a 32-byte hash."""
        if len(digest) != 32:
            raise SerializationError(f"expected a 32-byte hash, got {len(digest)}")
        self._parts.append(bytes(digest))
        return self

    def getvalue(self) -> bytes:
        """Return everything written so far."""
        return b"".join(self._parts)

    def __len__(self) -> int:
        return sum(len(part) for part in self._parts)


class Reader:
    """Reads consensus primitives out of a byte string."""

    __slots__ = ("_data", "_offset")

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)
        self._offset = 0

    @property
    def offset(self) -> int:
        """Number of bytes consumed so far."""
        return self._offset

    @property
    def remaining(self) -> int:
        """Number of bytes left in the stream."""
        return len(self._data) - self._offset

    def raw(self, length: int) -> bytes:
        """Read exactly ``length`` bytes."""
        if length < 0:
            raise SerializationError("negative read length")
        if self.remaining < length:
            raise SerializationError(
                f"truncated stream: wanted {length} bytes, {self.remaining} available"
            )
        chunk = self._data[self._offset : self._offset + length]
        self._offset += length
        return chunk

    def uint8(self) -> int:
        return struct.unpack("<B", self.raw(1))[0]

    def uint16(self) -> int:
        return struct.unpack("<H", self.raw(2))[0]

    def uint32(self) -> int:
        return struct.unpack("<I", self.raw(4))[0]

    def uint64(self) -> int:
        return struct.unpack("<Q", self.raw(8))[0]

    def int32(self) -> int:
        return struct.unpack("<i", self.raw(4))[0]

    def varint(self) -> int:
        """Read a canonical compact-size integer."""
        prefix = self.uint8()
        if prefix < 0xFD:
            return prefix
        if prefix == 0xFD:
            value = self.uint16()
            minimum = 0xFD
        elif prefix == 0xFE:
            value = self.uint32()
            minimum = 0x10000
        else:
            value = self.uint64()
            minimum = 0x100000000
        if value < minimum:
            raise SerializationError("non-canonical varint encoding")
        return value

    def varbytes(self, *, max_length: int | None = None) -> bytes:
        """Read a length-prefixed byte string."""
        length = self.varint()
        if max_length is not None and length > max_length:
            raise SerializationError(f"byte string too long: {length} > {max_length}")
        return self.raw(length)

    def varstr(self, *, max_length: int = 1024) -> str:
        """Read a length-prefixed UTF-8 string."""
        raw = self.varbytes(max_length=max_length)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SerializationError(f"invalid UTF-8 string: {exc}") from exc

    def hash32(self) -> bytes:
        """Read a 32-byte hash."""
        return self.raw(32)

    def expect_end(self) -> None:
        """Assert that the whole stream has been consumed."""
        if self.remaining:
            raise SerializationError(f"{self.remaining} trailing bytes after object")
