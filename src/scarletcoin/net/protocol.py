"""The ScarletCoin peer-to-peer wire protocol.

Every message is wrapped in the same envelope::

    magic     4 bytes   network identifier, e.g. b"SCRL"
    command  12 bytes   ASCII command name, zero padded
    length    4 bytes   payload length, little endian
    checksum  4 bytes   first four bytes of hash256(payload)
    payload   variable

The command set is deliberately small:

``version`` / ``verack``
    Handshake.  Carries the protocol version, user agent and chain height.
``ping`` / ``pong``
    Liveness.
``getaddr`` / ``addr``
    Peer discovery.
``getblocks`` / ``inv`` / ``getdata`` / ``notfound``
    Block and transaction announcement and retrieval.
``block`` / ``tx``
    The objects themselves.
``mempool``
    Ask a peer to announce everything in its memory pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from scarletcoin.core.block import Block
from scarletcoin.core.serialize import Reader, SerializationError, Writer
from scarletcoin.core.transaction import Transaction
from scarletcoin.crypto.hashing import hash256

__all__ = [
    "MAX_PAYLOAD",
    "PROTOCOL_VERSION",
    "Addr",
    "BlockMessage",
    "GetAddr",
    "GetBlocks",
    "GetData",
    "Inv",
    "InvItem",
    "InvType",
    "Mempool",
    "Message",
    "NetworkAddress",
    "NotFound",
    "Ping",
    "Pong",
    "ProtocolError",
    "TxMessage",
    "VerAck",
    "Version",
    "decode_payload",
    "encode_message",
    "parse_header",
]

PROTOCOL_VERSION = 1
HEADER_SIZE = 24
#: Largest payload we will read: a maximum-size block with room to spare.
MAX_PAYLOAD = 2 * 1024 * 1024
#: Most hashes announced or requested in a single message.
MAX_INV_ITEMS = 5_000
#: Most addresses in a single ``addr`` message.
MAX_ADDR_ITEMS = 1_000
#: Most block hashes a ``getblocks`` answer may contain.
MAX_BLOCKS_PER_INV = 500


class ProtocolError(Exception):
    """Raised when a peer sends something that violates the wire protocol."""


class InvType:
    """Inventory item types."""

    TX = 1
    BLOCK = 2


@dataclass(frozen=True, slots=True)
class InvItem:
    """One announced or requested object."""

    type: int
    hash: bytes

    def __post_init__(self) -> None:
        if len(self.hash) != 32:
            raise ProtocolError("inventory hash must be 32 bytes")
        if self.type not in (InvType.TX, InvType.BLOCK):
            raise ProtocolError(f"unknown inventory type {self.type}")

    @property
    def is_block(self) -> bool:
        """``True`` for a block inventory item."""
        return self.type == InvType.BLOCK

    def __str__(self) -> str:
        kind = "block" if self.is_block else "tx"
        return f"{kind} {self.hash[::-1].hex()}"


@dataclass(frozen=True, slots=True)
class NetworkAddress:
    """A peer's address as gossiped in ``addr`` messages."""

    host: str
    port: int
    last_seen: int = 0

    def __post_init__(self) -> None:
        if not self.host or len(self.host) > 255:
            raise ProtocolError("peer host must be between 1 and 255 characters")
        if not 1 <= self.port <= 0xFFFF:
            raise ProtocolError(f"peer port out of range: {self.port}")

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


class Message:
    """Base class for protocol messages."""

    command: ClassVar[str] = ""

    def encode(self) -> bytes:
        """Return the message payload."""
        return b""

    @classmethod
    def decode(cls, payload: bytes) -> Message:
        """Parse a payload into a message."""
        if payload:
            raise ProtocolError(f"{cls.command} takes no payload")
        return cls()

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self.encode() == other.encode()

    def __hash__(self) -> int:
        return hash((type(self), self.encode()))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}()"


