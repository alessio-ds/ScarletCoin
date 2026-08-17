"""The memory pool of unconfirmed anonymous transactions."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from scarletcoin.core.block import Block
from scarletcoin.core.chain import Blockchain
from scarletcoin.core.params import ChainParams
from scarletcoin.core.transaction import Transaction, TransactionError
from scarletcoin.core.utxo import Coin
from scarletcoin.core.validation import (
    MissingInputError,
    ValidationError,
    check_transaction_final,
    check_transaction_inputs,
)

__all__ = ["Mempool", "MempoolEntry", "MempoolError"]


class MempoolError(ValidationError):
    """Raised when a transaction cannot enter the pool."""


@dataclass(frozen=True, slots=True)
class MempoolEntry:
    """A transaction waiting to be mined."""

    transaction: Transaction
    txid: bytes
    fee: int
    size: int
    received: float

    @property
    def fee_rate(self) -> float:
        return self.fee * 1000 / self.size if self.size else 0.0

    def to_dict(self) -> dict:
        return {
            "txid": self.txid[::-1].hex(),
            "fee": self.fee,
            "size": self.size,
            "fee_rate": round(self.fee_rate, 3),
            "received": int(self.received),
        }


class _PoolView:
    """Coin view combining the chain's outputs with the pool's own outputs."""

    __slots__ = ("_chain", "_height", "_mempool")

    def __init__(self, mempool: Mempool, chain: Blockchain, height: int) -> None:
        self._mempool = mempool
        self._chain = chain
        self._height = height

    def get_coin(self, one_time_key: bytes) -> Coin | None:
        coin = self._mempool._outputs.get(bytes(one_time_key))
        if coin is not None:
            return coin
        return self._chain.get_coin(one_time_key)

    def has_key_image(self, key_image: bytes) -> bool:
        if bytes(key_image) in self._mempool._key_images:
            return True
        return self._chain.has_key_image(key_image)


class Mempool:
    """A fee-ordered set of unconfirmed transactions."""

    def __init__(
        self,
        chain: Blockchain,
        params: ChainParams,
        *,
        max_bytes: int = 5_000_000,
    ) -> None:
        self.chain = chain
        self.params = params
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._by_txid: dict[bytes, MempoolEntry] = {}
        self._outputs: dict[bytes, Coin] = {}
        self._output_owner: dict[bytes, bytes] = {}
        self._key_images: dict[bytes, bytes] = {}
        self._order: list[bytes] = []
        self._total_bytes = 0

    # ------------------------------------------------------------------ queries

    def __len__(self) -> int:
        return len(self._by_txid)

    def __contains__(self, txid: bytes) -> bool:
        return txid in self._by_txid

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def get(self, txid: bytes) -> Transaction | None:
        entry = self._by_txid.get(txid)
        return None if entry is None else entry.transaction

    def entries(self) -> list[MempoolEntry]:
        with self._lock:
            return sorted(
                self._by_txid.values(), key=lambda e: (-e.fee_rate, e.received, e.txid)
            )

    def txids(self) -> list[bytes]:
        with self._lock:
            return list(self._order)

    def coin_view(self, height: int | None = None) -> _PoolView:
        return _PoolView(self, self.chain, self.chain.height + 1 if height is None else height)

    # ------------------------------------------------------------------ mutation

    def add(self, transaction: Transaction) -> MempoolEntry:
        with self._lock:
            txid = transaction.txid()
            if txid in self._by_txid:
                raise MempoolError("transaction is already in the mempool")
            try:
                transaction.check_sanity()
            except TransactionError as exc:
                raise MempoolError(str(exc)) from exc
            if transaction.is_coinbase:
                raise MempoolError("a coinbase transaction cannot be relayed on its own")
            if self.chain.get_transaction(txid) is not None:
                raise MempoolError("transaction is already in a block")

            height = self.chain.height + 1
            if not check_transaction_final(transaction, height):
                raise MempoolError(f"transaction is locked until height {transaction.lock_time}")
            size = transaction.size()
            if size > self.params.max_block_size // 2:
                raise MempoolError("transaction is too large to be relayed")

            for txin in transaction.inputs:
                if self._key_images.get(txin.key_image) is not None:
                    raise MempoolError(
                        f"key image {txin.key_image.hex()} is already spent by another"
                        " mempool transaction"
                    )

            fee = check_transaction_inputs(
                transaction, self.coin_view(height), height=height, params=self.params
            )
            minimum = self.minimum_fee(size)
            if fee < minimum:
                raise MempoolError(
                    f"fee of {fee} scar is below the {minimum} scar minimum for {size} bytes"
                )

            entry = MempoolEntry(transaction, txid, fee, size, time.time())
            self._insert(entry)
            self._evict_if_needed()
            return entry

    def minimum_fee(self, size: int) -> int:
        return max(1, (size * self.params.min_relay_fee_per_kb + 999) // 1000)

    def _insert(self, entry: MempoolEntry) -> None:
        self._by_txid[entry.txid] = entry
        self._order.append(entry.txid)
        self._total_bytes += entry.size
        for txin in entry.transaction.inputs:
            self._key_images[txin.key_image] = entry.txid
        for _index, txout in enumerate(entry.transaction.outputs):
            self._outputs[txout.one_time_key] = Coin(
                txout.value, self.chain.height + 1, False
            )
            self._output_owner[txout.one_time_key] = entry.txid

    def remove(self, txid: bytes, *, with_descendants: bool = True) -> list[bytes]:
        with self._lock:
            removed: list[bytes] = []
            pending = [txid]
            while pending:
                current = pending.pop()
                entry = self._by_txid.pop(current, None)
                if entry is None:
                    continue
                removed.append(current)
                self._order.remove(current)
                self._total_bytes -= entry.size
                for txin in entry.transaction.inputs:
                    if self._key_images.get(txin.key_image) == current:
                        del self._key_images[txin.key_image]
                created = {txout.one_time_key for txout in entry.transaction.outputs}
                for txout in entry.transaction.outputs:
                    self._outputs.pop(txout.one_time_key, None)
                    self._output_owner.pop(txout.one_time_key, None)
                if with_descendants:
                    for other_txid, other in list(self._by_txid.items()):
                        if any(
                            member in created
                            for txin in other.transaction.inputs
                            for member in txin.ring
                        ):
                            pending.append(other_txid)
            return removed

    def clear(self) -> None:
        with self._lock:
            self._by_txid.clear()
            self._key_images.clear()
            self._outputs.clear()
            self._output_owner.clear()
            self._order.clear()
            self._total_bytes = 0

    def _output_tx(self, one_time_key: bytes) -> bytes | None:
        """Return the pooled txid that created ``one_time_key``, if any."""
        return self._output_owner.get(bytes(one_time_key))

    def _evict_if_needed(self) -> None:
        if self._total_bytes <= self.max_bytes:
            return
        for entry in sorted(self._by_txid.values(), key=lambda e: (e.fee_rate, -e.received)):
            if self._total_bytes <= self.max_bytes:
                return
            self.remove(entry.txid)

    # ------------------------------------------------------------ chain listener

    def block_connected(self, block: Block, height: int) -> None:
        with self._lock:
            for transaction in block.transactions:
                self.remove(transaction.txid(), with_descendants=False)
                for txin in transaction.inputs:
                    conflict = self._key_images.get(txin.key_image)
                    if conflict is not None:
                        self.remove(conflict)
            self._revalidate()

    def block_disconnected(self, block: Block, height: int) -> None:
        with self._lock:
            for transaction in block.transactions[1:]:
                try:
                    self.add(transaction)
                except ValidationError:
                    continue
            self._revalidate()

    def _revalidate(self) -> None:
        transactions = [self._by_txid[txid].transaction for txid in self._order]
        self.clear()
        for transaction in transactions:
            try:
                self.add(transaction)
            except (ValidationError, MissingInputError):
                continue

    # ------------------------------------------------------------ block template

    def collect(self, *, max_bytes: int, height: int) -> tuple[list[Transaction], int]:
        with self._lock:
            selected: list[Transaction] = []
            selected_ids: set[bytes] = set()
            total_fee = 0
            used = 0
            candidates = self.entries()
            while True:
                progressed = False
                for entry in list(candidates):
                    if used + entry.size > max_bytes:
                        continue
                    if not check_transaction_final(entry.transaction, height):
                        candidates.remove(entry)
                        continue
                    # A transaction must come after any pooled transaction that
                    # created one of its ring members' outputs.
                    parents = {
                        self._output_tx(member)
                        for txin in entry.transaction.inputs
                        for member in txin.ring
                    }
                    parents = {p for p in parents if p is not None and p in self._by_txid}
                    if not parents <= selected_ids:
                        continue
                    selected.append(entry.transaction)
                    selected_ids.add(entry.txid)
                    total_fee += entry.fee
                    used += entry.size
                    candidates.remove(entry)
                    progressed = True
                if not progressed:
                    return selected, total_fee

    def to_dict(self) -> dict:
        entries = self.entries()
        return {
            "count": len(entries),
            "bytes": self._total_bytes,
            "total_fee": sum(entry.fee for entry in entries),
            "transactions": [entry.to_dict() for entry in entries],
        }