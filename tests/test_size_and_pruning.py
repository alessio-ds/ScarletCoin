"""How big the chain is, and what pruning does to it.

The size figures back three separate promises: ``getinfo``/``getchainsize`` over
RPC, the "chain weight" card in the explorer, and the number a user sees before
deciding whether to run a node. Pruning is the destructive half, so its
guarantees — balances survive, validation survives, reorganisations do not — are
pinned down here.
"""

from __future__ import annotations

import sqlite3

import pytest

from scarletcoin.core.chain import MIN_PRUNE_KEEP, Blockchain, prune_database
from scarletcoin.core.params import REGTEST
from scarletcoin.core.storage import (
    SCHEMA_VERSION,
    Storage,
    StorageError,
    database_files,
    database_size,
    inspect_database,
)
from scarletcoin.units import format_bytes
from tests.helpers import mine_and_add


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, "0 B"),
            (1, "1 B"),
            (999, "999 B"),
            (1000, "1.00 kB"),
            (1536, "1.54 kB"),
            (9_999, "10.00 kB"),
            (99_500, "99.5 kB"),
            (1_000_000, "1.00 MB"),
            (21_500_000_000, "21.5 GB"),
            (4_000_000_000_000_000_000, "4000 PB"),
        ],
    )
    def test_it_reads_like_a_person_wrote_it(self, count, expected):
        assert format_bytes(count) == expected

    def test_negatives_keep_their_sign(self):
        assert format_bytes(-2048) == "-2.05 kB"

    def test_floats_are_refused(self):
        with pytest.raises(TypeError):
            format_bytes(1.5)  # type: ignore[arg-type]


class TestChainSize:
    def test_an_empty_chain_is_the_genesis_block(self, chain):
        stats = chain.storage.size_stats()
        assert stats["chain_blocks"] == 1
        assert stats["chain_bytes"] == len(REGTEST.genesis_block.serialize())
        assert stats["pruned_blocks"] == 0
        assert stats["prune_height"] == 0

    def test_mining_makes_the_chain_bigger(self, chain, key):
        before = chain.storage.size_stats()["chain_bytes"]
        blocks = mine_and_add(chain, key, count=5)
        after = chain.storage.size_stats(max_age=0.0)["chain_bytes"]
        assert after == before + sum(block.size() for block in blocks)

    def test_stats_report_the_size_in_bytes_and_in_words(self, chain, key):
        mine_and_add(chain, key, count=3)
        stats = chain.stats()
        assert stats["chain_bytes"] > 0
        assert stats["chain_size"] == format_bytes(stats["chain_bytes"])
        assert stats["disk_size"] == format_bytes(stats["disk_bytes"])
        # In memory there is no file, so the only honest disk figure is zero.
        assert stats["average_block_bytes"] > 0

    def test_the_measurement_is_cached_but_never_stale(self, chain, key):
        first = chain.storage.size_stats()["chain_bytes"]
        assert chain.storage.size_stats()["chain_bytes"] == first
        mine_and_add(chain, key, count=1)
        # Storing a block drops the cache, so the next answer is the new size.
        assert chain.storage.size_stats()["chain_bytes"] > first

    def test_disk_size_counts_the_write_ahead_log(self, tmp_path, key):
        storage = Storage(tmp_path / "chain.sqlite3")
        chain = Blockchain(storage, REGTEST)
        try:
            mine_and_add(chain, key, count=5)
            names = {path.name for path in database_files(storage.path)}
            assert names == {"chain.sqlite3", "chain.sqlite3-wal", "chain.sqlite3-shm"}
            assert database_size(storage.path) == chain.stats()["disk_bytes"] > 0
        finally:
            storage.close()

    def test_a_missing_database_measures_zero(self, tmp_path):
        assert database_size(tmp_path / "nothing.sqlite3") == 0


class TestInspectDatabase:
    def test_it_reports_nothing_for_a_path_that_does_not_exist(self, tmp_path):
        summary = inspect_database(tmp_path / "absent.sqlite3")
        assert summary["exists"] is False
        assert summary["disk_bytes"] == 0
        assert summary["height"] is None

    def test_it_reads_a_chain_without_locking_it(self, tmp_path, key):
        path = tmp_path / "chain.sqlite3"
        storage = Storage(path)
        chain = Blockchain(storage, REGTEST)
        mine_and_add(chain, key, count=4)
        # Deliberately still open for writing: a wallet asks how big the chain is
        # while a node is running, and must not be blocked or block it.
        summary = inspect_database(path)
        assert summary["exists"] is True
        assert summary["height"] == 4
        assert summary["blocks"] == 5
        assert summary["chain_bytes"] == chain.stats()["chain_bytes"]
        assert summary["error"] == ""
        assert chain.height == 4  # the writer is unharmed
        storage.close()

    def test_a_file_that_is_not_a_database_is_reported_not_raised(self, tmp_path):
        path = tmp_path / "junk.sqlite3"
        path.write_bytes(b"definitely not sqlite")
        summary = inspect_database(path)
        assert summary["exists"] is True
        assert summary["error"]
        assert summary["height"] is None


