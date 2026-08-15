"""The address book: peers we know about, persisted between runs."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = ["AddressBook", "AddressEntry", "parse_address"]

logger = logging.getLogger(__name__)

#: Give up on an address after this many consecutive failures.
MAX_FAILURES = 10
#: Forget addresses we have not heard from in a month.
STALE_AFTER = 30 * 24 * 3600


def parse_address(text: str, default_port: int) -> tuple[str, int]:
    """Parse ``host``, ``host:port`` or ``[v6]:port`` into a ``(host, port)`` pair.

    Raises:
        ValueError: if the port is not a number in range.
    """
    text = text.strip()
    if not text:
        raise ValueError("empty peer address")
    if text.startswith("["):  # bracketed IPv6
        end = text.find("]")
        if end == -1:
            raise ValueError(f"malformed IPv6 address {text!r}")
        host = text[1:end]
        rest = text[end + 1 :]
        port = int(rest[1:]) if rest.startswith(":") else default_port
    elif text.count(":") == 1:
        host, _, raw_port = text.partition(":")
        port = int(raw_port)
    else:  # bare hostname, IPv4 or unbracketed IPv6
        host, port = text, default_port
    if not host:
        raise ValueError(f"malformed peer address {text!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range in {text!r}")
    return host, port


@dataclass
class AddressEntry:
    """One known peer address and how our attempts to reach it went."""

    host: str
    port: int
    last_seen: int = 0
    last_try: int = 0
    failures: int = 0
    source: str = "gossip"

    @property
    def key(self) -> tuple[str, int]:
        """Identity of the entry."""
        return self.host, self.port

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


class AddressBook:
    """A thread-safe, file-backed set of peer addresses."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, int], AddressEntry] = {}
        self._banned: dict[str, float] = {}
        if path is not None:
            self.load()

    # ------------------------------------------------------------------ storage

    def load(self) -> None:
        """Read the address book from disk, ignoring a missing or corrupt file."""
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            entries = [AddressEntry(**item) for item in raw.get("peers", [])]
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("ignoring unreadable address book %s: %s", self.path, exc)
            return
        with self._lock:
            self._entries = {entry.key: entry for entry in entries}

    def save(self) -> None:
        """Write the address book to disk atomically."""
        if self.path is None:
            return
        with self._lock:
            payload = {"peers": [asdict(entry) for entry in self._entries.values()]}
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, indent=1), "utf-8")
            temporary.replace(self.path)
        except OSError as exc:  # pragma: no cover - disk errors
            logger.warning("could not save the address book: %s", exc)

    # ------------------------------------------------------------------ mutation

    def add(self, host: str, port: int, *, source: str = "gossip", last_seen: int = 0) -> None:
        """Record an address, keeping the newest ``last_seen`` we have seen."""
        with self._lock:
            key = (host, port)
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = AddressEntry(
                    host, port, last_seen=last_seen or int(time.time()), source=source
                )
            else:
                entry.last_seen = max(entry.last_seen, last_seen)

    def mark_success(self, host: str, port: int) -> None:
        """Record a successful connection."""
        with self._lock:
            entry = self._entries.setdefault((host, port), AddressEntry(host, port))
            entry.last_seen = int(time.time())
            entry.last_try = int(time.time())
            entry.failures = 0

    def mark_failure(self, host: str, port: int) -> None:
        """Record a failed connection attempt."""
        with self._lock:
            entry = self._entries.setdefault((host, port), AddressEntry(host, port))
            entry.last_try = int(time.time())
            entry.failures += 1
            if entry.failures >= MAX_FAILURES:
                self._entries.pop((host, port), None)

    def forget(self, host: str, port: int) -> None:
        """Drop one address, without banning the host."""
        with self._lock:
            self._entries.pop((host, port), None)

    def ban(self, host: str, seconds: float = 3600.0) -> None:
        """Refuse connections from ``host`` for a while."""
        with self._lock:
            self._banned[host] = time.time() + seconds
            for key in [key for key in self._entries if key[0] == host]:
                del self._entries[key]

    def is_banned(self, host: str) -> bool:
        """Return ``True`` if ``host`` is currently banned."""
        with self._lock:
            until = self._banned.get(host)
            if until is None:
                return False
            if until < time.time():
                del self._banned[host]
                return False
            return True

    def prune(self) -> None:
        """Drop addresses we have not heard from in a long time."""
        cutoff = time.time() - STALE_AFTER
        with self._lock:
            for key, entry in list(self._entries.items()):
                if entry.source != "seed" and entry.last_seen and entry.last_seen < cutoff:
                    del self._entries[key]

    # ------------------------------------------------------------------- queries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def all(self) -> list[AddressEntry]:
        """Return every known address."""
        with self._lock:
            return list(self._entries.values())

    def sample(self, count: int) -> list[AddressEntry]:
        """Return up to ``count`` random addresses, for gossiping."""
        entries = self.all()
        random.shuffle(entries)
        return entries[:count]

    def candidates(
        self, exclude: set[tuple[str, int]], *, retry_delay: float = 60.0
    ) -> list[AddressEntry]:
        """Return addresses worth trying now, most promising first."""
        now = time.time()
        options = [
            entry
            for entry in self.all()
            if entry.key not in exclude
            and not self.is_banned(entry.host)
            and now - entry.last_try >= retry_delay * (entry.failures + 1)
        ]
        options.sort(key=lambda entry: (entry.failures, -entry.last_seen))
        return options