@dataclass(frozen=True)
class Version(Message):
    """First message of the handshake."""

    command: ClassVar[str] = "version"

    version: int
    user_agent: str
    start_height: int
    nonce: int
    listen_port: int
    timestamp: int

    def encode(self) -> bytes:
        writer = Writer()
        writer.uint32(self.version)
        writer.varstr(self.user_agent)
        writer.uint32(self.start_height)
        writer.uint64(self.nonce)
        writer.uint16(self.listen_port)
        writer.uint64(self.timestamp)
        return writer.getvalue()

    @classmethod
    def decode(cls, payload: bytes) -> Version:
        reader = Reader(payload)
        message = cls(
            version=reader.uint32(),
            user_agent=reader.varstr(max_length=128),
            start_height=reader.uint32(),
            nonce=reader.uint64(),
            listen_port=reader.uint16(),
            timestamp=reader.uint64(),
        )
        reader.expect_end()
        return message


class VerAck(Message):
    """Acknowledges a ``version``."""

    command: ClassVar[str] = "verack"


@dataclass(frozen=True)
class Ping(Message):
    """Liveness probe."""

    command: ClassVar[str] = "ping"

    nonce: int

    def encode(self) -> bytes:
        return Writer().uint64(self.nonce).getvalue()

    @classmethod
    def decode(cls, payload: bytes) -> Ping:
        reader = Reader(payload)
        message = cls(reader.uint64())
        reader.expect_end()
        return message


@dataclass(frozen=True)
class Pong(Message):
    """Answer to a :class:`Ping`."""

    command: ClassVar[str] = "pong"

    nonce: int

    def encode(self) -> bytes:
        return Writer().uint64(self.nonce).getvalue()

    @classmethod
    def decode(cls, payload: bytes) -> Pong:
        reader = Reader(payload)
        message = cls(reader.uint64())
        reader.expect_end()
        return message


class GetAddr(Message):
    """Requests known peer addresses."""

    command: ClassVar[str] = "getaddr"


class Mempool(Message):
    """Requests an announcement of the peer's memory pool."""

    command: ClassVar[str] = "mempool"


@dataclass(frozen=True)
class Addr(Message):
    """Gossips known peer addresses."""

    command: ClassVar[str] = "addr"

    addresses: tuple[NetworkAddress, ...] = field(default_factory=tuple)

    def encode(self) -> bytes:
        if len(self.addresses) > MAX_ADDR_ITEMS:
            raise ProtocolError("too many addresses in one message")
        writer = Writer()
        writer.varint(len(self.addresses))
        for address in self.addresses:
            writer.varstr(address.host).uint16(address.port).uint32(address.last_seen)
        return writer.getvalue()

    @classmethod
    def decode(cls, payload: bytes) -> Addr:
        reader = Reader(payload)
        count = reader.varint()
        if count > MAX_ADDR_ITEMS:
            raise ProtocolError("too many addresses in one message")
        addresses = tuple(
            NetworkAddress(reader.varstr(max_length=255), reader.uint16(), reader.uint32())
            for _ in range(count)
        )
        reader.expect_end()
        return cls(addresses)


@dataclass(frozen=True)
class _InventoryMessage(Message):
    items: tuple[InvItem, ...] = field(default_factory=tuple)

    def encode(self) -> bytes:
        if len(self.items) > MAX_INV_ITEMS:
            raise ProtocolError("too many inventory items in one message")
        writer = Writer()
        writer.varint(len(self.items))
        for item in self.items:
            writer.uint8(item.type).hash32(item.hash)
        return writer.getvalue()

    @classmethod
    def decode(cls, payload: bytes):
        reader = Reader(payload)
        count = reader.varint()
        if count > MAX_INV_ITEMS:
            raise ProtocolError("too many inventory items in one message")
        items = tuple(InvItem(reader.uint8(), reader.hash32()) for _ in range(count))
        reader.expect_end()
        return cls(items)


class Inv(_InventoryMessage):
    """Announces objects the sender has."""

    command: ClassVar[str] = "inv"


class GetData(_InventoryMessage):
    """Requests objects previously announced."""

    command: ClassVar[str] = "getdata"


class NotFound(_InventoryMessage):
    """Reports requested objects the sender does not have."""

    command: ClassVar[str] = "notfound"


