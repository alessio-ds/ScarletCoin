"""A WebSocket endpoint that pushes chain events to browser clients.

The explorer connects to it so it can reload the moment a new block arrives,
instead of polling.  The hub runs its own event loop on a daemon thread; the
node pushes events into it from its own threads through
:meth:`WebSocketHub.broadcast`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

__all__ = ["WebSocketHub"]


class WebSocketHub:
    """A tiny pub/sub endpoint: every connected client receives every event."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port
        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._started = threading.Event()
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        """``True`` once the server is accepting connections."""
        return self._started.is_set() and not self._stop.is_set()

    @property
    def url(self) -> str | None:
        """``ws://host:port`` of the endpoint, or ``None`` before it is known."""
        if self.port == 0:
            return None
        return f"ws://{self.host}:{self.port}"

    def start(self) -> None:
        """Start the server on a background thread."""
        self._thread = threading.Thread(target=self._run, name="scarlet-ws", daemon=True)
        self._thread.start()
        self._started.wait(timeout=10.0)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:  # pragma: no cover - defensive, must not kill the thread
            logger.exception("websocket server failed")
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        self._server = await serve(self._handler, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self._started.set()
        logger.info("websocket endpoint listening on %s", self.url)
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, websocket) -> None:
        self._clients.add(websocket)
        try:
            async for _message in websocket:
                pass  # clients are read-only; incoming messages are ignored
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(websocket)

    def broadcast(self, event: dict) -> None:
        """Push ``event`` to every connected client; a no-op if not running."""
        if self._loop is None or self._loop.is_closed() or not self.running:
            return
        message = json.dumps(event)
        asyncio.run_coroutine_threadsafe(self._broadcast_now(message), self._loop)

    async def _broadcast_now(self, message: str) -> None:
        dead: list = []
        for client in list(self._clients):
            try:
                await client.send(message)
            except ConnectionClosed:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    def stop(self) -> None:
        """Stop the server; safe to call more than once."""
        if not self.running:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
