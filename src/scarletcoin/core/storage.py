"""Persistent storage for the anonymous chain.

Schema version 3: outputs are keyed by one-time pubkey, double-spend prevention
uses a key-image table. Undo data is no longer needed because outputs are
never removed by spending — only key images are recorded.
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
from scarletcoin.core.transaction import Transaction
from scarletcoin.core.utxo import Coin

__all__ = [
    "BlockIndexEntry",
    "PruneResult",
    "Storage",
    "StorageError",
    "TxLocation",
    "database_files",
    "database_size",
    "inspect_database",
]

SCHEMA_VERSION = 3

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

CREATE TABLE IF NOT EXISTS outputs (
    one_time_key BLOB PRIMARY KEY,
    value        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    coinbase     INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS key_images (
    key_image BLOB PRIMARY KEY,
    height    INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS tx_location (
    txid       BLOB PRIMARY KEY,
    block_hash BLOB NOT NULL,
    position   INTEGER NOT NULL,
    height     INTEGER NOT NULL
) WITHOUT ROWID;
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

    @property
    def timestamp(self) -> int:
        return self.header.timestamp

    @property
    def bits(self) -> int:
        return self.header.bits


@dataclass(frozen=True, slots=True)
class TxLocation:
    """Where a confirmed transaction lives."""

    block_hash: bytes
    position: int
    height: int


@dataclass(frozen=True, slots=True)
class PruneResult:
    """What a call to :meth:`Storage.prune_to` did."""

    blocks: int
    transactions: int
    freed_bytes: int
    prune_height: int


def database_files(path: str | Path) -> list[Path]:
    main = Path(path)
    return [main, Path(f"{main}-wal"), Path(f"{main}-shm")]


def database_size(path: str | Path) -> int:
    total = 0
    for candidate in database_files(path):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


def inspect_database(path: str | Path) -> dict:
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
        connection = sqlite3.connect(
            f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
        )
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
        marker = connection.execute(
            "SELECT value FROM meta WHERE key = 'prune_height'"
        ).fetchone()
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
        if from_version < 3:
            with self._lock:
                cur = self._connection
                for table in ("utxo", "undo", "address_history"):
                    with suppress(sqlite3.Error):
                        cur.execute(f"DROP TABLE IF EXISTS {table}")
                columns = {row[1] for row in cur.execute("PRAGMA table_info(blocks)")}
                if "pruned" not in columns:
                    cur.execute(
                        "ALTER TABLE blocks ADD COLUMN pruned INTEGER NOT NULL DEFAULT 0"
                    )
                cur.executescript(_SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION).encode())

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def write(self) -> Iterator[None]:
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
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return None if row is None else bytes(row["value"])

    def set_meta(self, key: str, value: bytes) -> None:
        self._execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def tip_hash(self) -> bytes | None:
        return self.get_meta("tip")

    def set_tip(self, block_hash: bytes) -> None:
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
        return self._one("SELECT 1 FROM blocks WHERE hash = ?", (block_hash,)) is not None

    def get_entry(self, block_hash: bytes) -> BlockIndexEntry | None:
        row = self._one("SELECT * FROM blocks WHERE hash = ?", (block_hash,))
        return None if row is None else self._entry(row)

    def get_block(self, block_hash: bytes) -> Block | None:
        row = self._one("SELECT raw, pruned FROM blocks WHERE hash = ?", (block_hash,))
        if row is None or row["pruned"]:
            return None
        return Block.deserialize(bytes(row["raw"]))

    def set_in_chain(self, block_hash: bytes, in_chain: bool) -> None:
        self._execute(
            "UPDATE blocks SET in_chain = ? WHERE hash = ?",
            (1 if in_chain else 0, block_hash),
        )
        self._forget_sizes()

    def get_chain_entry(self, height: int) -> BlockIndexEntry | None:
        row = self._one(
            "SELECT * FROM blocks WHERE in_chain = 1 AND height = ?", (height,)
        )
        return None if row is None else self._entry(row)

    def children_of(self, block_hash: bytes) -> list[BlockIndexEntry]:
        rows = self._query("SELECT * FROM blocks WHERE prev_hash = ?", (block_hash,))
        return [self._entry(row) for row in rows]

    def best_entries(self, limit: int = 8) -> list[BlockIndexEntry]:
        rows = self._query(
            "SELECT * FROM blocks ORDER BY chainwork DESC, height DESC LIMIT ?", (limit,)
        )
        return [self._entry(row) for row in rows]

    def count_blocks_since(self, timestamp: int) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM blocks WHERE in_chain = 1 AND timestamp >= ?",
            (timestamp,),
        )
        return 0 if row is None else int(row["n"])

    def block_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM blocks")
        return 0 if row is None else int(row["n"])

    # ------------------------------------------------------------------- sizes

    def _forget_sizes(self) -> None:
        with self._lock:
            self._size_cache = None

    def size_stats(self, *, max_age: float = SIZE_CACHE_SECONDS) -> dict:
        with self._lock:
            fresh = (
                self._size_cache is not None
                and time.monotonic() - self._size_measured_at < max_age
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
            chain_blocks = 0 if blocks is None else int(blocks["chain_blocks"])
            chain_bytes = 0 if blocks is None else int(blocks["chain_bytes"])
            full_blocks = 0 if blocks is None else int(blocks["full_blocks"])
            full_bytes = 0 if blocks is None else int(blocks["full_bytes"])
            stats = {
                "blocks": 0 if blocks is None else int(blocks["blocks"]),
                "chain_blocks": chain_blocks,
                "block_bytes": 0 if blocks is None else int(blocks["block_bytes"]),
                "chain_bytes": chain_bytes,
                "undo_bytes": 0,
                "disk_bytes": database_size(self.path),
                "pruned_blocks": 0 if blocks is None else int(blocks["pruned_blocks"]),
                "prune_height": self.prune_height,
                "average_block_bytes": (
                    0 if full_blocks == 0 else round(full_bytes / full_blocks)
                ),
            }
            self._size_cache = stats
            self._size_measured_at = time.monotonic()
            return dict(stats)

    def vacuum(self) -> int:
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
        stored = self.get_meta("prune_height")
        return 0 if stored is None else int(stored)

    def prunable_blocks(self, height: int) -> list[bytes]:
        rows = self._query(
            "SELECT hash FROM blocks WHERE pruned = 0 AND height BETWEEN 1 AND ? ORDER BY height",
            (height,),
        )
        return [bytes(row["hash"]) for row in rows]

    def prune_to(self, height: int) -> PruneResult:
        candidates = self.prunable_blocks(height)
        marker = self.prune_height
        freed = 0
        transactions = 0
        for block_hash in candidates:
            row = self._one("SELECT raw FROM blocks WHERE hash = ?", (block_hash,))
            if row is None:
                continue
            raw = bytes(row["raw"])
            try:
                block = Block.deserialize(raw)
            except Exception:
                continue
            for transaction in block.transactions:
                self.unindex_transaction(transaction.txid())
                transactions += 1
            freed += len(raw) - 80
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

    # ---------------------------------------------------------------- outputs

    def get_coin(self, one_time_key: bytes) -> Coin | None:
        row = self._one(
            "SELECT value, height, coinbase FROM outputs WHERE one_time_key = ?",
            (one_time_key,),
        )
        if row is None:
            return None
        return Coin(
            value=int(row["value"]),
            height=int(row["height"]),
            is_coinbase=bool(row["coinbase"]),
        )

    def has_key_image(self, key_image: bytes) -> bool:
        return self._one("SELECT 1 FROM key_images WHERE key_image = ?", (key_image,)) is not None

    def add_coin(self, one_time_key: bytes, coin: Coin) -> None:
        self._execute(
            "INSERT OR REPLACE INTO outputs (one_time_key, value, height, coinbase)"
            " VALUES (?, ?, ?, ?)",
            (one_time_key, coin.value, coin.height, 1 if coin.is_coinbase else 0),
        )

    def remove_coin(self, one_time_key: bytes) -> None:
        self._execute("DELETE FROM outputs WHERE one_time_key = ?", (one_time_key,))

    def spend_key_image(self, key_image: bytes, height: int) -> None:
        self._execute(
            "INSERT OR REPLACE INTO key_images (key_image, height) VALUES (?, ?)",
            (key_image, height),
        )

    def unspend_key_image(self, key_image: bytes) -> None:
        self._execute("DELETE FROM key_images WHERE key_image = ?", (key_image,))

    def output_stats(self) -> tuple[int, int]:
        row = self._one("SELECT COUNT(*) AS n, COALESCE(SUM(value), 0) AS total FROM outputs")
        if row is None:
            return 0, 0
        return int(row["n"]), int(row["total"])

    def all_outputs(self) -> list[tuple[bytes, int, int, bool]]:
        """Return ``(one_time_key, value, height, is_coinbase)`` for every output."""
        rows = self._query(
            "SELECT one_time_key, value, height, coinbase FROM outputs ORDER BY height"
        )
        return [
            (bytes(r["one_time_key"]), int(r["value"]), int(r["height"]), bool(r["coinbase"]))
            for r in rows
        ]

    def all_key_images(self) -> list[tuple[bytes, int]]:
        """Return ``(key_image, height)`` for every spent key image."""
        rows = self._query("SELECT key_image, height FROM key_images")
        return [(bytes(r["key_image"]), int(r["height"])) for r in rows]

    def outputs_by_value(self, value: int, limit: int = 500) -> list[tuple[bytes, int]]:
        """Return up to ``limit`` outputs matching ``value``, for decoy selection."""
        rows = self._query(
            "SELECT one_time_key, height FROM outputs WHERE value = ?"
            " ORDER BY height DESC LIMIT ?",
            (value, limit),
        )
        return [(bytes(r["one_time_key"]), int(r["height"])) for r in rows]

    # --------------------------------------------------------------- tx lookups

    def index_transaction(
        self, transaction: Transaction, *, block_hash: bytes, position: int, height: int
    ) -> None:
        txid = transaction.txid()
        self._execute(
            "INSERT OR REPLACE INTO tx_location (txid, block_hash, position, height)"
            " VALUES (?, ?, ?, ?)",
            (txid, block_hash, position, height),
        )

    def unindex_transaction(self, txid: bytes) -> None:
        self._execute("DELETE FROM tx_location WHERE txid = ?", (txid,))

    def get_tx_location(self, txid: bytes) -> TxLocation | None:
        row = self._one(
            "SELECT block_hash, position, height FROM tx_location WHERE txid = ?", (txid,)
        )
        if row is None:
            return None
        return TxLocation(bytes(row["block_hash"]), int(row["position"]), int(row["height"]))