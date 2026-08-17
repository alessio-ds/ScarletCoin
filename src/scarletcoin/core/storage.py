"""Persistent storage for the block index, the UTXO set and the lookup indexes.

Everything lives in a single SQLite database, which gives us atomic multi-table
updates for free: connecting or disconnecting a block either happens completely
or not at all.  The schema is:

``blocks``
    Every block we have ever validated, whether or not it is on the active
    chain, with its height and cumulative proof of work.  A pruned block keeps
    its 80-byte header and loses its body.
``utxo``
    The current set of unspent outputs (active chain only).
``undo``
    For each block, the coins it consumed, so a reorganisation can put them back.
``tx_location`` / ``address_history``
    Indexes used by the RPC interface, the explorer and the wallet.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from scarletcoin.core.block import Block, BlockHeader
from scarletcoin.core.serialize import Reader, Writer
from scarletcoin.core.transaction import OutPoint, Transaction
from scarletcoin.core.utxo import Coin

__all__ = [
    "BlockIndexEntry",
    "HeaderEntry",
    "PruneResult",
    "Storage",
    "StorageError",
    "TxLocation",
    "database_files",
    "database_size",
    "inspect_database",
]

#: 1: the original schema.  2: ``blocks.pruned``, so a body can be dropped while
#: the header stays.  3: the output rewrite (P2SH): the UTXO set gained a type
#: and the serialisation changed, so the whole database is rebuilt.
SCHEMA_VERSION = 3

#: How long :meth:`Storage.size_stats` may reuse its last measurement.
SIZE_CACHE_SECONDS = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    hash      BLOB PRIMARY KEY,
    height    INTEGER NOT NULL,
    prev_hash BLOB NOT NULL,
    chainwork BLOB NOT NULL,
    in_chain  INTEGER NOT NULL DEFAULT 0,
    timestamp INTEGER NOT NULL,
    raw       BLOB NOT NULL,
    pruned    INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS blocks_prev ON blocks (prev_hash);
CREATE INDEX IF NOT EXISTS blocks_chain ON blocks (in_chain, height);
CREATE INDEX IF NOT EXISTS blocks_work ON blocks (chainwork DESC);

CREATE TABLE IF NOT EXISTS utxo (
    txid        BLOB NOT NULL,
    idx         INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    type        INTEGER NOT NULL,
    payload     BLOB NOT NULL,
    height      INTEGER NOT NULL,
    coinbase    INTEGER NOT NULL,
    PRIMARY KEY (txid, idx)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS utxo_address ON utxo (payload);

CREATE TABLE IF NOT EXISTS undo (
    block_hash BLOB PRIMARY KEY,
    data       BLOB NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS headers (
    hash      BLOB PRIMARY KEY,
    height    INTEGER NOT NULL,
    prev_hash BLOB NOT NULL,
    chainwork BLOB NOT NULL,
    raw       BLOB NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS headers_height ON headers (height);
CREATE INDEX IF NOT EXISTS headers_work ON headers (chainwork DESC);

CREATE TABLE IF NOT EXISTS tx_location (
    txid       BLOB PRIMARY KEY,
    block_hash BLOB NOT NULL,
    position   INTEGER NOT NULL,
    height     INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS address_history (
    pubkey_hash BLOB NOT NULL,
    txid        BLOB NOT NULL,
    height      INTEGER NOT NULL,
    PRIMARY KEY (pubkey_hash, txid)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS address_history_txid ON address_history (txid);
"""


class StorageError(RuntimeError):
    """Raised when the database is unusable or was written by another version."""


@dataclass(frozen=True, slots=True)
class BlockIndexEntry:
    """Metadata about a known block."""

    hash: bytes
    height: int
    prev_hash: bytes
    chainwork: int
    in_chain: bool
    header: BlockHeader
    pruned: bool = False
    """``True`` when only the header of this block is still stored."""

    @property
    def timestamp(self) -> int:
        """The block's timestamp."""
        return self.header.timestamp

    @property
    def bits(self) -> int:
        """The block's compact target."""
        return self.header.bits


