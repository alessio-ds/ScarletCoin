"""The set of unspent transaction outputs.

A :class:`Coin` is one unspent output plus the context needed to validate a
spend of it (the height it was created at and whether it came from a coinbase,
which is subject to a maturity delay).

Two views over the coin set are defined:

:class:`CoinView`
    The read-only interface validation code needs.
:class:`CoinOverlay`
    A scratch layer on top of another view.  Blocks and mempool transactions are
    validated against an overlay, so nothing touches the database until the
    whole block (or transaction) has been accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scarletcoin.core.serialize import Reader, Writer
from scarletcoin.core.transaction import OutPoint, Transaction

__all__ = ["Coin", "CoinOverlay", "CoinView"]


@dataclass(frozen=True, slots=True)
class Coin:
    """An unspent transaction output."""

    value: int
    pubkey_hash: bytes
    height: int
    is_coinbase: bool

    def serialize(self) -> bytes:
        """Encode the coin (used for block undo data)."""
        writer = Writer()
        writer.uint64(self.value)
        writer.raw(self.pubkey_hash)
        writer.uint32(self.height)
        writer.uint8(1 if self.is_coinbase else 0)
        return writer.getvalue()

    @classmethod
    def read(cls, reader: Reader) -> Coin:
        """Decode a coin from ``reader``."""
        return cls(
            value=reader.uint64(),
            pubkey_hash=reader.raw(20),
            height=reader.uint32(),
            is_coinbase=bool(reader.uint8()),
        )

    def is_spendable_at(self, height: int, maturity: int) -> bool:
        """Return ``True`` if this coin may be spent in a block at ``height``."""
        if not self.is_coinbase:
            return True
        return height - self.height >= maturity


class CoinView(Protocol):
    """Read-only access to unspent outputs."""

    def get_coin(self, outpoint: OutPoint) -> Coin | None:
        """Return the unspent coin at ``outpoint``, or ``None`` if it does not exist."""
        ...


class CoinOverlay:
    """A pending set of additions and spends layered over another :class:`CoinView`."""

    __slots__ = ("_added", "_base", "_spent")

    def __init__(self, base: CoinView) -> None:
        self._base = base
        self._added: dict[OutPoint, Coin] = {}
        self._spent: set[OutPoint] = set()

    def get_coin(self, outpoint: OutPoint) -> Coin | None:
        """Return the coin at ``outpoint`` as seen through this overlay."""
        if outpoint in self._spent:
            return None
        coin = self._added.get(outpoint)
        if coin is not None:
            return coin
        return self._base.get_coin(outpoint)

    def spend(self, outpoint: OutPoint) -> Coin:
        """Mark ``outpoint`` as spent and return the coin it held.

        Raises:
            KeyError: if the outpoint is unknown or already spent.
        """
        coin = self.get_coin(outpoint)
        if coin is None:
            raise KeyError(f"no unspent output at {outpoint}")
        self._added.pop(outpoint, None)
        self._spent.add(outpoint)
        return coin

    def add(self, outpoint: OutPoint, coin: Coin) -> None:
        """Record a new unspent output."""
        self._spent.discard(outpoint)
        self._added[outpoint] = coin

    def add_transaction(self, transaction: Transaction, height: int) -> None:
        """Record every output created by ``transaction``."""
        txid = transaction.txid()
        is_coinbase = transaction.is_coinbase
        for index, output in enumerate(transaction.outputs):
            self.add(
                OutPoint(txid, index),
                Coin(output.value, output.pubkey_hash, height, is_coinbase),
            )

    @property
    def added(self) -> dict[OutPoint, Coin]:
        """Coins created by this overlay and not spent again inside it."""
        return dict(self._added)

    @property
    def spent(self) -> set[OutPoint]:
        """Outpoints this overlay consumed from the underlying view."""
        return set(self._spent)
