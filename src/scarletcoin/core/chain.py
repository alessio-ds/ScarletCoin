"""The blockchain: block acceptance, the active chain and reorganisations.

:class:`Blockchain` owns the consensus state machine.  Blocks are validated in
three stages:

1. **Sanity** — proof of work, size, Merkle root, well-formed transactions.
   Requires no context (see :meth:`Block.check_sanity`).
2. **Context** — difficulty, timestamps and the height committed to by the
   coinbase.  Requires the parent block only, so it works on side branches too.
3. **Connection** — inputs, signatures, maturity, double spends and the block
   reward.  Requires the full UTXO state at the parent, so it happens when a
   block is actually spliced into the active chain.

The chain with the most cumulative proof of work always wins.  When a side
branch overtakes the tip, :meth:`Blockchain.add_block` rolls the active chain
back to the fork point and connects the new branch, inside a single database
transaction: if any block of the new branch turns out to be invalid, the whole
switch is rolled back and the old chain stays active.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from scarletcoin.core.block import Block, BlockError
from scarletcoin.core.coinbase import coinbase_height
from scarletcoin.core.params import ChainParams
from scarletcoin.core.pow import block_work, difficulty, next_bits
from scarletcoin.core.storage import (
    BlockIndexEntry,
    PruneResult,
    Storage,
    TxLocation,
    database_size,
)
from scarletcoin.core.transaction import OutPoint, Transaction, TransactionError
from scarletcoin.core.utxo import Coin, CoinOverlay
from scarletcoin.core.validation import (
    PrematureBlockError,
    ValidationError,
    check_transaction_final,
    check_transaction_inputs,
)
from scarletcoin.units import format_bytes

__all__ = [
    "MIN_PRUNE_KEEP",
    "AddBlockResult",
    "BlockStatus",
    "Blockchain",
    "ChainListener",
    "prune_database",
]

_MAX_CANDIDATES = 64
_MAX_REMEMBERED_INVALID = 5_000

#: Fewest recent blocks a pruned node keeps.
#:
#: Pruning throws away the undo data that a reorganisation needs, so the margin
#: has to be wider than any reorganisation the network could plausibly produce,
#: and wider than the coinbase maturity period so mined coins can still be
#: traced. At one block a minute this is two days of history.
MIN_PRUNE_KEEP = 2880


class BlockStatus(Enum):
    """Outcome of submitting a block to the chain."""

    CONNECTED = "connected"
    """The block became part of the active chain."""
    SIDE_BRANCH = "side-branch"
    """Valid, stored, but with less work than the current tip."""
    DUPLICATE = "duplicate"
    """Already known."""
    ORPHAN = "orphan"
    """Its parent is unknown, so it could not be validated yet."""
    PREMATURE = "premature"
    """Ahead of this machine's clock, so it cannot be judged yet.

    Not a verdict on the block: almost always this node's clock is wrong. Nothing
    is stored and nothing is remembered, so the block is accepted as soon as it is
    offered again with the clock put right.
    """
    INVALID = "invalid"
    """Broke a consensus rule."""


@dataclass(frozen=True, slots=True)
class AddBlockResult:
    """What happened to a submitted block."""

    status: BlockStatus
    block_hash: bytes
    height: int | None = None
    reason: str = ""
    reorganised: bool = False
    """``True`` if accepting the block rolled back one or more active blocks."""

    @property
    def accepted(self) -> bool:
        """``True`` if the block was stored (on the active chain or a branch)."""
        return self.status in (BlockStatus.CONNECTED, BlockStatus.SIDE_BRANCH)


class ChainListener(Protocol):
    """Callbacks fired after the active chain changes."""

    def block_connected(self, block: Block, height: int) -> None:
        """Called once a block has been added to the active chain."""
        ...

    def block_disconnected(self, block: Block, height: int) -> None:
        """Called once a block has been removed from the active chain."""
        ...


class _ConnectError(ValidationError):
    """Internal: a specific block failed connection-time validation."""

    def __init__(self, block_hash: bytes, message: str) -> None:
        super().__init__(message)
        self.block_hash = block_hash


class Blockchain:
    """The validated block tree plus the active chain and its UTXO set."""

    def __init__(self, storage: Storage, params: ChainParams) -> None:
        self.storage = storage
        self.params = params
        self._lock = threading.RLock()
        self._listeners: list[ChainListener] = []
        self._invalid: dict[bytes, str] = {}
        self._tip = self._load_or_create_genesis()

    # --------------------------------------------------------------- lifecycle

    def _load_or_create_genesis(self) -> BlockIndexEntry:
        tip_hash = self.storage.tip_hash
        if tip_hash is not None:
            entry = self.storage.get_entry(tip_hash)
            if entry is None:  # pragma: no cover - corrupt database
                raise ValidationError("database records a tip that is not stored")
            genesis = self.storage.get_chain_entry(0)
            if genesis is None or genesis.hash != self.params.genesis_hash:
                raise ValidationError(
                    f"this database belongs to a different network than {self.params.name}"
                )
            return entry

        genesis = self.params.genesis_block
        genesis.check_sanity(
            pow_limit=self.params.pow_limit, max_block_size=self.params.max_block_size
        )
        with self.storage.write():
            entry = self.storage.put_block(
                genesis, height=0, chainwork=block_work(genesis.header.bits), in_chain=True
            )
            self._apply_block_state(genesis, height=0, spent={})
            self.storage.put_undo(genesis.hash(), [])
            self.storage.set_tip(genesis.hash())
        return entry

    def add_listener(self, listener: ChainListener) -> None:
        """Register a listener for active-chain changes."""
        self._listeners.append(listener)

    # -------------------------------------------------------------- properties

    @property
    def tip(self) -> BlockIndexEntry:
        """The active chain tip."""
        return self._tip

    @property
    def height(self) -> int:
        """Height of the active chain tip."""
        return self._tip.height

    @property
    def tip_hash(self) -> bytes:
        """Hash of the active chain tip."""
        return self._tip.hash

    # ------------------------------------------------------------ coin lookups

    def get_coin(self, outpoint: OutPoint) -> Coin | None:
        """Return the unspent coin at ``outpoint`` (implements ``CoinView``)."""
        return self.storage.get_coin(outpoint)

    # ------------------------------------------------------------- block reads

    def get_entry(self, block_hash: bytes) -> BlockIndexEntry | None:
        """Return the index entry of a known block."""
        return self.storage.get_entry(block_hash)

    def get_block(self, block_hash: bytes) -> Block | None:
        """Return a known block."""
        return self.storage.get_block(block_hash)

    def get_entry_by_height(self, height: int) -> BlockIndexEntry | None:
        """Return the active-chain entry at ``height``."""
        return self.storage.get_chain_entry(height)

    def get_block_by_height(self, height: int) -> Block | None:
        """Return the active-chain block at ``height``."""
        entry = self.storage.get_chain_entry(height)
        return None if entry is None else self.storage.get_block(entry.hash)

    def has_block(self, block_hash: bytes) -> bool:
        """Return ``True`` if the block is known (valid or on a side branch)."""
        return self.storage.has_block(block_hash)

    def get_transaction(self, txid: bytes) -> tuple[Transaction, TxLocation] | None:
        """Look up a confirmed transaction by id."""
        location = self.storage.get_tx_location(txid)
        if location is None:
            return None
        block = self.storage.get_block(location.block_hash)
        if block is None or location.position >= len(block.transactions):
            return None
        return block.transactions[location.position], location

    def confirmations(self, height: int) -> int:
        """Number of confirmations of a transaction included at ``height``."""
        return max(0, self.height - height + 1)

    # ------------------------------------------------------- consensus context

    def median_time_past(self, entry: BlockIndexEntry) -> int:
        """Median timestamp of ``entry`` and its most recent ancestors."""
        timestamps: list[int] = []
        walker: BlockIndexEntry | None = entry
        for _ in range(self.params.median_time_blocks):
            if walker is None:
                break
            timestamps.append(walker.timestamp)
            walker = self.storage.get_entry(walker.prev_hash)
        return int(statistics.median_low(timestamps))

    def ancestor_at(self, entry: BlockIndexEntry, height: int) -> BlockIndexEntry | None:
        """Return the ancestor of ``entry`` at ``height`` (or ``entry`` itself)."""
        if height > entry.height or height < 0:
            return None
        if entry.in_chain:
            return self.storage.get_chain_entry(height)
        walker: BlockIndexEntry | None = entry
        while walker is not None and walker.height > height:
            if walker.in_chain:
                return self.storage.get_chain_entry(height)
            walker = self.storage.get_entry(walker.prev_hash)
        return walker

    def next_bits_after(self, parent: BlockIndexEntry) -> int:
        """Return the compact target required for the child of ``parent``."""
        height = parent.height + 1
        interval = self.params.retarget_interval
        if height % interval != 0:
            return parent.bits
        first = self.ancestor_at(parent, height - interval)
        if first is None:  # pragma: no cover - the chain always reaches genesis
            return parent.bits
        return next_bits(
            parent.bits,
            parent.timestamp - first.timestamp,
            target_timespan=self.params.target_timespan,
            pow_limit=self.params.pow_limit,
            max_adjustment_factor=self.params.max_adjustment_factor,
        )

    def next_bits(self) -> int:
        """Return the compact target the next block on the active chain must meet."""
        return self.next_bits_after(self._tip)

    def difficulty(self) -> float:
        """Current difficulty, relative to the easiest allowed target."""
        return difficulty(self._tip.bits, pow_limit=self.params.pow_limit)

    # -------------------------------------------------------------- block sync

    def locator(self) -> list[bytes]:
        """Return a block locator: recent hashes, then exponentially sparser ones.

        A peer uses it to find the most recent block we have in common.
        """
        hashes: list[bytes] = []
        height = self.height
        step = 1
        while height >= 0:
            entry = self.storage.get_chain_entry(height)
            if entry is not None:
                hashes.append(entry.hash)
            if len(hashes) >= 10:
                step *= 2
            height -= step
        if not hashes or hashes[-1] != self.params.genesis_hash:
            hashes.append(self.params.genesis_hash)
        return hashes

    def find_fork_height(self, locator: Iterable[bytes]) -> int:
        """Return the height of the newest active-chain block listed in ``locator``."""
        for block_hash in locator:
            entry = self.storage.get_entry(block_hash)
            if entry is not None and entry.in_chain:
                return entry.height
        return 0

    def active_hashes_after(self, height: int, limit: int) -> list[bytes]:
        """Return up to ``limit`` active-chain block hashes above ``height``."""
        hashes: list[bytes] = []
        for next_height in range(height + 1, min(self.height, height + limit) + 1):
            entry = self.storage.get_chain_entry(next_height)
            if entry is None:  # pragma: no cover - the active chain has no gaps
                break
            hashes.append(entry.hash)
        return hashes

    # --------------------------------------------------------- block acceptance

    def add_block(self, block: Block) -> AddBlockResult:
        """Validate and store ``block``, activating the best chain afterwards.

        This never raises for a bad block: the outcome is reported in the
        returned :class:`AddBlockResult`.
        """
        block_hash = block.hash()
        with self._lock:
            if block_hash in self._invalid:
                return AddBlockResult(
                    BlockStatus.INVALID, block_hash, reason=self._invalid[block_hash]
                )
            if self.storage.has_block(block_hash):
                entry = self.storage.get_entry(block_hash)
                height = None if entry is None else entry.height
                return AddBlockResult(BlockStatus.DUPLICATE, block_hash, height)

            try:
                block.check_sanity(
                    pow_limit=self.params.pow_limit, max_block_size=self.params.max_block_size
                )
            except (BlockError, TransactionError) as exc:
                self._invalid[block_hash] = str(exc)
                return AddBlockResult(BlockStatus.INVALID, block_hash, reason=str(exc))

            parent = self.storage.get_entry(block.header.prev_hash)
            if parent is None:
                return AddBlockResult(
                    BlockStatus.ORPHAN, block_hash, reason="parent block is unknown"
                )
            if parent.hash in self._invalid:
                reason = "the parent block is invalid"
                self._invalid[block_hash] = reason
                return AddBlockResult(BlockStatus.INVALID, block_hash, reason=reason)

            height = parent.height + 1
            try:
                self._check_context(block, parent, height)
            except PrematureBlockError as exc:
                # Deliberately not cached and not stored. The block is fine; this
                # machine's clock is behind the network's. Remembering it as
                # invalid would mean refusing it for the rest of the process even
                # after the clock was fixed.
                return AddBlockResult(BlockStatus.PREMATURE, block_hash, height, reason=str(exc))
            except ValidationError as exc:
                self._invalid[block_hash] = str(exc)
                return AddBlockResult(BlockStatus.INVALID, block_hash, height, reason=str(exc))

            chainwork = parent.chainwork + block_work(block.header.bits)
            with self.storage.write():
                self.storage.put_block(block, height=height, chainwork=chainwork)

            reorganised = self._activate_best_chain()
            if block_hash in self._invalid:
                return AddBlockResult(
                    BlockStatus.INVALID, block_hash, height, reason=self._invalid[block_hash]
                )
            entry = self.storage.get_entry(block_hash)
            status = (
                BlockStatus.CONNECTED
                if entry is not None and entry.in_chain
                else BlockStatus.SIDE_BRANCH
            )
            return AddBlockResult(status, block_hash, height, reorganised=reorganised)

    def _check_context(self, block: Block, parent: BlockIndexEntry, height: int) -> None:
        """Validate a block against its parent (difficulty, time, height)."""
        expected = self.next_bits_after(parent)
        if block.header.bits != expected:
            raise ValidationError(
                f"wrong difficulty: header says {block.header.bits:#010x},"
                f" the chain requires {expected:#010x}"
            )
        median = self.median_time_past(parent)
        if block.header.timestamp <= median:
            raise ValidationError(
                f"timestamp {block.header.timestamp} is not newer than the median"
                f" of the last {self.params.median_time_blocks} blocks ({median})"
            )
        limit = int(time.time()) + self.params.max_future_time
        if block.header.timestamp > limit:
            raise PrematureBlockError(
                f"timestamp {block.header.timestamp} is more than"
                f" {self.params.max_future_time // 3600}h ahead of this machine's clock"
                f" (now {int(time.time())}); check the clock on this machine"
            )
        try:
            claimed = coinbase_height(block.coinbase)
        except (TransactionError, BlockError) as exc:
            raise ValidationError(str(exc)) from exc
        if claimed != height:
            raise ValidationError(
                f"coinbase claims height {claimed} but the block is at height {height}"
            )

    # ------------------------------------------------------ chain reorganisation

    def _activate_best_chain(self) -> bool:
        """Switch to the stored chain with the most work.  Returns ``True`` on a reorg."""
        reorganised = False
        while True:
            candidate = self._best_candidate()
            if candidate is None:
                return reorganised
            try:
                with self.storage.write():
                    new_tip, events = self._switch_to(candidate)
            except _ConnectError as exc:
                self._invalidate(exc.block_hash, str(exc))
                continue
            self._tip = new_tip
            if any(kind == "disconnect" for kind, _, _ in events):
                reorganised = True
            self._notify(events)

    def _best_candidate(self) -> BlockIndexEntry | None:
        """Return the stored block with the most work, if it beats the current tip."""
        for entry in self.storage.best_entries(_MAX_CANDIDATES):
            if entry.chainwork <= self._tip.chainwork:
                return None
            if entry.hash in self._invalid:
                continue
            return entry
        return None

    def _invalidate(self, block_hash: bytes, reason: str) -> None:
        """Mark a block and everything built on top of it as unusable."""
        if len(self._invalid) > _MAX_REMEMBERED_INVALID:
            self._invalid.clear()
        pending = [(block_hash, reason)]
        while pending:
            current, why = pending.pop()
            if current in self._invalid:
                continue
            self._invalid[current] = why
            pending.extend(
                (child.hash, f"builds on an invalid block: {why}")
                for child in self.storage.children_of(current)
            )

    def _switch_to(
        self, candidate: BlockIndexEntry
    ) -> tuple[BlockIndexEntry, list[tuple[str, Block, int]]]:
        """Make ``candidate`` the tip, rolling back the active chain if needed."""
        branch: list[BlockIndexEntry] = []
        walker = candidate
        while not walker.in_chain:
            if walker.hash in self._invalid:
                raise _ConnectError(walker.hash, "branch contains an invalid block")
            branch.append(walker)
            parent = self.storage.get_entry(walker.prev_hash)
            if parent is None:  # pragma: no cover - parents are always stored
                raise _ConnectError(walker.hash, "branch is missing its parent")
            walker = parent
        fork = walker
        branch.reverse()

        events: list[tuple[str, Block, int]] = []
        tip = self._tip
        while tip.hash != fork.hash:
            block = self._disconnect_block(tip)
            events.append(("disconnect", block, tip.height))
            parent = self.storage.get_entry(tip.prev_hash)
            if parent is None:  # pragma: no cover - parents are always stored
                raise _ConnectError(tip.hash, "active chain is missing a parent block")
            tip = parent

        for entry in branch:
            block = self.storage.get_block(entry.hash)
            if block is None:  # pragma: no cover - index and blocks are written together
                raise _ConnectError(entry.hash, "block data is missing")
            self._connect_block(block, entry)
            events.append(("connect", block, entry.height))
            tip = replace(entry, in_chain=True)

        self.storage.set_tip(tip.hash)
        return tip, events

    def _connect_block(self, block: Block, entry: BlockIndexEntry) -> None:
        """Validate ``block`` against the current UTXO set and apply it.

        Raises:
            _ConnectError: if the block cannot be connected.
        """
        height = entry.height
        overlay = CoinOverlay(self.storage)
        spent: dict[OutPoint, Coin] = {}
        undo: list[tuple[OutPoint, Coin]] = []
        created: set[OutPoint] = set()
        fees = 0

        for transaction in block.transactions:
            if not check_transaction_final(transaction, height):
                raise _ConnectError(
                    entry.hash,
                    f"transaction {transaction.txid_hex()} is locked until"
                    f" height {transaction.lock_time}",
                )
            if not transaction.is_coinbase:
                try:
                    fees += check_transaction_inputs(
                        transaction, overlay, height=height, params=self.params
                    )
                except ValidationError as exc:
                    raise _ConnectError(
                        entry.hash, f"transaction {transaction.txid_hex()}: {exc}"
                    ) from exc
                for txin in transaction.inputs:
                    coin = overlay.spend(txin.prevout)
                    spent[txin.prevout] = coin
                    if txin.prevout not in created:
                        undo.append((txin.prevout, coin))
            txid = transaction.txid()
            created.update(OutPoint(txid, index) for index in range(len(transaction.outputs)))
            overlay.add_transaction(transaction, height)

        subsidy = self.params.subsidy(height)
        reward = block.coinbase.total_output()
        if reward > subsidy + fees:
            raise _ConnectError(
                entry.hash,
                f"coinbase pays {reward} scar but only {subsidy + fees} is available"
                f" (subsidy {subsidy} + fees {fees})",
            )

        for outpoint in overlay.spent:
            self.storage.remove_coin(outpoint)
        for outpoint, coin in overlay.added.items():
            self.storage.add_coin(outpoint, coin)
        self.storage.put_undo(entry.hash, undo)
        self._index_block(block, height=height, spent=spent)
        self.storage.set_in_chain(entry.hash, True)

    def _apply_block_state(self, block: Block, *, height: int, spent: dict[OutPoint, Coin]) -> None:
        """Add every output of ``block`` to the UTXO set and index it (genesis path)."""
        for transaction in block.transactions:
            txid = transaction.txid()
            for index, output in enumerate(transaction.outputs):
                self.storage.add_coin(
                    OutPoint(txid, index),
                    Coin(output.value, output.pubkey_hash, height, transaction.is_coinbase),
                )
        self._index_block(block, height=height, spent=spent)

    def _index_block(self, block: Block, *, height: int, spent: dict[OutPoint, Coin]) -> None:
        """Update the transaction and address indexes for a connected block."""
        block_hash = block.hash()
        for position, transaction in enumerate(block.transactions):
            touched = {output.pubkey_hash for output in transaction.outputs}
            for txin in transaction.inputs:
                coin = spent.get(txin.prevout)
                if coin is not None:
                    touched.add(coin.pubkey_hash)
            self.storage.index_transaction(
                transaction,
                block_hash=block_hash,
                position=position,
                height=height,
                pubkey_hashes=touched,
            )

    def _disconnect_block(self, entry: BlockIndexEntry) -> Block:
        """Remove ``entry`` from the active chain and restore the coins it spent."""
        block = self.storage.get_block(entry.hash)
        if block is None:  # pragma: no cover - index and blocks are written together
            raise _ConnectError(entry.hash, "cannot disconnect a block whose data is missing")
        for transaction in block.transactions:
            txid = transaction.txid()
            for index in range(len(transaction.outputs)):
                self.storage.remove_coin(OutPoint(txid, index))
            self.storage.unindex_transaction(txid)
        for outpoint, coin in self.storage.get_undo(entry.hash):
            self.storage.add_coin(outpoint, coin)
        self.storage.set_in_chain(entry.hash, False)
        return block

    def _notify(self, events: list[tuple[str, Block, int]]) -> None:
        for kind, block, height in events:
            for listener in self._listeners:
                if kind == "connect":
                    listener.block_connected(block, height)
                else:
                    listener.block_disconnected(block, height)

    # --------------------------------------------------------------- statistics

    def total_supply(self) -> int:
        """Total value of all unspent outputs, in scar."""
        return self.storage.utxo_stats()[1]

    # ------------------------------------------------------------------ pruning

    @property
    def min_prune_keep(self) -> int:
        """Fewest recent blocks this network allows a pruned node to keep."""
        if self.params.name == "regtest":
            return max(2, self.params.coinbase_maturity)
        return MIN_PRUNE_KEEP

    @property
    def prune_height(self) -> int:
        """Highest height whose block bodies have been dropped (0 if none)."""
        return self.storage.prune_height

    def prune(self, keep_blocks: int, *, vacuum: bool = False) -> PruneResult:
        """Throw away the bodies of all but the last ``keep_blocks`` blocks.

        Headers, the UTXO set and therefore every balance are kept, so a pruned
        node still validates new blocks exactly as strictly as a full one. What
        it can no longer do is show old blocks and transactions, serve them to a
        peer that is syncing from scratch, or reorganise past the horizon.

        Args:
            keep_blocks: How many recent blocks to keep whole. Raised to
                :attr:`min_prune_keep` if it is smaller.
            vacuum: Rebuild the database afterwards so the freed space is
                actually returned to the filesystem. Slow on a big chain, and it
                needs room for a second copy while it runs.

        Returns:
            What was pruned; ``blocks`` is 0 when there was nothing to do.
        """
        with self._lock:
            keep = max(self.min_prune_keep, int(keep_blocks))
            horizon = self.height - keep
            if horizon < 1:
                return PruneResult(0, 0, 0, self.storage.prune_height)
            with self.storage.write():
                result = self.storage.prune_to(horizon)
        if vacuum and result.blocks:
            self.storage.vacuum()
        return result

    def network_stats(self, window: int | None = None) -> dict:
        """Measure how fast the chain is actually moving.

        Everything here is observed, not claimed: the pace comes from the
        timestamps of the last ``window`` blocks, and the hash rate from the work
        those blocks added divided by the time they took.

        Args:
            window: How many recent blocks to average over. Defaults to one
                retargeting period.

        Returns:
            A JSON-friendly dictionary. Fields that cannot be measured yet (an
            empty or one-block chain) are ``None``.
        """
        params = self.params
        window = max(1, window or params.retarget_interval)
        tip = self._tip
        now = int(time.time())

        first_height = max(0, tip.height - window)
        first = self.storage.get_chain_entry(first_height)
        blocks = tip.height - first_height
        seconds = (tip.timestamp - first.timestamp) if first is not None else 0

        spacing: float | None = None
        hash_rate: float | None = None
        if first is not None and blocks > 0 and seconds > 0:
            spacing = seconds / blocks
            hash_rate = (tip.chainwork - first.chainwork) / seconds

        interval = params.retarget_interval
        next_retarget = (tip.height // interval + 1) * interval
        estimated_bits = tip.bits
        if spacing is not None:
            # If the observed pace held for a whole period, this is where the
            # target would land.
            estimated_bits = next_bits(
                tip.bits,
                int(spacing * interval),
                target_timespan=params.target_timespan,
                pow_limit=params.pow_limit,
                max_adjustment_factor=params.max_adjustment_factor,
            )

        current_difficulty = difficulty(tip.bits, pow_limit=params.pow_limit)
        estimated_difficulty = difficulty(estimated_bits, pow_limit=params.pow_limit)
        return {
            "height": tip.height,
            "window": blocks,
            "window_seconds": seconds,
            "target_spacing": params.target_spacing,
            "average_spacing": None if spacing is None else round(spacing, 2),
            "hash_rate": None if hash_rate is None else round(hash_rate, 2),
            "difficulty": current_difficulty,
            "blocks_last_hour": self.storage.count_blocks_since(now - 3600),
            "blocks_last_day": self.storage.count_blocks_since(now - 86400),
            "seconds_since_last_block": max(0, now - tip.timestamp),
            "median_time": self.median_time_past(tip),
            "next_retarget_height": next_retarget,
            "blocks_until_retarget": next_retarget - tip.height,
            "estimated_next_bits": f"{estimated_bits:#010x}",
            "estimated_next_difficulty": estimated_difficulty,
            "estimated_difficulty_change": (
                None
                if not current_difficulty
                else round((estimated_difficulty / current_difficulty - 1) * 100, 2)
            ),
        }

    def hashrate_history(self, window: int | None = None, points: int = 240) -> list[dict]:
        """Sample the network hashrate across the chain's history.

        The hashrate at each sample height is the work added by the previous
        ``window`` blocks divided by the time they took — the same measure
        :meth:`network_stats` reports, but evaluated at regular intervals back
        through the chain instead of only at the tip.  Each sample also carries
        the difficulty at that height, so the two can be read together.

        Args:
            window: How many blocks each sample averages over.  Defaults to one
                retargeting period.
            points: Upper bound on the number of samples returned; the chain is
                sampled at evenly spaced heights, so a short chain simply returns
                one sample per height.

        Returns:
            A list of ``{"height", "time", "hash_rate", "difficulty"}`` records,
            oldest first.  Empty when the chain is shorter than ``window + 1``.
            The genesis block is never part of a sample: its timestamp is a
            nominal value, not the moment it was mined, so measuring hashrate
            across it would be meaningless.
        """
        params = self.params
        window = max(2, window or params.retarget_interval)
        points = max(1, int(points))
        tip = self._tip
        if tip.height < window + 1:
            return []

        step = max(1, (tip.height - window) // points)
        history: list[dict] = []
        height = window + 1
        while height <= tip.height:
            entry = self.storage.get_chain_entry(height)
            first = self.storage.get_chain_entry(height - window)
            if entry is not None and first is not None and entry.timestamp > first.timestamp:
                history.append(
                    {
                        "height": height,
                        "time": entry.timestamp,
                        "hash_rate": round(
                            (entry.chainwork - first.chainwork)
                            / (entry.timestamp - first.timestamp),
                            2,
                        ),
                        "difficulty": difficulty(entry.bits, pow_limit=params.pow_limit),
                    }
                )
            if height >= tip.height:
                break
            height = min(tip.height, height + step)
        return history

    def stats(self) -> dict:
        """Return a summary of the chain, for RPC and the explorer."""
        utxo_count, supply = self.storage.utxo_stats()
        sizes = self.storage.size_stats()
        return {
            "network": self.params.name,
            "height": self.height,
            "tip": self._tip.hash[::-1].hex(),
            "tip_time": self._tip.timestamp,
            "median_time": self.median_time_past(self._tip),
            "bits": f"{self._tip.bits:#010x}",
            "next_bits": f"{self.next_bits():#010x}",
            "difficulty": self.difficulty(),
            "chainwork": self._tip.chainwork,
            "blocks_stored": sizes["blocks"],
            "utxo_count": utxo_count,
            "supply": supply,
            # How big the chain is. ``chain_bytes`` is the serialised active
            # chain; ``disk_bytes`` is what the node's database actually costs,
            # indexes and all. The pre-formatted strings are here so every front
            # end spells a size the same way.
            "chain_bytes": sizes["chain_bytes"],
            "chain_size": format_bytes(sizes["chain_bytes"]),
            "disk_bytes": sizes["disk_bytes"],
            "disk_size": format_bytes(sizes["disk_bytes"]),
            "average_block_bytes": sizes["average_block_bytes"],
            "pruned_blocks": sizes["pruned_blocks"],
            "prune_height": sizes["prune_height"],
        }


def prune_database(
    path: str | Path,
    params: ChainParams,
    keep_blocks: int,
    *,
    vacuum: bool = True,
) -> tuple[PruneResult, int]:
    """Prune a chain database that no node currently has open.

    The offline counterpart of :meth:`Blockchain.prune`, used by
    ``scarlet-node prune`` and by the desktop applications before they start a
    node.  Opening the database read-write while a node is running would fight
    that node for the write lock, so callers check first.

    Returns:
        What was pruned, and how many bytes the file occupies afterwards.
    """
    storage = Storage(path)
    try:
        chain = Blockchain(storage, params)
        result = chain.prune(keep_blocks)
        if vacuum and result.blocks:
            storage.vacuum()
        return result, database_size(storage.path)
    finally:
        storage.close()