@dataclass(frozen=True, slots=True)
class TxLocation:
    """Where a confirmed transaction lives."""

    block_hash: bytes
    position: int
    height: int


@dataclass(frozen=True, slots=True)
class HeaderEntry:
    """A validated block header whose body has not been downloaded yet."""

    hash: bytes
    height: int
    prev_hash: bytes
    chainwork: int
    header: BlockHeader


@dataclass(frozen=True, slots=True)
class PruneResult:
    """What a call to :meth:`Storage.prune_to` did."""

    blocks: int
    """How many block bodies were dropped."""
    transactions: int
    """How many transactions were removed from the lookup indexes."""
    freed_bytes: int
    """Bytes of block bodies and undo data that stopped being stored."""
    prune_height: int
    """Everything at or below this height has been pruned."""


def database_files(path: str | Path) -> list[Path]:
    """Return every file SQLite keeps for the database at ``path``.

    In write-ahead-log mode the real size of a database is the main file plus its
    log and shared-memory companions, which can be a large fraction of the total
    just after a burst of blocks.
    """
    main = Path(path)
    return [main, Path(f"{main}-wal"), Path(f"{main}-shm")]


def database_size(path: str | Path) -> int:
    """Total number of bytes the database at ``path`` occupies on disk."""
    total = 0
    for candidate in database_files(path):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def inspect_database(path: str | Path) -> dict:
    """Summarise a chain database without opening it for writing.

    Used before a node is started, to tell somebody how much disk the chain they
    already have takes up.  It never migrates, never creates and never locks the
    database for writing, so it is safe to call while a node is running.

    Returns:
        A dictionary that always has ``exists`` and ``disk_bytes``; the remaining
        keys are ``None`` when the database could not be read.
    """
    target = Path(path)
    summary: dict = {
        "path": str(target),
        "exists": target.exists(),
        "disk_bytes": database_size(target),
        "height": None,
        "blocks": None,
        "chain_bytes": None,
        "block_bytes": None,
        "pruned_blocks": None,
        "prune_height": None,
        "error": "",
    }
    if not summary["exists"]:
        return summary
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(blocks)")}
        pruned = "SUM(pruned)" if "pruned" in columns else "0"
        row = connection.execute(
            "SELECT COUNT(*) AS blocks,"
            " COALESCE(MAX(CASE WHEN in_chain = 1 THEN height END), 0) AS height,"
            " COALESCE(SUM(LENGTH(raw)), 0) AS block_bytes,"
            " COALESCE(SUM(CASE WHEN in_chain = 1 THEN LENGTH(raw) ELSE 0 END), 0) AS chain_bytes,"
            f" COALESCE({pruned}, 0) AS pruned_blocks"
            " FROM blocks"
        ).fetchone()
        summary.update(
            {
                "blocks": int(row["blocks"]),
                "height": int(row["height"]),
                "block_bytes": int(row["block_bytes"]),
                "chain_bytes": int(row["chain_bytes"]),
                "pruned_blocks": int(row["pruned_blocks"]),
            }
        )
        marker = connection.execute("SELECT value FROM meta WHERE key = 'prune_height'").fetchone()
        if marker is not None:
            summary["prune_height"] = int(bytes(marker["value"]))
    except (sqlite3.Error, OSError, ValueError) as exc:
        summary["error"] = str(exc)
    finally:
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()
    return summary


def _work_to_blob(work: int) -> bytes:
    if work < 0:
        raise StorageError("cumulative work must not be negative")
    return work.to_bytes(32, "big")


def _blob_to_work(blob: bytes) -> int:
    return int.from_bytes(blob, "big")