class TestSchemaMigration:
    def _write_v1(self, path):
        """Create a database the way release 2.0.0 would have."""
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB NOT NULL);"
            "CREATE TABLE blocks (hash BLOB PRIMARY KEY, height INTEGER NOT NULL,"
            " prev_hash BLOB NOT NULL, chainwork BLOB NOT NULL,"
            " in_chain INTEGER NOT NULL DEFAULT 0, timestamp INTEGER NOT NULL,"
            " raw BLOB NOT NULL) WITHOUT ROWID;"
        )
        connection.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (b"1",))
        connection.close()

    def test_an_older_database_gains_the_pruned_column(self, tmp_path):
        path = tmp_path / "chain.sqlite3"
        self._write_v1(path)
        storage = Storage(path)
        try:
            assert int(storage.get_meta("schema_version")) == SCHEMA_VERSION
            rows = storage._query("SELECT name FROM pragma_table_info('blocks')")
            columns = {row["name"] for row in rows}
            assert "pruned" in columns
            # And it is usable: the chain bootstraps on top of the migrated schema.
            assert Blockchain(storage, REGTEST).height == 0
        finally:
            storage.close()

    def test_a_newer_database_is_refused_rather_than_guessed_at(self, tmp_path):
        path = tmp_path / "chain.sqlite3"
        Storage(path).close()
        connection = sqlite3.connect(str(path), isolation_level=None)
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1).encode(),),
        )
        connection.close()
        with pytest.raises(StorageError, match="this build understands"):
            Storage(path)


class TestPruning:
    def test_it_does_nothing_on_a_chain_shorter_than_what_is_kept(self, chain, key):
        mine_and_add(chain, key, count=10)
        result = chain.prune(1000)
        assert result.blocks == 0
        assert chain.prune_height == 0

    def test_it_keeps_the_requested_number_of_recent_blocks(self, chain, key):
        mine_and_add(chain, key, count=20)
        result = chain.prune(5)
        assert result.prune_height == 15
        assert result.blocks == 15  # heights 1..15; genesis is never pruned
        assert chain.storage.size_stats(max_age=0.0)["pruned_blocks"] == result.blocks

    def test_the_minimum_is_enforced_however_small_the_request(self, chain, key):
        mine_and_add(chain, key, count=20)
        chain.prune(0)
        assert chain.prune_height == 20 - chain.min_prune_keep

    def test_mainnet_keeps_far_more_than_regtest(self, chain):
        assert chain.min_prune_keep == REGTEST.coinbase_maturity
        assert chain.min_prune_keep < MIN_PRUNE_KEEP

    def test_genesis_survives_so_a_node_can_show_its_own_first_block(self, chain, key):
        mine_and_add(chain, key, count=20)
        chain.prune(2)
        genesis = chain.get_entry_by_height(0)
        assert genesis is not None and not genesis.pruned
        assert chain.get_block(genesis.hash) is not None

    def test_headers_survive_so_the_index_still_works(self, chain, key):
        mine_and_add(chain, key, count=20)
        chain.prune(2)
        entry = chain.get_entry_by_height(3)
        assert entry is not None
        assert entry.pruned is True
        assert entry.height == 3
        assert entry.bits == REGTEST.genesis_bits
        assert chain.get_block(entry.hash) is None
        assert chain.locator()  # walks the active chain by header alone

    def test_balances_are_untouched(self, chain, key):
        mine_and_add(chain, key, count=20)
        pubkey_hash = key.public_key().hash160()
        before = chain.storage.utxo_stats()
        chain.prune(2)
        assert chain.storage.utxo_stats() == before
        assert len(chain.storage.coins_of(pubkey_hash)) == 20

    def test_a_pruned_chain_still_validates_and_extends(self, chain, key):
        mine_and_add(chain, key, count=20)
        chain.prune(2)
        mine_and_add(chain, key, count=3)
        assert chain.height == 23

    def test_pruned_transactions_leave_the_indexes(self, chain, key):
        blocks = mine_and_add(chain, key, count=20)
        early = blocks[0].transactions[0].txid()
        late = blocks[-1].transactions[0].txid()
        assert chain.get_transaction(early) is not None
        chain.prune(2)
        assert chain.get_transaction(early) is None
        assert chain.get_transaction(late) is not None

    def test_pruning_twice_only_takes_what_is_new(self, chain, key):
        mine_and_add(chain, key, count=20)
        first = chain.prune(2)
        again = chain.prune(2)
        assert again.blocks == 0
        assert again.prune_height == first.prune_height
        mine_and_add(chain, key, count=5)
        third = chain.prune(2)
        assert third.blocks == 5
        assert third.prune_height == first.prune_height + 5

    def test_vacuuming_gives_the_space_back_to_the_filesystem(self, tmp_path, key):
        storage = Storage(tmp_path / "chain.sqlite3")
        chain = Blockchain(storage, REGTEST)
        try:
            mine_and_add(chain, key, count=60)
            chain.prune(2)
            storage.vacuum()
            after = database_size(storage.path)
            # A vacuum plus a WAL truncate leaves only live pages behind, which is
            # far less than the log a 60-block sync accumulated.
            assert after < 1_000_000
            assert chain.height == 60
        finally:
            storage.close()

    def test_vacuum_inside_a_transaction_is_refused(self, chain):
        with chain.storage.write(), pytest.raises(StorageError, match="inside a transaction"):
            chain.storage.vacuum()

    def test_prune_database_works_on_a_closed_chain(self, tmp_path, key):
        path = tmp_path / "chain.sqlite3"
        storage = Storage(path)
        chain = Blockchain(storage, REGTEST)
        mine_and_add(chain, key, count=20)
        storage.close()

        result, disk = prune_database(path, REGTEST, 2)
        assert result.blocks == 20 - REGTEST.coinbase_maturity
        assert disk > 0
        assert inspect_database(path)["prune_height"] == result.prune_height
