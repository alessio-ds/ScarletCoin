"""The memory pool of unconfirmed transactions.

The pool keeps transactions that are valid against the current chain tip but not
yet mined.  It also decides what a miner puts in the next block, ordered by fee
rate.

Two simplifications compared with Bitcoin, both deliberate and documented:

* there is no replace-by-fee — the first transaction to spend an output wins
  until it is mined or the chain reorganises;
* after any change to the active chain the whole pool is revalidated instead of
  surgically patched.  Pools of this size make the simpler code the better
  trade-off.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from scarletcoin.core.block import Block
from scarletcoin.core.chain import Blockchain
from scarletcoin.core.params import ChainParams
from scarletcoin.core.transaction import OutPoint, Transaction, TransactionError
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
        """Fee in scar per kilobyte."""
        return self.fee * 1000 / self.size if self.size else 0.0

    def to_dict(self) -> dict:
        """Return a JSON-friendly summary."""
        return {
            "txid": self.txid[::-1].hex(),
            "fee": self.fee,
            "size": self.size,
            "fee_rate": round(self.fee_rate, 3),
            "received": int(self.received),
        }


class _PoolView:
    """Coin view combining the chain's UTXO set with the pool's own outputs."""

    __slots__ = ("_chain", "_height", "_ignored", "_mempool")

    def __init__(
        self, mempool: Mempool, chain: Blockchain, height: int, ignored: frozenset = frozenset()
    ) -> None:
        self._mempool = mempool
        self._chain = chain
        self._height = height
        self._ignored = ignored

    def get_coin(self, outpoint: OutPoint) -> Coin | None:
        """Return the coin at ``outpoint``, or ``None`` if it is unavailable."""
        if outpoint in self._mempool._spent_by and outpoint not in self._ignored:
            return None
        entry = self._mempool._by_txid.get(outpoint.txid)
        if entry is not None:
            if outpoint.index >= len(entry.transaction.outputs):
                return None
            output = entry.transaction.outputs[outpoint.index]
            return Coin(output.value, output.type, output.payload, self._height, False)
        return self._chain.get_coin(outpoint)


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
        self._spent_by: dict[OutPoint, bytes] = {}
        self._order: list[bytes] = []
        self._total_bytes = 0

    # ------------------------------------------------------------------ queries

    def __len__(self) -> int:
        return len(self._by_txid)

    def __contains__(self, txid: bytes) -> bool:
        return txid in self._by_txid

    @property
    def total_bytes(self) -> int:
        """Serialised size of everything in the pool."""
        return self._total_bytes

    def is_spent(self, outpoint: OutPoint) -> bool:
        """Return ``True`` if a pooled transaction already spends ``outpoint``."""
        return outpoint in self._spent_by

    def get(self, txid: bytes) -> Transaction | None:
        """Return a pooled transaction by id."""
        entry = self._by_txid.get(txid)
        return None if entry is None else entry.transaction

    def entries(self) -> list[MempoolEntry]:
        """Return all entries, highest fee rate first."""
        with self._lock:
            return sorted(self._by_txid.values(), key=lambda e: (-e.fee_rate, e.received, e.txid))

    def txids(self) -> list[bytes]:
        """Return the ids of all pooled transactions, in arrival order."""
        with self._lock:
            return list(self._order)

    def coin_view(self, height: int | None = None, ignored: frozenset = frozenset()) -> _PoolView:
        """Return a coin view that also sees the pool's unconfirmed outputs.

        ``ignored`` outpoints are treated as unspent by the pool, which is what a
        replace-by-fee candidate needs: it spends the same outputs as the
        transaction it replaces.
        """
        return _PoolView(
            self, self.chain, self.chain.height + 1 if height is None else height, ignored
        )

    # ------------------------------------------------------------------ mutation

    def add(self, transaction: Transaction) -> MempoolEntry:
        """Validate ``transaction`` and add it to the pool.

        Returns:
            The new pool entry.

        Raises:
            MempoolError: if the transaction is already pooled, already mined,
                conflicts with a pooled transaction, pays too little, or is
                otherwise invalid.
            MissingInputError: if it spends outputs nobody knows about.
        """
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

            conflicts = {
                self._spent_by[txin.prevout]
                for txin in transaction.inputs
                if txin.prevout in self._spent_by
            }
            if conflicts:
                ignored = frozenset(txin.prevout for txin in transaction.inputs)
                fee = check_transaction_inputs(
                    transaction, self.coin_view(height, ignored), height=height, params=self.params
                )
                self._replace_conflicts(transaction, fee, size, conflicts)
            else:
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

    def _replace_conflicts(self, transaction: Transaction, fee: int, size: int, conflicts) -> None:
        """Replace pooled transactions this one double-spends, or refuse.

        Replace-by-fee: both the newcomer and every conflicting transaction must
        have signalled replaceability (every input sequence below
        ``SEQUENCE_FINAL - 1``), and the newcomer must pay a higher fee rate.
        """
        if not transaction.is_replaceable:
            owner = next(iter(conflicts))
            raise MempoolError(
                f"output is already spent by mempool transaction {owner[::-1].hex()};"
                " it is not replaceable"
            )
        for txid in conflicts:
            old = self._by_txid[txid]
            if not old.transaction.is_replaceable:
                raise MempoolError(
                    f"output is already spent by mempool transaction {txid[::-1].hex()};"
                    " it is not replaceable"
                )
            if fee * old.size <= old.fee * size:
                raise MempoolError(
                    f"replacement pays a lower fee rate than {txid[::-1].hex()}"
                )
        for txid in conflicts:
            self.remove(txid)

    def minimum_fee(self, size: int) -> int:
        """Return the smallest fee the node will relay for a transaction of ``size``."""
        return max(1, (size * self.params.min_relay_fee_per_kb + 999) // 1000)

    def estimate_fee_rate(self, blocks: int = 1) -> int:
        """Roughly estimate the fee rate (scar/kB) needed to confirm in ``blocks``.

        This is a simple percentile of the fee rates currently in the pool, not a
        prediction: for one block it aims at the 90th percentile, and each extra
        block of patience widens the window.  Falls back to the relay minimum
        when the pool is empty.
        """
        entries = self.entries()
        if not entries:
            return self.params.min_relay_fee_per_kb
        rates = sorted(entry.fee_rate for entry in entries)
        percentile = max(0.5, 1.0 - 0.1 * max(1, int(blocks)))
        index = min(len(rates) - 1, int(len(rates) * percentile))
        return max(self.params.min_relay_fee_per_kb, int(rates[index]))

    def _insert(self, entry: MempoolEntry) -> None:
        self._by_txid[entry.txid] = entry
        self._order.append(entry.txid)
        self._total_bytes += entry.size
        for txin in entry.transaction.inputs:
            self._spent_by[txin.prevout] = entry.txid

    def remove(self, txid: bytes, *, with_descendants: bool = True) -> list[bytes]:
        """Remove a transaction (and by default anything spending it).

        Returns:
            The ids that were removed.
        """
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
                    if self._spent_by.get(txin.prevout) == current:
                        del self._spent_by[txin.prevout]
                if with_descendants:
                    for index in range(len(entry.transaction.outputs)):
                        child = self._spent_by.get(OutPoint(current, index))
                        if child is not None:
                            pending.append(child)
            return removed

    def clear(self) -> None:
        """Drop every transaction."""
        with self._lock:
            self._by_txid.clear()
            self._spent_by.clear()
            self._order.clear()
            self._total_bytes = 0

    def _evict_if_needed(self) -> None:
        """Drop the cheapest transactions until the pool fits in ``max_bytes``."""
        if self._total_bytes <= self.max_bytes:
            return
        for entry in sorted(self._by_txid.values(), key=lambda e: (e.fee_rate, -e.received)):
            if self._total_bytes <= self.max_bytes:
                return
            self.remove(entry.txid)

    # ------------------------------------------------------------ chain listener

    def block_connected(self, block: Block, height: int) -> None:
        """Drop transactions that the new block confirmed or invalidated."""
        with self._lock:
            for transaction in block.transactions:
                self.remove(transaction.txid(), with_descendants=False)
                for txin in transaction.inputs:
                    conflict = self._spent_by.get(txin.prevout)
                    if conflict is not None:
                        self.remove(conflict)
            self._revalidate()

    def block_disconnected(self, block: Block, height: int) -> None:
        """Return a rolled-back block's transactions to the pool."""
        with self._lock:
            for transaction in block.transactions[1:]:
                try:
                    self.add(transaction)
                except ValidationError:
                    continue
            self._revalidate()

    def _revalidate(self) -> None:
        """Re-check every pooled transaction against the current chain state."""
        transactions = [self._by_txid[txid].transaction for txid in self._order]
        self.clear()
        for transaction in transactions:
            try:
                self.add(transaction)
            except (ValidationError, MissingInputError):
                continue

    # ------------------------------------------------------------ block template

    def collect(self, *, max_bytes: int, height: int) -> tuple[list[Transaction], int]:
        """Pick transactions for a new block at ``height``.

        Transactions are taken in fee-rate order, but a child is never included
        before its parent: the selection is repeated until no more transactions
        fit, so chains of unconfirmed transactions end up in the same block in
        dependency order.

        Returns:
            The selected transactions in dependency order and the total fee.
        """
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
                    parents = {
                        txin.prevout.txid
                        for txin in entry.transaction.inputs
                        if txin.prevout.txid in self._by_txid
                    }
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
        """Return a JSON-friendly summary of the pool."""
        entries = self.entries()
        return {
            "count": len(entries),
            "bytes": self._total_bytes,
            "total_fee": sum(entry.fee for entry in entries),
            "transactions": [entry.to_dict() for entry in entries],
        }
