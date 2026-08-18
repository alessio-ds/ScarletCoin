"""Tests for the chain state machine: validation, the UTXO set and reorganisations."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace

import pytest

from scarletcoin.core.block import Block, BlockHeader
from scarletcoin.core.chain import Blockchain, BlockStatus
from scarletcoin.core.coinbase import build_coinbase
from scarletcoin.core.params import REGTEST
from scarletcoin.core.storage import Storage, StorageError
from scarletcoin.core.template import create_block_template
from scarletcoin.core.transaction import OutPoint, Transaction, TxInput, TxOutput
from scarletcoin.core.utxo import Coin, CoinOverlay
from scarletcoin.core.validation import (
    MissingInputError,
    ValidationError,
    check_transaction_inputs,
)
from scarletcoin.crypto.keys import PrivateKey
from scarletcoin.miner.solver import solve_block
from tests.helpers import (
    make_block,
    make_chain,
    make_node_state,
    mine_and_add,
    mine_block,
    spend,
)


def _sign(unsigned: Transaction, key: PrivateKey, values: list[int]) -> Transaction:
    script_code = unsigned.p2pkh_script_code(key.public_key().hash160())
    witnesses = {
        index: (
            key.public_key().to_bytes(),
            key.sign(unsigned.signature_hash(index, values[index], script_code)),
        )
        for index in range(len(unsigned.inputs))
    }
    return unsigned.signed_with(witnesses)


class TestGenesis:
    def test_a_new_chain_starts_at_the_genesis_block(self, chain):
        assert chain.height == 0
        assert chain.tip_hash == REGTEST.genesis_hash
        assert chain.total_supply() == REGTEST.subsidy(0)

    def test_state_survives_a_restart(self, tmp_path, key):
        chain = make_chain(tmp_path)
        mine_and_add(chain, key, count=3)
        tip, supply = chain.tip_hash, chain.total_supply()
        chain.storage.close()

        reopened = Blockchain(Storage(tmp_path / "chain.sqlite3"), REGTEST)
        assert reopened.height == 3
        assert reopened.tip_hash == tip
        assert reopened.total_supply() == supply
        reopened.storage.close()

    def test_a_database_from_another_network_is_refused(self, tmp_path, key):
        chain = make_chain(tmp_path)
        chain.storage.close()
        from dataclasses import replace

        other = replace(REGTEST, name="regtest", genesis_timestamp=REGTEST.genesis_timestamp + 1)
        with pytest.raises(ValidationError, match="different network"):
            Blockchain(Storage(tmp_path / "chain.sqlite3"), other)


class TestCheckpoints:
    def test_a_block_matching_the_checkpoint_is_accepted(self, key):
        params = replace(REGTEST)
        base = make_chain(params=params)
        blocks = []
        for i in range(3):
            block = make_block(base, key, timestamp=REGTEST.genesis_timestamp + i + 1, extra=b"cp")
            base.add_block(block)
            blocks.append(block)
        checkpoint = base.get_entry_by_height(2).hash[::-1].hex()

        guarded = make_chain(params=replace(REGTEST, checkpoints={2: checkpoint}))
        for block in blocks:
            assert guarded.add_block(block).status is BlockStatus.CONNECTED
        guarded.storage.close()

    def test_a_block_against_the_checkpoint_is_rejected(self, key):
        guarded = make_chain(params=replace(REGTEST, checkpoints={2: "00" * 32}))
        first = make_block(guarded, key, timestamp=REGTEST.genesis_timestamp + 1, extra=b"x")
        assert guarded.add_block(first).status is BlockStatus.CONNECTED
        second = make_block(guarded, key, timestamp=REGTEST.genesis_timestamp + 2, extra=b"x")
        result = guarded.add_block(second)
        assert result.status is BlockStatus.INVALID
        assert "checkpoint" in result.reason
        guarded.storage.close()


class TestHeaderSync:
    def _headers(self, chain, key, count):
        blocks = []
        for i in range(count):
            block = make_block(chain, key, timestamp=REGTEST.genesis_timestamp + i + 1, extra=b"h")
            chain.add_block(block)
            blocks.append(block)
        return [block.header.serialize() for block in blocks]

    def test_headers_are_accepted_and_tracked(self, key):
        source = make_chain()
        raw_headers = self._headers(source, key, 5)
        source.storage.close()

        target = make_chain()
        for raw in raw_headers:
            assert target.add_header(BlockHeader.deserialize(raw)) is None
        assert target.header_height() == 5
        assert len(target.headers_to_download()) == 5
        assert target.header_tip().height == 5
        target.storage.close()

    def test_headers_list_the_missing_blocks_in_order(self, key):
        source = make_chain()
        raw_headers = self._headers(source, key, 5)
        hashes = [BlockHeader.deserialize(raw).hash() for raw in raw_headers]
        source.storage.close()

        target = make_chain()
        for raw in raw_headers:
            target.add_header(BlockHeader.deserialize(raw))
        assert target.headers_to_download() == hashes
        target.storage.close()

    def test_a_bad_header_is_rejected(self, key):
        from scarletcoin.core.pow import check_proof_of_work

        source = make_chain()
        raw_headers = self._headers(source, key, 2)
        source.storage.close()

        target = make_chain()
        assert target.add_header(BlockHeader.deserialize(raw_headers[0])) is None
        header = BlockHeader.deserialize(raw_headers[1])
        nonce = header.nonce
        while True:  # regtest's target is easy, so find a hash that misses it
            nonce += 1
            broken = header.with_nonce(nonce)
            if not check_proof_of_work(broken.hash(), broken.bits, pow_limit=REGTEST.pow_limit):
                break
        assert target.add_header(broken) is not None
        assert target.header_height() == 1
        target.storage.close()

    def test_an_orphan_header_is_held_until_its_parent_arrives(self, key):
        source = make_chain()
        raw_headers = self._headers(source, key, 3)
        source.storage.close()

        target = make_chain()
        second = BlockHeader.deserialize(raw_headers[1])
        assert target.add_header(second) is None  # parent unknown: deferred, not an error
        assert target.header_height() == 0
        target.add_header(BlockHeader.deserialize(raw_headers[0]))
        assert target.add_header(second) is None
        assert target.header_height() == 2
        target.storage.close()


class TestBlockAcceptance:
    def test_mining_extends_the_chain(self, chain, key):
        blocks = mine_and_add(chain, key, count=5)
        assert chain.height == 5
        assert chain.tip_hash == blocks[-1].hash()
        assert chain.total_supply() == REGTEST.subsidy(0) * 6

    def test_the_same_block_twice_is_a_duplicate(self, chain, key):
        block = mine_block(chain, key)
        assert chain.add_block(block).status is BlockStatus.CONNECTED
        assert chain.add_block(block).status is BlockStatus.DUPLICATE

    def test_a_block_without_its_parent_is_an_orphan(self, chain, key):
        first = mine_block(chain, key)
        chain.add_block(first)
        second = mine_block(chain, key)
        fresh, _ = make_node_state()
        assert fresh.add_block(second).status is BlockStatus.ORPHAN
        fresh.storage.close()

    def test_bad_proof_of_work_is_rejected(self, chain, key):
        from scarletcoin.core.pow import check_proof_of_work

        block = mine_block(chain, key)
        nonce = block.header.nonce
        while True:  # regtest's target is easy, so look for a hash that misses it
            nonce += 1
            broken = block.with_header(block.header.with_nonce(nonce))
            if not check_proof_of_work(
                broken.hash(), broken.header.bits, pow_limit=REGTEST.pow_limit
            ):
                break
        result = chain.add_block(broken)
        assert result.status is BlockStatus.INVALID
        assert "proof-of-work" in result.reason

    def test_the_wrong_difficulty_is_rejected(self, chain, key):
        template = create_block_template(chain)
        candidate = template.build_block(pubkey_hash=key.public_key().hash160())
        cheated = Block.create(
            prev_hash=candidate.header.prev_hash,
            transactions=list(candidate.transactions),
            bits=0x207FFFFE,
            timestamp=candidate.header.timestamp,
        )
        solved = solve_block(cheated)
        assert solved is not None
        result = chain.add_block(solved)
        assert result.status is BlockStatus.INVALID
        assert "difficulty" in result.reason

    def test_a_timestamp_too_far_ahead_is_refused_but_not_condemned(self, chain, key):
        """Still not accepted — but "too far ahead" is measured against *our* clock,
        so it is not a verdict on the block. It must stay judgeable later, or a
        node with a slow clock would permanently refuse the whole network."""
        block = mine_block(chain, key, timestamp=int(time.time()) + 3 * 60 * 60)
        result = chain.add_block(block)
        assert result.status is BlockStatus.PREMATURE
        assert not result.accepted
        assert "clock" in result.reason
        assert chain.height == 0
        assert not chain.has_block(block.hash())
        assert chain._invalid == {}

    def test_an_old_timestamp_is_rejected(self, chain, key):
        mine_and_add(chain, key, count=3)
        block = make_block(chain, key, timestamp=REGTEST.genesis_timestamp)
        result = chain.add_block(block)
        assert result.status is BlockStatus.INVALID
        assert "median" in result.reason

    def test_the_coinbase_must_claim_the_right_height(self, chain, key):
        result = chain.add_block(make_block(chain, key, height=99))
        assert result.status is BlockStatus.INVALID
        assert "claims height 99" in result.reason

    def test_an_over_paying_coinbase_is_rejected(self, chain, key):
        block = make_block(chain, key, reward=REGTEST.subsidy(1) + 1)
        result = chain.add_block(block)
        assert result.status is BlockStatus.INVALID
        assert "coinbase pays" in result.reason

    def test_a_coinbase_may_underpay(self, chain, key):
        assert chain.add_block(make_block(chain, key, reward=1)).status is BlockStatus.CONNECTED
        assert chain.total_supply() == REGTEST.subsidy(0) + 1


class TestSpending:
    def test_a_payment_moves_value(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        destination = other_key.address(REGTEST.address_version)
        transaction = spend(chain, key, destination, 10 * 10**8, mempool=pool)
        mine_and_add(chain, key, pool, count=1)

        assert chain.get_transaction(transaction.txid()) is not None
        received = sum(
            coin.value for _, coin in chain.storage.coins_of(other_key.public_key().hash160())
        )
        assert received == 10 * 10**8

    def test_immature_coinbase_outputs_cannot_be_spent(self, chain, key, other_key):
        mine_and_add(chain, key, count=1)
        coins = chain.storage.coins_of(key.public_key().hash160())
        outpoint, coin = coins[0]
        unsigned = Transaction(
            inputs=(TxInput(outpoint),),
            outputs=(TxOutput.p2pkh(coin.value - 1000, other_key.public_key().hash160()),),
        )
        transaction = _sign(unsigned, key, [coin.value])
        with pytest.raises(ValidationError, match="matures at height"):
            check_transaction_inputs(transaction, chain, height=chain.height + 1, params=REGTEST)

    def test_spending_an_unknown_output_is_a_missing_input(self, chain, key):
        unsigned = Transaction(
            inputs=(TxInput(OutPoint(b"\x33" * 32, 0)),),
            outputs=(TxOutput.p2pkh(1, key.public_key().hash160()),),
        )
        transaction = _sign(unsigned, key, [1000])
        with pytest.raises(MissingInputError):
            check_transaction_inputs(transaction, chain, height=1, params=REGTEST)

    def test_a_forged_signature_is_rejected(self, chain, key, other_key):
        mine_and_add(chain, key, count=4)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]
        unsigned = Transaction(
            inputs=(TxInput(outpoint),),
            outputs=(TxOutput.p2pkh(coin.value - 1000, other_key.public_key().hash160()),),
        )
        # other_key signs, but the coin belongs to key
        forged = _sign(unsigned, other_key, [coin.value])
        with pytest.raises(ValidationError, match="invalid signature"):
            check_transaction_inputs(forged, chain, height=chain.height + 1, params=REGTEST)

    def test_spending_more_than_the_inputs_hold_is_rejected(self, chain, key):
        mine_and_add(chain, key, count=4)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]
        unsigned = Transaction(
            inputs=(TxInput(outpoint),),
            outputs=(TxOutput.p2pkh(coin.value + 1, key.public_key().hash160()),),
        )
        transaction = _sign(unsigned, key, [coin.value])
        with pytest.raises(ValidationError, match="only provides"):
            check_transaction_inputs(transaction, chain, height=chain.height + 1, params=REGTEST)

    def test_a_block_with_a_double_spend_is_rejected(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]
        destination = other_key.public_key().hash160()

        def payment(amount: int) -> Transaction:
            unsigned = Transaction(
                inputs=(TxInput(outpoint),),
                outputs=(TxOutput.p2pkh(amount, destination),),
            )
            return _sign(unsigned, key, [coin.value])

        first, second = payment(coin.value - 1000), payment(coin.value - 2000)
        block = make_block(chain, key, transactions=[first, second])
        result = chain.add_block(block)
        assert result.status is BlockStatus.INVALID
        assert "unknown output" in result.reason

    def test_chained_transactions_inside_one_block(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        first = spend(
            chain, key, other_key.address(REGTEST.address_version), 5 * 10**8, mempool=pool
        )
        # spend the change of the first transaction, still unconfirmed
        change_index = 1 if len(first.outputs) > 1 else 0
        unsigned = Transaction(
            inputs=(TxInput(OutPoint(first.txid(), change_index)),),
            outputs=(TxOutput.p2pkh(10**8, other_key.public_key().hash160()),),
        )
        second = _sign(unsigned, key, [first.outputs[change_index].value])
        pool.add(second)
        assert len(pool) == 2

        mine_and_add(chain, key, pool, count=1)
        assert chain.get_transaction(second.txid()) is not None
        assert len(pool) == 0


class TestReorganisation:
    def test_a_longer_branch_wins(self, key, other_key):
        left, left_pool = make_node_state()
        right, right_pool = make_node_state()
        mine_and_add(left, key, left_pool, count=3)
        branch = mine_and_add(right, other_key, right_pool, count=5)

        results = [left.add_block(block) for block in branch]
        assert [result.status for result in results[:3]] == [BlockStatus.SIDE_BRANCH] * 3
        assert results[3].reorganised is True
        assert left.height == 5
        assert left.tip_hash == right.tip_hash
        assert left.total_supply() == right.total_supply()
        # the coins mined on the abandoned branch are gone
        assert left.storage.coins_of(key.public_key().hash160()) == []
        left.storage.close()
        right.storage.close()

    def test_a_shorter_branch_is_kept_but_not_activated(self, key, other_key):
        left, left_pool = make_node_state()
        right, right_pool = make_node_state()
        mine_and_add(left, key, left_pool, count=4)
        branch = mine_and_add(right, other_key, right_pool, count=2)

        for block in branch:
            assert left.add_block(block).status is BlockStatus.SIDE_BRANCH
        assert left.height == 4
        for block in branch:
            entry = left.get_entry(block.hash())
            assert entry is not None and not entry.in_chain
        left.storage.close()
        right.storage.close()

    def test_a_reorg_puts_transactions_back_in_the_mempool(self, key, other_key):
        left, left_pool = make_node_state()
        right, right_pool = make_node_state()
        # both chains share the first blocks so the payment stays valid
        shared = mine_and_add(left, key, left_pool, count=4)
        for block in shared:
            assert right.add_block(block).status is BlockStatus.CONNECTED

        transaction = spend(
            left, key, other_key.address(REGTEST.address_version), 10**8, mempool=left_pool
        )
        mine_and_add(left, key, left_pool, count=1)
        assert len(left_pool) == 0
        assert left.get_transaction(transaction.txid()) is not None

        branch = mine_and_add(right, other_key, right_pool, count=3)
        for block in branch:
            left.add_block(block)

        assert left.height == 7
        assert left.get_transaction(transaction.txid()) is None
        assert transaction.txid() in left_pool
        left.storage.close()
        right.storage.close()

    def test_an_invalid_branch_does_not_replace_a_valid_chain(self, key, other_key):
        left, left_pool = make_node_state()
        right, right_pool = make_node_state()
        mine_and_add(left, key, left_pool, count=2)
        branch = mine_and_add(right, other_key, right_pool, count=3)

        # Corrupt the last block of the heavier branch: it pays itself too much.
        good = branch[-1]
        template_coinbase = build_coinbase(
            height=3,
            reward=REGTEST.subsidy(3) * 10,
            pubkey_hash=other_key.public_key().hash160(),
            extra=b"greedy",
        )
        bad = solve_block(
            Block.create(
                prev_hash=good.header.prev_hash,
                transactions=[template_coinbase],
                bits=good.header.bits,
                timestamp=good.header.timestamp,
            )
        )
        for block in branch[:-1]:
            left.add_block(block)
        assert left.height == 2  # the branch is still lighter
        result = left.add_block(bad)
        assert result.status is BlockStatus.INVALID
        assert left.height == 2
        assert left.tip_hash == left.get_entry_by_height(2).hash
        # a valid block on the same branch still works
        assert left.add_block(good).status is BlockStatus.CONNECTED
        assert left.height == 3
        left.storage.close()
        right.storage.close()

    def test_locator_and_fork_finding(self, chain, key):
        mine_and_add(chain, key, count=12)
        locator = chain.locator()
        assert locator[0] == chain.tip_hash
        assert locator[-1] == REGTEST.genesis_hash
        assert chain.find_fork_height(locator) == 12
        assert chain.find_fork_height([b"\x00" * 32, REGTEST.genesis_hash]) == 0
        assert len(chain.active_hashes_after(5, 3)) == 3
        assert chain.active_hashes_after(12, 10) == []


class TestDifficulty:
    def test_fast_blocks_make_the_next_target_harder(self, key):
        from dataclasses import replace

        from scarletcoin.core.pow import bits_to_target

        params = replace(REGTEST, retarget_interval=4, target_spacing=10)
        chain = make_chain(params=params)
        base = REGTEST.genesis_timestamp + 1
        # Three blocks one second apart: much faster than the ten-second target.
        for index in range(3):
            block = mine_block(chain, key, timestamp=base + index)
            assert chain.add_block(block).status is BlockStatus.CONNECTED
        # The block at height 4 is the first of a new period, so it retargets.
        assert chain.next_bits() != chain.tip.bits
        assert bits_to_target(chain.next_bits()) < bits_to_target(chain.tip.bits)
        chain.storage.close()

    def test_the_target_never_gets_easier_than_the_pow_limit(self, key):
        from dataclasses import replace

        params = replace(REGTEST, retarget_interval=2, target_spacing=1)
        chain = make_chain(params=params)
        base = REGTEST.genesis_timestamp + 1
        # Fast blocks first, so the target tightens...
        for index, stamp in enumerate((base, base + 1, base + 100_000)):
            block = mine_block(chain, key, timestamp=stamp)
            assert chain.add_block(block).status is BlockStatus.CONNECTED, index
        assert chain.get_entry_by_height(2).bits != params.pow_limit_bits
        # ...then a very slow one, which would push it past the limit.
        assert chain.next_bits() == params.pow_limit_bits
        chain.storage.close()


class TestUtxoOverlay:
    def test_overlay_hides_spent_coins(self, chain):
        overlay = CoinOverlay(chain)
        outpoint = OutPoint(b"\x44" * 32, 0)
        coin = Coin(100, 0, b"\x01" * 20, 1, False)
        overlay.add(outpoint, coin)
        assert overlay.get_coin(outpoint) == coin
        assert overlay.spend(outpoint) == coin
        assert overlay.get_coin(outpoint) is None
        with pytest.raises(KeyError):
            overlay.spend(outpoint)

    def test_overlay_does_not_touch_the_database(self, chain, key):
        mine_and_add(chain, key, count=1)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]
        overlay = CoinOverlay(chain)
        overlay.spend(outpoint)
        assert overlay.get_coin(outpoint) is None
        assert chain.get_coin(outpoint) == coin


class TestStorage:
    def test_missing_undo_data_is_an_error(self, chain, key):
        mine_and_add(chain, key, count=1)
        entry = chain.get_entry_by_height(1)
        chain.storage.delete_undo(entry.hash)
        with pytest.raises(StorageError, match="missing undo data"):
            chain.storage.get_undo(entry.hash)

    def test_rich_list_and_stats(self, chain, key, other_key):
        mine_and_add(chain, key, count=3)
        richest = chain.storage.richest_addresses(5)
        assert richest[0][1] == REGTEST.subsidy(0) * 3
        count, total = chain.storage.utxo_stats()
        assert count == 4  # three coinbases plus the genesis output
        assert total == chain.total_supply()

    def test_address_history_follows_the_chain(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = spend(
            chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool
        )
        mine_and_add(chain, key, pool, count=1)
        history = chain.storage.address_history(other_key.public_key().hash160())
        assert [txid for txid, *_ in history] == [transaction.txid()]

    def test_address_history_precomputes_amounts(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = spend(
            chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool
        )
        spent_value = sum(chain.storage.get_coin(txin.prevout).value for txin in transaction.inputs)
        mine_and_add(chain, key, pool, count=1)

        other_hash = other_key.public_key().hash160()
        key_hash = key.public_key().hash160()

        received = sum(o.value for o in transaction.outputs if o.payload == other_hash)
        assert received == 10**8
        key_received = sum(o.value for o in transaction.outputs if o.payload == key_hash)

        other_history = {
            txid: (recv, sent, coinbase)
            for txid, _height, recv, sent, coinbase in chain.storage.address_history(other_hash)
        }
        recv, sent, coinbase = other_history[transaction.txid()]
        assert (recv, sent, coinbase) == (received, 0, False)

        key_history = {
            txid: (recv, sent, coinbase)
            for txid, _height, recv, sent, coinbase in chain.storage.address_history(key_hash)
        }
        recv, sent, coinbase = key_history[transaction.txid()]
        assert (recv, sent, coinbase) == (key_received, spent_value, False)

        # The coinbase transactions that mined those blocks are flagged as such.
        coinbase_rows = [
            row for row in chain.storage.address_history(key_hash, limit=1000) if row[4]
        ]
        assert len(coinbase_rows) == 5  # the four blocks plus the one mining the spend

    def test_schema_v4_backfills_address_history(self, tmp_path, key, other_key):
        path = tmp_path / "chain.sqlite3"
        chain, pool = make_node_state(tmp_path)
        mine_and_add(chain, key, pool, count=4)
        transaction = spend(
            chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool
        )
        spent_value = sum(chain.storage.get_coin(txin.prevout).value for txin in transaction.inputs)
        mine_and_add(chain, key, pool, count=1)
        chain.storage.close()

        # Rewind to schema 3 with blanked precomputed columns, then reopen: the
        # node must recompute them from the stored blocks and undo records.
        connection = sqlite3.connect(str(path))
        connection.execute("UPDATE address_history SET received = 0, sent = 0, coinbase = 0")
        connection.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (b"3",))
        connection.commit()
        connection.close()

        reopened = make_chain(tmp_path)
        other_hash = other_key.public_key().hash160()
        history = {
            txid: (recv, sent, coinbase)
            for txid, _height, recv, sent, coinbase in reopened.storage.address_history(other_hash)
        }
        assert history[transaction.txid()] == (10**8, 0, False)

        key_hash = key.public_key().hash160()
        key_history = {
            txid: (recv, sent, coinbase)
            for txid, _height, recv, sent, coinbase in reopened.storage.address_history(key_hash)
        }
        recv, sent, coinbase = key_history[transaction.txid()]
        key_received = sum(o.value for o in transaction.outputs if o.payload == key_hash)
        assert (recv, sent, coinbase) == (key_received, spent_value, False)
        reopened.storage.close()
