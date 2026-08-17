"""Tests for the chain state machine: validation, outputs and reorganisations."""

from __future__ import annotations

import os
import random
import time

import pytest

from scarletcoin.core.chain import Blockchain, BlockStatus
from scarletcoin.core.params import REGTEST
from scarletcoin.core.storage import Storage
from scarletcoin.core.transaction import Transaction, TxInput, TxOutput
from scarletcoin.core.utxo import Coin, CoinOverlay
from scarletcoin.core.validation import (
    ValidationError,
    check_transaction_inputs,
)
from scarletcoin.crypto.hash_to_point import hash_to_point
from scarletcoin.crypto.ringsig import ring_sign
from scarletcoin.crypto.schnorr import schnorr_point_to_bytes
from scarletcoin.crypto.stealth import derive_ephemeral, derive_one_time_public
from tests.helpers import (
    build_spend,
    decoy_outputs,
    make_block,
    make_chain,
    make_node_state,
    mine_and_add,
    mine_block,
    owned_coins,
    spend,
    stealth_address,
)


def _spend_coin(
    chain,
    keypair,
    one_time_key,
    spend_key,
    value,
    destination,
    amount,
    *,
    fee=1000,
    ring_size=2,
):
    exclude = {one_time_key}
    decoys = decoy_outputs(chain, value, exclude=exclude)
    ring = [one_time_key, *decoys[: max(0, ring_size - 1)]]
    assert len(ring) >= 2, "not enough decoys to spend a specific coin"
    random.shuffle(ring)
    secret_idx = ring.index(one_time_key)

    R_point, r_scalar = derive_ephemeral(os.urandom(32))
    dest_otk = schnorr_point_to_bytes(derive_one_time_public(r_scalar, destination))
    outputs = [TxOutput(amount, dest_otk)]
    change = value - amount - fee
    if change > 0:
        change_otk = schnorr_point_to_bytes(
            derive_one_time_public(r_scalar, stealth_address(keypair, chain.params))
        )
        outputs.append(TxOutput(change, change_otk))

    key_image = schnorr_point_to_bytes(spend_key * hash_to_point(one_time_key))
    tx = Transaction(
        version=2,
        inputs=(TxInput(tuple(ring), key_image),),
        outputs=tuple(outputs),
        lock_time=0,
        tx_public_key=schnorr_point_to_bytes(R_point),
    )
    sig = ring_sign(ring, secret_idx, spend_key, tx.signature_hash(0))
    return tx.signed_with(0, sig)


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
        while True:
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
        block = make_block(chain, key, bits=0x207FFFFE)
        result = chain.add_block(block)
        assert result.status is BlockStatus.INVALID
        assert "difficulty" in result.reason

    def test_a_timestamp_too_far_ahead_is_refused_but_not_condemned(self, chain, key):
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
        destination = stealth_address(other_key, chain.params)
        transaction = spend(chain, key, destination, 10 * 10**8, mempool=pool)
        mine_and_add(chain, key, pool, count=1)

        assert chain.get_transaction(transaction.txid()) is not None
        received = sum(v for _, _, v in owned_coins(chain, other_key))
        assert received == 10 * 10**8

    def test_immature_coinbase_outputs_cannot_be_spent(self, chain, key, other_key):
        mine_and_add(chain, key, count=1)
        owned = owned_coins(chain, key)
        assert owned, "should have a coinbase output at height 1"
        one_time_key, spend_key, value = owned[0]
        destination = stealth_address(other_key, chain.params)

        tx = _spend_coin(
            chain, key, one_time_key, spend_key, value, destination, value - 1000
        )
        with pytest.raises(ValidationError, match="not mature"):
            check_transaction_inputs(
                tx, chain, height=chain.height + 1, params=REGTEST
            )

    def test_double_spend_via_same_key_image_is_rejected(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        destination = stealth_address(other_key, chain.params)

        tx1 = build_spend(chain, key, destination, 10 * 10**8)
        tx2 = build_spend(chain, key, destination, 10 * 10**8)
        assert tx1.inputs[0].key_image == tx2.inputs[0].key_image

        block = make_block(chain, key, transactions=[tx1, tx2])
        result = chain.add_block(block)
        assert result.status is BlockStatus.INVALID
        assert "key image" in result.reason

    def test_a_payment_in_a_hand_made_block(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        destination = stealth_address(other_key, chain.params)
        transaction = build_spend(chain, key, destination, 10 * 10**8)

        mine_and_add(chain, key, pool, count=1)
        assert len(pool) == 0

        block = make_block(chain, key, transactions=[transaction])
        result = chain.add_block(block)
        assert result.status is BlockStatus.CONNECTED
        received = sum(v for _, _, v in owned_coins(chain, other_key))
        assert received == 10 * 10**8


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
        assert owned_coins(left, key) == []
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
        shared = mine_and_add(left, key, left_pool, count=4)
        for block in shared:
            assert right.add_block(block).status is BlockStatus.CONNECTED

        transaction = spend(
            left,
            key,
            stealth_address(other_key, left.params),
            10**8,
            mempool=left_pool,
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

        good = branch[-1]
        bad = make_block(
            left,
            other_key,
            reward=REGTEST.subsidy(3) * 10,
            prev_hash=good.header.prev_hash,
            height=3,
            timestamp=good.header.timestamp,
        )
        for block in branch[:-1]:
            left.add_block(block)
        assert left.height == 2
        result = left.add_block(bad)
        assert result.status is BlockStatus.INVALID
        assert left.height == 2
        assert left.tip_hash == left.get_entry_by_height(2).hash
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
        for index in range(3):
            block = mine_block(chain, key, timestamp=base + index)
            assert chain.add_block(block).status is BlockStatus.CONNECTED
        assert chain.next_bits() != chain.tip.bits
        assert bits_to_target(chain.next_bits()) < bits_to_target(chain.tip.bits)
        chain.storage.close()

    def test_the_target_never_gets_easier_than_the_pow_limit(self, key):
        from dataclasses import replace

        params = replace(REGTEST, retarget_interval=2, target_spacing=1)
        chain = make_chain(params=params)
        base = REGTEST.genesis_timestamp + 1
        for index, stamp in enumerate((base, base + 1, base + 100_000)):
            block = mine_block(chain, key, timestamp=stamp)
            assert chain.add_block(block).status is BlockStatus.CONNECTED, index
        assert chain.get_entry_by_height(2).bits != params.pow_limit_bits
        assert chain.next_bits() == params.pow_limit_bits
        chain.storage.close()


class TestUtxoOverlay:
    def test_overlay_hides_removed_coins(self, chain):
        from ecdsa import SECP256k1

        otk = schnorr_point_to_bytes(1 * SECP256k1.generator)
        overlay = CoinOverlay(chain)
        coin = Coin(100, 1, False)
        overlay.add(otk, coin)
        assert overlay.get_coin(otk) == coin
        assert overlay.remove(otk) == coin
        assert overlay.get_coin(otk) is None
        with pytest.raises(KeyError):
            overlay.remove(otk)

    def test_overlay_tracks_key_images(self, chain):
        from ecdsa import SECP256k1

        ki = schnorr_point_to_bytes(2 * SECP256k1.generator)
        overlay = CoinOverlay(chain)
        overlay.spend(ki)
        assert overlay.has_key_image(ki)
        with pytest.raises(ValueError):
            overlay.spend(ki)

    def test_overlay_does_not_touch_the_database(self, chain, key):
        mine_and_add(chain, key, count=1)
        owned = owned_coins(chain, key)
        assert owned
        otk = owned[0][0]
        coin = chain.get_coin(otk)
        overlay = CoinOverlay(chain)
        overlay.remove(otk)
        assert overlay.get_coin(otk) is None
        assert chain.get_coin(otk) == coin


class TestOutputSet:
    def test_output_stats(self, chain, key):
        mine_and_add(chain, key, count=3)
        count, total = chain.storage.output_stats()
        assert count == 4  # three coinbases plus the genesis output
        assert total == chain.total_supply()

    def test_has_key_image_after_spending(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        destination = stealth_address(other_key, chain.params)
        spend(chain, key, destination, 10 * 10**8, mempool=pool)
        mine_and_add(chain, key, pool, count=1)
        rows = chain.storage.all_key_images()
        assert len(rows) == 1