class Storage:
    """A thread-safe SQLite-backed store for chain data."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self._size_cache: dict | None = None
        self._size_measured_at = 0.0
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()

    # ------------------------------------------------------------------ set-up

    def _configure(self) -> None:
        cursor = self._connection
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.executescript(_SCHEMA)
        stored = self.get_meta("schema_version")
        if stored is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION).encode())
            return
        version = int(stored)
        if version > SCHEMA_VERSION:
            raise StorageError(
                f"database {self.path} uses schema version {version},"
                f" this build understands {SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION:
            self._migrate(version)

    def _migrate(self, from_version: int) -> None:
        """Bring an older database up to :data:`SCHEMA_VERSION`.

        The schema 3 rewrite changes the serialisation of blocks, transactions
        and coins, so databases from before it are not migrated: every table is
        dropped and the chain is rebuilt from the new genesis.  This is the
        hard-fork reset.
        """
        if from_version < 3:
            for table in ("blocks", "utxo", "undo", "tx_location", "address_history", "meta"):
                self._execute(f"DROP TABLE IF EXISTS {table}")
            self._connection.executescript(_SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION).encode())

    def close(self) -> None:
        """Flush and close the database."""
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ transactions

    @contextmanager
    def write(self) -> Iterator[None]:
        """Run a block of updates inside one immediate SQLite transaction.

        Nested uses join the outermost transaction.
        """
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._connection.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._connection.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._connection.execute("COMMIT")

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, params).fetchone()

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._connection.execute(sql, params)

    # -------------------------------------------------------------------- meta

    def get_meta(self, key: str) -> bytes | None:
        """Read a metadata value."""
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else bytes(row["value"])

    def set_meta(self, key: str, value: bytes) -> None:
        """Write a metadata value."""
        self._execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def tip_hash(self) -> bytes | None:
        """Hash of the active chain tip, if the chain has been initialised."""
        return self.get_meta("tip")

    def set_tip(self, block_hash: bytes) -> None:
        """Record the active chain tip."""
        self.set_meta("tip", block_hash)

    # ------------------------------------------------------------- block index

    @staticmethod
    def _entry(row: sqlite3.Row) -> BlockIndexEntry:
        raw = bytes(row["raw"])
        return BlockIndexEntry(
            hash=bytes(row["hash"]),
            height=int(row["height"]),
            prev_hash=bytes(row["prev_hash"]),
            chainwork=_blob_to_work(bytes(row["chainwork"])),
            in_chain=bool(row["in_chain"]),
            header=BlockHeader.deserialize(raw[:80]),
            pruned=bool(row["pruned"]),
        )

    def put_block(
        self, block: Block, *, height: int, chainwork: int, in_chain: bool = False
    ) -> BlockIndexEntry:
        """Store a validated block and its index entry."""
        block_hash = block.hash()
        self._execute(
            "INSERT OR REPLACE INTO blocks"
            " (hash, height, prev_hash, chainwork, in_chain, timestamp, raw, pruned)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                block_hash,
                height,
                block.header.prev_hash,
                _work_to_blob(chainwork),
                1 if in_chain else 0,
                block.header.timestamp,
                block.serialize(),
            ),
        )
        self._forget_sizes()
        return BlockIndexEntry(
            hash=block_hash,
            height=height,
            prev_hash=block.header.prev_hash,
            chainwork=chainwork,
            in_chain=in_chain,
            header=block.header,
        )

    def has_block(self, block_hash: bytes) -> bool:
        """Return ``True`` if the block is already stored."""
        return self._one("SELECT 1 FROM blocks WHERE hash = ?", (block_hash,)) is not None

    def get_entry(self, block_hash: bytes) -> BlockIndexEntry | None:
        """Return the index entry for ``block_hash``."""
        row = self._one("SELECT * FROM blocks WHERE hash = ?", (block_hash,))
        return None if row is None else self._entry(row)

    def get_block(self, block_hash: bytes) -> Block | None:
        """Return a stored block, or ``None`` if it is unknown or pruned."""
        row = self._one("SELECT raw, pruned FROM blocks WHERE hash = ?", (block_hash,))
        if row is None or row["pruned"]:
            return None
        return Block.deserialize(bytes(row["raw"]))

    def set_in_chain(self, block_hash: bytes, in_chain: bool) -> None:
        """Mark a block as being on (or off) the active chain."""
        self._execute(
            "UPDATE blocks SET in_chain = ? WHERE hash = ?",
            (1 if in_chain else 0, block_hash),
        )
        self._forget_sizes()

    def get_chain_entry(self, height: int) -> BlockIndexEntry | None:
        """Return the active-chain block at ``height``."""
        row = self._one(
            "SELECT * FROM blocks WHERE in_chain = 1 AND height = ?",
            (height,),
        )
        return None if row is None else self._entry(row)

    def children_of(self, block_hash: bytes) -> list[BlockIndexEntry]:
        """Return every stored block whose parent is ``block_hash``."""
        rows = self._query("SELECT * FROM blocks WHERE prev_hash = ?", (block_hash,))
        return [self._entry(row) for row in rows]

    def best_entries(self, limit: int = 8) -> list[BlockIndexEntry]:
        """Return the stored blocks with the most cumulative work, best first."""
        rows = self._query(
            "SELECT * FROM blocks ORDER BY chainwork DESC, height DESC LIMIT ?", (limit,)
        )
        return [self._entry(row) for row in rows]

    def count_blocks_since(self, timestamp: int) -> int:
        """Number of active-chain blocks with a timestamp at or after ``timestamp``."""
        row = self._one(
            "SELECT COUNT(*) AS n FROM blocks WHERE in_chain = 1 AND timestamp >= ?",
            (timestamp,),
        )
        return 0 if row is None else int(row["n"])

    def block_count(self) -> int:
        """Total number of stored blocks, including side branches."""
        row = self._one("SELECT COUNT(*) AS n FROM blocks")
        return 0 if row is None else int(row["n"])

    # ------------------------------------------------------------------ headers

    def put_header(self, header: BlockHeader, *, height: int, chainwork: int) -> None:
        """Store a validated block header (headers-first sync)."""
        self._execute(
            "INSERT OR REPLACE INTO headers (hash, height, prev_hash, chainwork, raw)"
            " VALUES (?, ?, ?, ?, ?)",
            (header.hash(), height, header.prev_hash, _work_to_blob(chainwork), header.serialize()),
        )

    def has_header(self, block_hash: bytes) -> bool:
        """Return ``True`` if the header is stored."""
        return self._one("SELECT 1 FROM headers WHERE hash = ?", (block_hash,)) is not None

    def header_entry(self, block_hash: bytes) -> HeaderEntry | None:
        """Return the stored header entry for ``block_hash``."""
        row = self._one("SELECT * FROM headers WHERE hash = ?", (block_hash,))
        if row is None:
            return None
        return HeaderEntry(
            hash=bytes(row["hash"]),
            height=int(row["height"]),
            prev_hash=bytes(row["prev_hash"]),
            chainwork=_blob_to_work(bytes(row["chainwork"])),
            header=BlockHeader.deserialize(bytes(row["raw"])),
        )

    def best_header(self) -> HeaderEntry | None:
        """Return the stored header with the most cumulative work."""
        row = self._one("SELECT * FROM headers ORDER BY chainwork DESC, height DESC LIMIT 1")
        if row is None:
            return None
        return HeaderEntry(
            hash=bytes(row["hash"]),
            height=int(row["height"]),
            prev_hash=bytes(row["prev_hash"]),
            chainwork=_blob_to_work(bytes(row["chainwork"])),
            header=BlockHeader.deserialize(bytes(row["raw"])),
        )

    def header_count(self) -> int:
        """Number of stored headers."""
        row = self._one("SELECT COUNT(*) AS n FROM headers")
        return 0 if row is None else int(row["n"])

    def remove_headers_from(self, block_hash: bytes) -> int:
        """Drop a header and every header built on top of it; returns how many went."""
        count = 0
        pending = [block_hash]
        while pending:
            current = pending.pop()
            if not self.has_header(current):
                continue
            self._execute("DELETE FROM headers WHERE hash = ?", (current,))
            count += 1
            for row in self._query("SELECT hash FROM headers WHERE prev_hash = ?", (current,)):
                pending.append(bytes(row["hash"]))
        return count

    # ------------------------------------------------------------------- sizes

    def _forget_sizes(self) -> None:
        """Drop the cached size measurement after the chain changed."""
        with self._lock:
            self._size_cache = None

    def size_stats(self, *, max_age: float = SIZE_CACHE_SECONDS) -> dict:
        """Measure how much room the chain takes up.

        Summing the length of every stored block is a full table scan, and
        ``getinfo`` is polled once a few seconds by the desktop applications, so
        the answer is cached until the chain changes or ``max_age`` passes.

        Returns:
            ``chain_bytes`` is the serialised size of the active chain — the
            honest answer to "how big is this blockchain". ``block_bytes`` adds
            side branches, ``disk_bytes`` adds the indexes, the UTXO set and
            SQLite's own overhead.
        """
        with self._lock:
            fresh = self._size_cache is not None and time.monotonic() - self._size_measured_at < (
                max_age
            )
            if fresh:
                assert self._size_cache is not None
                return dict(self._size_cache)
            blocks = self._one(
                "SELECT COUNT(*) AS blocks,"
                " COALESCE(SUM(LENGTH(raw)), 0) AS block_bytes,"
                " COALESCE(SUM(CASE WHEN in_chain = 1 THEN LENGTH(raw) ELSE 0 END), 0)"
                "     AS chain_bytes,"
                " COALESCE(SUM(CASE WHEN in_chain = 1 THEN 1 ELSE 0 END), 0) AS chain_blocks,"
                " COALESCE(SUM(pruned), 0) AS pruned_blocks,"
                " COALESCE(SUM(CASE WHEN in_chain = 1 AND pruned = 0 THEN LENGTH(raw) ELSE 0 END),"
                "     0) AS full_bytes,"
                " COALESCE(SUM(CASE WHEN in_chain = 1 AND pruned = 0 THEN 1 ELSE 0 END), 0)"
                "     AS full_blocks"
                " FROM blocks"
            )
            undo = self._one("SELECT COALESCE(SUM(LENGTH(data)), 0) AS total FROM undo")
            chain_blocks = 0 if blocks is None else int(blocks["chain_blocks"])
            chain_bytes = 0 if blocks is None else int(blocks["chain_bytes"])
            full_blocks = 0 if blocks is None else int(blocks["full_blocks"])
            full_bytes = 0 if blocks is None else int(blocks["full_bytes"])
            stats = {
                "blocks": 0 if blocks is None else int(blocks["blocks"]),
                "chain_blocks": chain_blocks,
                "block_bytes": 0 if blocks is None else int(blocks["block_bytes"]),
                "chain_bytes": chain_bytes,
                "undo_bytes": 0 if undo is None else int(undo["total"]),
                "disk_bytes": database_size(self.path),
                "pruned_blocks": 0 if blocks is None else int(blocks["pruned_blocks"]),
                "prune_height": self.prune_height,
                # Averaged over the blocks whose bodies are still here, so a
                # pruned node does not report a suspiciously tiny block size.
                "average_block_bytes": (0 if full_blocks == 0 else round(full_bytes / full_blocks)),
            }
            self._size_cache = stats
            self._size_measured_at = time.monotonic()
            return dict(stats)

    def vacuum(self) -> int:
        """Rebuild the database so freed pages are handed back to the filesystem.

        SQLite reuses the space a delete frees, but it does not shrink the file
        on its own, so pruning only shows up in ``du`` after this.  Returns how
        many bytes the file lost.

        Raises:
            StorageError: if called inside a write transaction, which SQLite
                forbids.
        """
        with self._lock:
            if self._depth:
                raise StorageError("cannot vacuum inside a transaction")
            before = database_size(self.path)
            self._connection.execute("VACUUM")
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._forget_sizes()
            return max(0, before - database_size(self.path))

    # ----------------------------------------------------------------- pruning

    @property
    def prune_height(self) -> int:
        """Highest pruned height; 0 when the whole chain is still stored."""
        stored = self.get_meta("prune_height")
        return 0 if stored is None else int(stored)

    def prunable_blocks(self, height: int) -> list[bytes]:
        """Hashes of stored blocks at or below ``height`` that still have a body.

        Genesis is never included: it costs almost nothing and every node is
        expected to be able to show the first block of its own chain.
        """
        rows = self._query(
            "SELECT hash FROM blocks WHERE pruned = 0 AND height BETWEEN 1 AND ? ORDER BY height",
            (height,),
        )
        return [bytes(row["hash"]) for row in rows]

    def prune_to(self, height: int) -> PruneResult:
        """Drop the bodies of every block at or below ``height``.

        The 80-byte header stays, so the block index, the difficulty schedule and
        the timestamp rules keep working, and the UTXO set is untouched, so
        balances stay exactly right.  What goes is the ability to show or serve
        those old blocks and the transactions in them.

        Undo data goes too, which is what makes this irreversible: the node can
        no longer reorganise past ``height``.  Callers are expected to keep a
        generous margin (see :data:`scarletcoin.core.chain.MIN_PRUNE_KEEP`).

        Must be called inside :meth:`write`.
        """
        candidates = self.prunable_blocks(height)
        marker = self.prune_height
        freed = 0
        transactions = 0
        for block_hash in candidates:
            row = self._one("SELECT raw FROM blocks WHERE hash = ?", (block_hash,))
            if row is None:  # pragma: no cover - selected a moment ago
                continue
            raw = bytes(row["raw"])
            try:
                block = Block.deserialize(raw)
            except Exception:  # pragma: no cover - stored blocks always parse
                continue
            for transaction in block.transactions:
                self.unindex_transaction(transaction.txid())
                transactions += 1
            undo = self._one(
                "SELECT LENGTH(data) AS size FROM undo WHERE block_hash = ?", (block_hash,)
            )
            freed += len(raw) - 80 + (0 if undo is None else int(undo["size"]))
            self.delete_undo(block_hash)
            self._execute(
                "UPDATE blocks SET raw = ?, pruned = 1 WHERE hash = ?",
                (raw[:80], block_hash),
            )
        if candidates:
            marker = max(height, marker)
            self.set_meta("prune_height", str(marker).encode())
            self._forget_sizes()
        return PruneResult(
            blocks=len(candidates),
            transactions=transactions,
            freed_bytes=freed,
            prune_height=marker,
        )

    # ---------------------------------------------------------------- UTXO set

    def get_coin(self, outpoint: OutPoint) -> Coin | None:
        """Return the unspent coin at ``outpoint``, if any."""
        row = self._one(
            "SELECT value, type, payload, height, coinbase FROM utxo WHERE txid = ? AND idx = ?",
            (outpoint.txid, outpoint.index),
        )
        if row is None:
            return None
        return Coin(
            value=int(row["value"]),
            output_type=int(row["type"]),
            payload=bytes(row["payload"]),
            height=int(row["height"]),
            is_coinbase=bool(row["coinbase"]),
        )

    def add_coin(self, outpoint: OutPoint, coin: Coin) -> None:
        """Insert an unspent output."""
        self._execute(
            "INSERT OR REPLACE INTO utxo (txid, idx, value, type, payload, height, coinbase)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                outpoint.txid,
                outpoint.index,
                coin.value,
                coin.output_type,
                coin.payload,
                coin.height,
                1 if coin.is_coinbase else 0,
            ),
        )

    def remove_coin(self, outpoint: OutPoint) -> None:
        """Delete an unspent output."""
        self._execute(
            "DELETE FROM utxo WHERE txid = ? AND idx = ?", (outpoint.txid, outpoint.index)
        )

    def coins_of(self, payload: bytes) -> list[tuple[OutPoint, Coin]]:
        """Return every unspent output paying ``payload`` (pubkey or script hash)."""
        rows = self._query(
            "SELECT txid, idx, value, type, payload, height, coinbase"
            " FROM utxo WHERE payload = ? ORDER BY height, txid, idx",
            (payload,),
        )
        return [
            (
                OutPoint(bytes(row["txid"]), int(row["idx"])),
                Coin(
                    value=int(row["value"]),
                    output_type=int(row["type"]),
                    payload=bytes(row["payload"]),
                    height=int(row["height"]),
                    is_coinbase=bool(row["coinbase"]),
                ),
            )
            for row in rows
        ]

    def utxo_stats(self) -> tuple[int, int]:
        """Return ``(number_of_outputs, total_value)`` for the whole UTXO set."""
        row = self._one("SELECT COUNT(*) AS n, COALESCE(SUM(value), 0) AS total FROM utxo")
        if row is None:
            return 0, 0
        return int(row["n"]), int(row["total"])

    def richest_addresses(self, limit: int = 10) -> list[tuple[bytes, int]]:
        """Return the ``limit`` public-key hashes holding the most value."""
        rows = self._query(
            "SELECT payload, SUM(value) AS total FROM utxo"
            " GROUP BY payload ORDER BY total DESC LIMIT ?",
            (limit,),
        )
        return [(bytes(row["payload"]), int(row["total"])) for row in rows]

    # --------------------------------------------------------------- undo data

    def put_undo(self, block_hash: bytes, spent: list[tuple[OutPoint, Coin]]) -> None:
        """Store the coins consumed by a block so it can be rolled back."""
        writer = Writer()
        writer.varint(len(spent))
        for outpoint, coin in spent:
            writer.hash32(outpoint.txid).uint32(outpoint.index).raw(coin.serialize())
        self._execute(
            "INSERT OR REPLACE INTO undo (block_hash, data) VALUES (?, ?)",
            (block_hash, writer.getvalue()),
        )

    def get_undo(self, block_hash: bytes) -> list[tuple[OutPoint, Coin]]:
        """Return the coins consumed by a block.

        Raises:
            StorageError: if the undo record is missing, which makes the block
                impossible to disconnect.
        """
        row = self._one("SELECT data FROM undo WHERE block_hash = ?", (block_hash,))
        if row is None:
            raise StorageError(f"missing undo data for block {block_hash[::-1].hex()}")
        reader = Reader(bytes(row["data"]))
        count = reader.varint()
        spent: list[tuple[OutPoint, Coin]] = []
        for _ in range(count):
            outpoint = OutPoint(reader.hash32(), reader.uint32())
            spent.append((outpoint, Coin.read(reader)))
        reader.expect_end()
        return spent

    def delete_undo(self, block_hash: bytes) -> None:
        """Drop a block's undo record."""
        self._execute("DELETE FROM undo WHERE block_hash = ?", (block_hash,))

    # ----------------------------------------------------------------- indexes

    def index_transaction(
        self,
        transaction: Transaction,
        *,
        block_hash: bytes,
        position: int,
        height: int,
        pubkey_hashes: set[bytes],
    ) -> None:
        """Record where a confirmed transaction lives and which addresses it touches."""
        txid = transaction.txid()
        self._execute(
            "INSERT OR REPLACE INTO tx_location (txid, block_hash, position, height)"
            " VALUES (?, ?, ?, ?)",
            (txid, block_hash, position, height),
        )
        for pubkey_hash in pubkey_hashes:
            self._execute(
                "INSERT OR REPLACE INTO address_history (pubkey_hash, txid, height)"
                " VALUES (?, ?, ?)",
                (pubkey_hash, txid, height),
            )

    def unindex_transaction(self, txid: bytes) -> None:
        """Forget a transaction that is no longer on the active chain."""
        self._execute("DELETE FROM tx_location WHERE txid = ?", (txid,))
        self._execute("DELETE FROM address_history WHERE txid = ?", (txid,))

    def get_tx_location(self, txid: bytes) -> TxLocation | None:
        """Return where a confirmed transaction lives."""
        row = self._one(
            "SELECT block_hash, position, height FROM tx_location WHERE txid = ?", (txid,)
        )
        if row is None:
            return None
        return TxLocation(bytes(row["block_hash"]), int(row["position"]), int(row["height"]))

    def address_history(self, pubkey_hash: bytes, limit: int = 200) -> list[tuple[bytes, int]]:
        """Return ``(txid, height)`` pairs touching ``pubkey_hash``, newest first."""
        rows = self._query(
            "SELECT txid, height FROM address_history WHERE pubkey_hash = ?"
            " ORDER BY height DESC LIMIT ?",
            (pubkey_hash, limit),
        )
        return [(bytes(row["txid"]), int(row["height"])) for row in rows]