@dataclass(frozen=True)
class GetBlocks(Message):
    """Asks for the block hashes that follow a locator."""

    command: ClassVar[str] = "getblocks"

    locator: tuple[bytes, ...]
    stop_hash: bytes = b"\x00" * 32

    def encode(self) -> bytes:
        if not self.locator or len(self.locator) > 64:
            raise ProtocolError("a locator must contain between 1 and 64 hashes")
        writer = Writer()
        writer.varint(len(self.locator))
        for block_hash in self.locator:
            writer.hash32(block_hash)
        writer.hash32(self.stop_hash)
        return writer.getvalue()

    @classmethod
    def decode(cls, payload: bytes) -> GetBlocks:
        reader = Reader(payload)
        count = reader.varint()
        if not 1 <= count <= 64:
            raise ProtocolError("a locator must contain between 1 and 64 hashes")
        locator = tuple(reader.hash32() for _ in range(count))
        stop_hash = reader.hash32()
        reader.expect_end()
        return cls(locator, stop_hash)


@dataclass(frozen=True)
class BlockMessage(Message):
    """Carries a whole block."""

    command: ClassVar[str] = "block"

    block: Block

    def encode(self) -> bytes:
        return self.block.serialize()

    @classmethod
    def decode(cls, payload: bytes) -> BlockMessage:
        return cls(Block.deserialize(payload))


@dataclass(frozen=True)
class TxMessage(Message):
    """Carries a single transaction."""

    command: ClassVar[str] = "tx"

    transaction: Transaction

    def encode(self) -> bytes:
        return self.transaction.serialize()

    @classmethod
    def decode(cls, payload: bytes) -> TxMessage:
        return cls(Transaction.deserialize(payload))


_MESSAGE_TYPES: dict[str, type[Message]] = {
    message_type.command: message_type
    for message_type in (
        Version,
        VerAck,
        Ping,
        Pong,
        GetAddr,
        Addr,
        Inv,
        GetData,
        NotFound,
        GetBlocks,
        BlockMessage,
        TxMessage,
        Mempool,
    )
}


def encode_message(magic: bytes, message: Message) -> bytes:
    """Wrap ``message`` in its envelope."""
    if len(magic) != 4:
        raise ProtocolError("network magic must be 4 bytes")
    command = message.command.encode("ascii")
    if not command or len(command) > 12:
        raise ProtocolError(f"invalid command name {message.command!r}")
    payload = message.encode()
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"{message.command} payload is too large: {len(payload)} bytes")
    return (
        magic
        + command.ljust(12, b"\x00")
        + len(payload).to_bytes(4, "little")
        + hash256(payload)[:4]
        + payload
    )


def parse_header(header: bytes, magic: bytes) -> tuple[str, int, bytes]:
    """Split an envelope header into ``(command, payload_length, checksum)``.

    Raises:
        ProtocolError: if the magic is wrong, the command is not ASCII, or the
            announced payload is too large.
    """
    if len(header) != HEADER_SIZE:
        raise ProtocolError(f"message header must be {HEADER_SIZE} bytes")
    if header[:4] != magic:
        raise ProtocolError("message is for a different network")
    raw_command = header[4:16].rstrip(b"\x00")
    try:
        command = raw_command.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError("command name is not ASCII") from exc
    if not command.isprintable():
        raise ProtocolError("command name contains control characters")
    length = int.from_bytes(header[16:20], "little")
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"payload of {length} bytes is too large")
    return command, length, header[20:24]


def decode_payload(command: str, payload: bytes, checksum: bytes) -> Message | None:
    """Verify a payload's checksum and decode it.

    Returns:
        The message, or ``None`` if the command is unknown (unknown commands are
        ignored, so the protocol can grow without breaking old nodes).

    Raises:
        ProtocolError: if the checksum or the payload is malformed.
    """
    if hash256(payload)[:4] != checksum:
        raise ProtocolError(f"bad checksum on {command} message")
    message_type = _MESSAGE_TYPES.get(command)
    if message_type is None:
        return None
    try:
        return message_type.decode(payload)
    except (SerializationError, ValueError) as exc:
        raise ProtocolError(f"malformed {command} message: {exc}") from exc
