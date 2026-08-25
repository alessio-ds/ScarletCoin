"""A single peer-to-peer connection."""

from __future__ import annotations

import contextlib
import itertools
import logging
import socket
import threading
import time
from collections import deque

from scarletcoin.net import cipher, protocol
from scarletcoin.net.protocol import HEADER_SIZE, Message, ProtocolError

__all__ = ["_RECV_TIMEOUT", "Peer", "PeerDisconnected"]

_ids = itertools.count(1)
_MAX_KNOWN_INVENTORY = 20_000
_RECV_TIMEOUT = 120.0

logger = logging.getLogger(__name__)


class PeerDisconnected(Exception):
    """Raised when the remote end closed the connection or stopped responding."""


class Peer:
    """A framed message channel to one remote node, plus what we know about it."""

    def __init__(
        self,
        sock: socket.socket,
        address: tuple[str, int],
        *,
        magic: bytes,
        inbound: bool,
    ) -> None:
        self.id = next(_ids)
        self.socket = sock
        self.host, self.port = address[0], address[1]
        self.magic = magic
        self.inbound = inbound
        self.connected_at = time.time()

        self._send_lock = threading.Lock()
        self._closed = threading.Event()

        # Link encryption, enabled once the handshake exchanges ephemeral keys.
        self._cipher: cipher.P2PCipher | None = None
        self.ephemeral_key = cipher.generate_ephemeral_key()
        """This connection's ephemeral secp256k1 key for the ECDH handshake."""

        # Handshake state, filled in from the peer's ``version`` message.
        self.version: int | None = None
        self.nonce: int | None = None
        """The random number the peer identifies itself with."""
        self.user_agent: str = ""
        self.start_height: int = 0
        self.listen_port: int | None = None
        self.handshake_done = threading.Event()

        # Bookkeeping used by the node.
        self.last_message_at = time.time()
        self.last_ping_nonce: int | None = None
        self.last_ping_sent: float | None = None
        self.latency: float | None = None
        self.misbehaviour = 0
        self.known_inventory: set[bytes] = set()
        self.pending_blocks: deque[bytes] = deque()
        self.requested_blocks: set[bytes] = set()
        self.syncing = False
        self.blocks_served = 0
        self._blocks_served_last_check = 0
        self._last_getdata_at: float | None = None

    # ------------------------------------------------------------------ helpers

    @property
    def closed(self) -> bool:
        """``True`` once the connection has been closed."""
        return self._closed.is_set()

    @property
    def address(self) -> str:
        """``host:port`` of the remote end."""
        return f"{self.host}:{self.port}"

    @property
    def advertised_address(self) -> tuple[str, int] | None:
        """The address the peer says it listens on, if it gave one."""
        if self.listen_port:
            return self.host, self.listen_port
        return None

    def __str__(self) -> str:
        direction = "in" if self.inbound else "out"
        return f"peer#{self.id}[{direction} {self.address}]"

    def note_inventory(self, block_hash: bytes) -> None:
        """Remember that this peer already knows about ``block_hash``."""
        if len(self.known_inventory) >= _MAX_KNOWN_INVENTORY:
            self.known_inventory.clear()
        self.known_inventory.add(block_hash)

    def knows(self, block_hash: bytes) -> bool:
        """Return ``True`` if we already exchanged this hash with the peer."""
        return block_hash in self.known_inventory

    # ------------------------------------------------------------------- traffic

    def enable_encryption(self, key: bytes) -> None:
        """Turn on link encryption with the shared session key."""
        self._cipher = cipher.P2PCipher(key)

    def send(self, message: Message) -> None:
        """Send a message.

        Raises:
            PeerDisconnected: if the socket is gone.
        """
        if self.closed:
            raise PeerDisconnected(f"{self} is closed")
        command = message.command
        payload = message.encode()
        if self._cipher is not None:
            payload = self._cipher.encrypt(payload)
        data = protocol.encode_payload(self.magic, command.encode("ascii"), payload)
        try:
            with self._send_lock:
                self.socket.sendall(data)
        except OSError as exc:
            self.close()
            raise PeerDisconnected(f"{self}: send failed: {exc}") from exc
        logger.debug(
            "%s: sent cmd=%s payload_len=%d total=%d",
            self,
            command,
            len(payload),
            len(data),
        )

    def _recv_exactly(self, length: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < length:
            try:
                chunk = self.socket.recv(min(65536, length - len(buffer)))
            except TimeoutError as exc:
                raise PeerDisconnected(f"{self}: read timed out") from exc
            except OSError as exc:
                raise PeerDisconnected(f"{self}: read failed: {exc}") from exc
            if not chunk:
                raise PeerDisconnected(f"{self}: connection closed by the remote end")
            buffer.extend(chunk)
        return bytes(buffer)

    def receive(self) -> Message | None:
        """Read one message.

        Returns:
            The decoded message, or ``None`` for a well-formed message with an
            unknown command (which is ignored on purpose).

        Raises:
            PeerDisconnected: if the connection is gone.
            ProtocolError: if the peer violated the protocol.
        """
        header = self._recv_exactly(HEADER_SIZE)
        command, length, checksum = protocol.parse_header(header, self.magic)
        logger.debug(
            "%s: received header cmd=%s payload_len=%d",
            self,
            command,
            length,
        )
        payload = self._recv_exactly(length) if length else b""
        self.last_message_at = time.time()
        if self._cipher is not None:
            plaintext = self._cipher.decrypt(payload)
            if plaintext is None:
                raise ProtocolError(f"{self}: cannot decrypt message (forged or replayed)")
            return protocol.decode_payload(command, plaintext)
        return protocol.decode_payload(command, payload, checksum)

    def close(self) -> None:
        """Close the connection; safe to call more than once."""
        if self._closed.is_set():
            return
        self._closed.set()
        self.handshake_done.set()
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.socket.close()

    # ------------------------------------------------------------------ reports

    def to_dict(self) -> dict:
        """Return a JSON-friendly summary, used by the ``getpeers`` RPC."""
        return {
            "id": self.id,
            "address": self.address,
            "direction": "inbound" if self.inbound else "outbound",
            "user_agent": self.user_agent,
            "version": self.version,
            "start_height": self.start_height,
            "connected_for": round(time.time() - self.connected_at, 1),
            "latency_ms": None if self.latency is None else round(self.latency * 1000, 1),
            "misbehaviour": self.misbehaviour,
        }


def connect_to(host: str, port: int, *, magic: bytes, timeout: float = 10.0) -> Peer:
    """Open an outbound connection to ``host:port``.

    Raises:
        PeerDisconnected: if the connection cannot be established.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise PeerDisconnected(f"cannot connect to {host}:{port}: {exc}") from exc
    sock.settimeout(_RECV_TIMEOUT)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return Peer(sock, (host, port), magic=magic, inbound=False)
