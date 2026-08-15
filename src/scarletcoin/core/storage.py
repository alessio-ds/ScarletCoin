"""Persistent storage for the block index, the UTXO set and the lookup indexes.

Everything lives in a single SQLite database, which gives us atomic multi-table
updates for free: connecting or disconnecting a block either happens completely
or not at all.  The schema is:

``blocks``
    Every block we have ever validated, whether or not it is on the active
    chain, with its height and cumulative proof of work.
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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scarletcoin.core.block import Block, BlockHeader
from scarletcoin.core.serialize import Reader, Writer
from scarletcoin.core.transaction import OutPoint, Transaction
from scarletcoin.core.utxo import Coin

__all__ = ["BlockIndexEntry", "Storage", "StorageError", "TxLocation"]

SCHEMA_VERSION = 1

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
    raw       BLOB NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS blocks_prev ON blocks (prev_hash);
CREATE INDEX IF NOT EXISTS blocks_chain ON blocks (in_chain, height);
CREATE INDEX IF NOT EXISTS blocks_work ON blocks (chainwork DESC);

CREATE TABLE IF NOT EXISTS utxo (
    txid        BLOB NOT NULL,
    idx         INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    pubkey_hash BLOB NOT NULL,
    height      INTEGER NOT NULL,
    coinbase    INTEGER NOT NULL,
    PRIMARY KEY (txid, idx)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS utxo_address ON utxo (pubkey_hash);

CREATE TABLE IF NOT EXISTS undo (
    block_hash BLOB PRIMARY KEY,
    data       BLOB NOT NULL
) WITHOUT ROWID;

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
        elif int(stored) != SCHEMA_VERSION:
            raise StorageError(
                f"database {self.path} uses schema version {int(stored)},"
                f" this build understands {SCHEMA_VERSION}"
            )

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
        )

    def put_block(
        self, block: Block, *, height: int, chainwork: int, in_chain: bool = False
    ) -> BlockIndexEntry:
        """Store a validated block and its index entry."""
        block_hash = block.hash()
        self._execute(
            "INSERT OR REPLACE INTO blocks"
            " (hash, height, prev_hash, chainwork, in_chain, timestamp, raw)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        """Return a stored block."""
        row = self._one("SELECT raw FROM blocks WHERE hash = ?", (block_hash,))
        return None if row is None else Block.deserialize(bytes(row["raw"]))

    def set_in_chain(self, block_hash: bytes, in_chain: bool) -> None:
        """Mark a block as being on (or off) the active chain."""
        self._execute(
            "UPDATE blocks SET in_chain = ? WHERE hash = ?",
            (1 if in_chain else 0, block_hash),
        )

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

    def block_count(self) -> int:
        """Total number of stored blocks, including side branches."""
        row = self._one("SELECT COUNT(*) AS n FROM blocks")
        return 0 if row is None else int(row["n"])

    # ---------------------------------------------------------------- UTXO set

    def get_coin(self, outpoint: OutPoint) -> Coin | None:
        """Return the unspent coin at ``outpoint``, if any."""
        row = self._one(
            "SELECT value, pubkey_hash, height, coinbase FROM utxo WHERE txid = ? AND idx = ?",
            (outpoint.txid, outpoint.index),
        )
        if row is None:
            return None
        return Coin(
            value=int(row["value"]),
            pubkey_hash=bytes(row["pubkey_hash"]),
            height=int(row["height"]),
            is_coinbase=bool(row["coinbase"]),
        )

    def add_coin(self, outpoint: OutPoint, coin: Coin) -> None:
        """Insert an unspent output."""
        self._execute(
            "INSERT OR REPLACE INTO utxo (txid, idx, value, pubkey_hash, height, coinbase)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                outpoint.txid,
                outpoint.index,
                coin.value,
                coin.pubkey_hash,
                coin.height,
                1 if coin.is_coinbase else 0,
            ),
        )

    def remove_coin(self, outpoint: OutPoint) -> None:
        """Delete an unspent output."""
        self._execute(
            "DELETE FROM utxo WHERE txid = ? AND idx = ?", (outpoint.txid, outpoint.index)
        )

    def coins_of(self, pubkey_hash: bytes) -> list[tuple[OutPoint, Coin]]:
        """Return every unspent output paying ``pubkey_hash``."""
        rows = self._query(
            "SELECT txid, idx, value, pubkey_hash, height, coinbase"
            " FROM utxo WHERE pubkey_hash = ? ORDER BY height, txid, idx",
            (pubkey_hash,),
        )
        return [
            (
                OutPoint(bytes(row["txid"]), int(row["idx"])),
                Coin(
                    value=int(row["value"]),
                    pubkey_hash=bytes(row["pubkey_hash"]),
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
            "SELECT pubkey_hash, SUM(value) AS total FROM utxo"
            " GROUP BY pubkey_hash ORDER BY total DESC LIMIT ?",
            (limit,),
        )
        return [(bytes(row["pubkey_hash"]), int(row["total"])) for row in rows]

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
