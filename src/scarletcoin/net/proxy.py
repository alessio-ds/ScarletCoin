"""SOCKS5 proxy support for P2P and RPC connections."""

from __future__ import annotations

import http.client
import socket
import struct

__all__ = [
    "SocksHTTPConnection",
    "SocksHTTPSConnection",
    "socks5_connect",
]

SOCKS5 = 0x05
SOCKS5_CONNECT = 0x01
SOCKS5_ATYP_DOMAIN = 0x03


def _recv_exactly(sock: socket.socket, length: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < length:
        chunk = sock.recv(length - len(buffer))
        if not chunk:
            raise OSError("SOCKS5 proxy closed the connection")
        buffer.extend(chunk)
    return bytes(buffer)


def socks5_connect(
    proxy_host: str, proxy_port: int, dest_host: str, dest_port: int, timeout: float = 30.0
) -> socket.socket:
    """Establish a SOCKS5-proxied TCP connection.

    Returns the connected socket.
    """
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        sock.sendall(b"\x05\x01\x00")
        resp = _recv_exactly(sock, 2)
        if resp != b"\x05\x00":
            raise OSError("SOCKS5 proxy rejected the no-auth handshake")
        host_bytes = dest_host.encode("utf-8")
        request = struct.pack("!BBBB", SOCKS5, SOCKS5_CONNECT, 0, SOCKS5_ATYP_DOMAIN)
        request += struct.pack("!B", len(host_bytes)) + host_bytes + struct.pack("!H", dest_port)
        sock.sendall(request)
        reply = _recv_exactly(sock, 10)
        if reply[0] != SOCKS5 or reply[1] != 0:
            raise OSError(f"SOCKS5 proxy refused connection to {dest_host}:{dest_port}")
        # Skip address field (4-18 bytes depending on type)
        if reply[3] == 0x01:
            _recv_exactly(sock, 4)
        elif reply[3] == 0x03:
            length = _recv_exactly(sock, 1)[0]
            _recv_exactly(sock, length)
        elif reply[3] == 0x04:
            _recv_exactly(sock, 16)
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


class _SocksConnection(http.client.HTTPConnection):
    """HTTP connection through a SOCKS5 proxy."""

    def __init__(
        self, proxy_host: str, proxy_port: int, dest_host: str, dest_port: int,
        timeout: float = 30.0, **kwargs
    ) -> None:
        self._proxy = (proxy_host, proxy_port)
        super().__init__(dest_host, dest_port, timeout=timeout, **kwargs)

    def connect(self) -> None:
        self.sock = socks5_connect(
            self._proxy[0], self._proxy[1], self.host, self.port, timeout=self.timeout
        )


class SocksHTTPConnection(_SocksConnection):
    """HTTP connection through a SOCKS5 proxy."""


class SocksHTTPSConnection(_SocksConnection):
    """HTTPS connection through a SOCKS5 proxy."""

    def __init__(
        self, proxy_host: str, proxy_port: int, dest_host: str, dest_port: int,
        timeout: float = 30.0, context=None, **kwargs
    ) -> None:
        super().__init__(proxy_host, proxy_port, dest_host, dest_port, timeout=timeout, **kwargs)
        self._context = context

    def connect(self) -> None:
        super().connect()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)