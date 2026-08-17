"""The output set and the key-image double-spend index.

Every output is keyed by its *one-time public key* (33 bytes), not by an
outpoint. When an output is spent, its key image is recorded; the node cannot
tell *which* one-time key was spent, only that a particular key image has been
seen before.

:class:`Coin` — one unspent output.
:class:`CoinView` — read-only access to outputs.
:class:`CoinOverlay` — a scratch layer that also tracks key images.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scarletcoin.core.serialize import Reader, Writer

__all__ = ["Coin", "CoinOverlay", "CoinView"]


@dataclass(frozen=True, slots=True)
class Coin:
    """An output on the anonymous chain."""

    value: int
    height: int
    is_coinbase: bool

    def serialize(self) -> bytes:
        """Encode the coin (for block undo data)."""
        writer = Writer()
        writer.uint64(self.value)
        writer.uint32(self.height)
        writer.uint8(1 if self.is_coinbase else 0)
        return writer.getvalue()

    @classmethod
    def read(cls, reader: Reader) -> Coin:
        """Decode a coin from ``reader``."""
        return cls(
            value=reader.uint64(),
            height=reader.uint32(),
            is_coinbase=bool(reader.uint8()),
        )

    def is_spendable_at(self, height: int, maturity: int) -> bool:
        """Return ``True`` if this coin may be spent in a block at ``height``."""
        if not self.is_coinbase:
            return True
        return height - self.height >= maturity


class CoinView(Protocol):
    """Read-only access to outputs and key images."""

    def get_coin(self, one_time_key: bytes) -> Coin | None:
        """Return the output at ``one_time_key``, or ``None`` if it is unknown."""
        ...

    def has_key_image(self, key_image: bytes) -> bool:
        """Return ``True`` if ``key_image`` has already been spent."""
        ...


class CoinOverlay:
    """A pending set of outputs and key images layered over a :class:`CoinView`."""

    __slots__ = ("_added", "_base", "_key_images", "_removed")

    def __init__(self, base: CoinView) -> None:
        self._base = base
        self._added: dict[bytes, Coin] = {}
        self._removed: set[bytes] = set()
        self._key_images: set[bytes] = set()

    def get_coin(self, one_time_key: bytes) -> Coin | None:
        """Return the output at ``one_time_key`` as seen through this overlay."""
        if one_time_key in self._removed:
            return None
        coin = self._added.get(bytes(one_time_key))
        if coin is not None:
            return coin
        return self._base.get_coin(one_time_key)

    def has_key_image(self, key_image: bytes) -> bool:
        """Return ``True`` if ``key_image`` is spent."""
        if bytes(key_image) in self._key_images:
            return True
        return self._base.has_key_image(key_image)

    def add(self, one_time_key: bytes, coin: Coin) -> None:
        """Record a new output."""
        key = bytes(one_time_key)
        self._removed.discard(key)
        self._added[key] = coin

    def remove(self, one_time_key: bytes) -> Coin:
        """Mark ``one_time_key`` as removed and return its coin.

        Raises:
            KeyError: if the output is unknown.
        """
        key = bytes(one_time_key)
        coin = self.get_coin(key)
        if coin is None:
            raise KeyError(f"no output at {key.hex()}")
        self._added.pop(key, None)
        self._removed.add(key)
        return coin

    def spend(self, key_image: bytes) -> None:
        """Record a key image as spent.

        Raises:
            ValueError: if the key image is already spent.
        """
        ki = bytes(key_image)
        if self.has_key_image(ki):
            raise ValueError(f"key image {ki.hex()} is already spent")
        self._key_images.add(ki)

    def add_transaction(self, transaction, height: int) -> None:
        """Record every output created by ``transaction``."""
        is_coinbase = transaction.is_coinbase
        for txout in transaction.outputs:
            self.add(txout.one_time_key, Coin(txout.value, height, is_coinbase))

    @property
    def added(self) -> dict[bytes, Coin]:
        """Outputs created by this overlay."""
        return dict(self._added)

    @property
    def removed(self) -> set[bytes]:
        """One-time keys this overlay removed."""
        return set(self._removed)

    @property
    def key_images(self) -> set[bytes]:
        """Key images spent in this overlay."""
        return set(self._key_images)