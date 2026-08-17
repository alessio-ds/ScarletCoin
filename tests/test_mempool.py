"""Tests for the mempool and the transaction builder."""

from __future__ import annotations

import pytest

from scarletcoin.core.mempool import MempoolError
from scarletcoin.core.params import REGTEST
from scarletcoin.core.template import create_block_template
from scarletcoin.core.transaction import OutPoint, Transaction, TxInput, TxOutput
from scarletcoin.core.utxo import Coin
from scarletcoin.core.validation import MissingInputError
from scarletcoin.crypto.keys import PrivateKey
from scarletcoin.wallet.builder import (
    InsufficientFundsError,
    build_transaction,
    dust_threshold,
    estimate_size,
    fee_for_size,
    select_coins,
)
from tests.helpers import make_node_state, mine_and_add, spend


def _coins(*values: int, pubkey_hash: bytes) -> list[tuple[OutPoint, Coin]]:
    return [
        (OutPoint(bytes([index + 1]) * 32, 0), Coin(value, 0, pubkey_hash, 1, False))
        for index, value in enumerate(values)
    ]


class TestMempool:
    def test_a_payment_is_accepted_and_ordered_by_fee(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        destination = other_key.address(REGTEST.address_version)
        cheap = spend(chain, key, destination, 10**8, fee_per_kb=1000, mempool=pool)
        expensive = spend(chain, key, destination, 10**8, fee_per_kb=50_000, mempool=pool)
        assert len(pool) == 2
        assert [entry.txid for entry in pool.entries()] == [expensive.txid(), cheap.txid()]

    def test_the_same_transaction_twice_is_refused(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = spend(
            chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool
        )
        with pytest.raises(MempoolError, match="already in the mempool"):
            pool.add(transaction)

    def test_a_conflicting_transaction_is_refused(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]

        def payment(amount: int) -> Transaction:
            unsigned = Transaction(
                inputs=(TxInput(outpoint),),
                outputs=(TxOutput.p2pkh(amount, other_key.public_key().hash160()),),
            )
            signature = key.sign(
                unsigned.signature_hash(
                    0, coin.value, unsigned.p2pkh_script_code(key.public_key().hash160())
                )
            )
            return unsigned.signed_with({0: (key.public_key().to_bytes(), signature)})

        pool.add(payment(coin.value - 10_000))
        with pytest.raises(MempoolError, match="already spent by mempool"):
            pool.add(payment(coin.value - 20_000))

    def test_a_transaction_below_the_relay_fee_is_refused(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]
        unsigned = Transaction(
            inputs=(TxInput(outpoint),),
            outputs=(TxOutput.p2pkh(coin.value, other_key.public_key().hash160()),),
        )
        signature = key.sign(
            unsigned.signature_hash(
                0, coin.value, unsigned.p2pkh_script_code(key.public_key().hash160())
            )
        )
        free = unsigned.signed_with({0: (key.public_key().to_bytes(), signature)})
        with pytest.raises(MempoolError, match="below the"):
            pool.add(free)

    def test_a_coinbase_cannot_be_relayed(self, chain_and_pool, key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=1)
        coinbase = chain.get_block_by_height(1).coinbase
        with pytest.raises(MempoolError, match="coinbase"):
            pool.add(coinbase)

    def _replaceable(self, key, other_key, amount, fee_per_kb, *, chain, outpoint, coin):
        unsigned = Transaction(
            inputs=(TxInput(outpoint, sequence=0xFFFFFFFD),),
            outputs=(TxOutput.p2pkh(amount, other_key.public_key().hash160()),),
        )
        signature = key.sign(
            unsigned.signature_hash(
                0, coin.value, unsigned.p2pkh_script_code(key.public_key().hash160())
            )
        )
        return unsigned.signed_with({0: (key.public_key().to_bytes(), signature)})

    def test_replace_by_fee_swaps_a_conflicting_transaction(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]
        first = self._replaceable(
            key, other_key, coin.value - 1000, 1000, chain=chain, outpoint=outpoint, coin=coin
        )
        pool.add(first)
        replacement = self._replaceable(
            key, other_key, coin.value - 10_000, 50_000, chain=chain, outpoint=outpoint, coin=coin
        )
        pool.add(replacement)
        assert len(pool) == 1
        assert replacement.txid() in pool

    def test_rbf_requires_a_higher_fee_rate(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]
        first = self._replaceable(
            key, other_key, coin.value - 10_000, 50_000, chain=chain, outpoint=outpoint, coin=coin
        )
        pool.add(first)
        cheaper = self._replaceable(
            key, other_key, coin.value - 1000, 1000, chain=chain, outpoint=outpoint, coin=coin
        )
        with pytest.raises(MempoolError, match="fee rate"):
            pool.add(cheaper)

    def test_rbf_requires_both_sides_to_opt_in(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        outpoint, coin = chain.storage.coins_of(key.public_key().hash160())[0]
        final = spend(chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool)
        assert not final.is_replaceable
        replacement = self._replaceable(
            key, other_key, coin.value - 10_000, 50_000, chain=chain, outpoint=outpoint, coin=coin
        )
        with pytest.raises(MempoolError, match="not replaceable"):
            pool.add(replacement)

    def test_an_orphan_transaction_is_refused(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        unsigned = Transaction(
            inputs=(TxInput(OutPoint(b"\x77" * 32, 0)),),
            outputs=(TxOutput.p2pkh(10**8, other_key.public_key().hash160()),),
        )
        signature = key.sign(
            unsigned.signature_hash(
                0, 10**9, unsigned.p2pkh_script_code(key.public_key().hash160())
            )
        )
        orphan = unsigned.signed_with({0: (key.public_key().to_bytes(), signature)})
        with pytest.raises(MissingInputError):
            pool.add(orphan)

    def test_mining_a_block_clears_the_pool(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = spend(
            chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool
        )
        template = create_block_template(chain, pool)
        assert [tx.txid() for tx in template.transactions] == [transaction.txid()]
        assert template.coinbase_value > REGTEST.subsidy(template.height)

        mine_and_add(chain, key, pool, count=1)
        assert len(pool) == 0
        assert pool.total_bytes == 0

    def test_removing_a_parent_removes_its_children(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        parent = spend(chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool)
        change_index = len(parent.outputs) - 1
        unsigned = Transaction(
            inputs=(TxInput(OutPoint(parent.txid(), change_index)),),
            outputs=(TxOutput.p2pkh(10**8, other_key.public_key().hash160()),),
        )
        signature = key.sign(
            unsigned.signature_hash(
                0,
                parent.outputs[change_index].value,
                unsigned.p2pkh_script_code(key.public_key().hash160()),
            )
        )
        child = unsigned.signed_with({0: (key.public_key().to_bytes(), signature)})
        pool.add(child)

        removed = pool.remove(parent.txid())
        assert set(removed) == {parent.txid(), child.txid()}
        assert len(pool) == 0

    def test_eviction_keeps_the_pool_bounded(self, key, other_key):
        chain, pool = make_node_state()
        pool.max_bytes = 400
        mine_and_add(chain, key, pool, count=4)
        destination = other_key.address(REGTEST.address_version)
        spend(chain, key, destination, 10**8, fee_per_kb=1000, mempool=pool)
        spend(chain, key, destination, 10**8, fee_per_kb=100_000, mempool=pool)
        assert pool.total_bytes <= 400 * 2
        assert len(pool) == 1
        chain.storage.close()

    def test_minimum_fee_grows_with_size(self, chain_and_pool):
        _, pool = chain_and_pool
        assert pool.minimum_fee(1000) == REGTEST.min_relay_fee_per_kb
        assert pool.minimum_fee(2000) == REGTEST.min_relay_fee_per_kb * 2
        assert pool.minimum_fee(1) >= 1


class TestCoinSelection:
    def test_a_single_covering_coin_is_preferred(self):
        pubkey_hash = PrivateKey.generate().public_key().hash160()
        coins = _coins(10**8, 5 * 10**8, 20 * 10**8, pubkey_hash=pubkey_hash)
        chosen, fee = select_coins(coins, 4 * 10**8, fee_per_kb=1000, output_count=1)
        assert len(chosen) == 1
        assert chosen[0][1].value == 5 * 10**8
        assert fee > 0

    def test_larger_coins_are_accumulated_when_needed(self):
        pubkey_hash = PrivateKey.generate().public_key().hash160()
        coins = _coins(10**8, 2 * 10**8, 3 * 10**8, pubkey_hash=pubkey_hash)
        chosen, _ = select_coins(coins, 4 * 10**8, fee_per_kb=1000, output_count=1)
        assert [coin.value for _, coin in chosen] == [3 * 10**8, 2 * 10**8]

    def test_not_enough_money_is_an_error(self):
        pubkey_hash = PrivateKey.generate().public_key().hash160()
        coins = _coins(1000, pubkey_hash=pubkey_hash)
        with pytest.raises(InsufficientFundsError, match="only"):
            select_coins(coins, 10**8, fee_per_kb=1000, output_count=1)

    def test_the_fee_must_be_covered_too(self):
        pubkey_hash = PrivateKey.generate().public_key().hash160()
        coins = _coins(10**8, pubkey_hash=pubkey_hash)
        with pytest.raises(InsufficientFundsError):
            select_coins(coins, 10**8, fee_per_kb=1000, output_count=1)

    def test_size_estimate_matches_reality(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = spend(
            chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool
        )
        expected = estimate_size(len(transaction.inputs), len(transaction.outputs))
        assert transaction.size() == expected

    def test_dust_change_is_given_to_the_miner(self):
        key = PrivateKey.generate()
        pubkey_hash = key.public_key().hash160()
        rate = 1000
        # Choose an amount that leaves less than the dust threshold as change.
        fee = fee_for_size(estimate_size(1, 2), rate)
        coins = _coins(10**8, pubkey_hash=pubkey_hash)
        amount = 10**8 - fee - dust_threshold(rate) + 1
        built = build_transaction(
            spendable_coins=coins,
            keys={pubkey_hash: key},
            outputs=[(pubkey_hash, amount)],
            change_hash=pubkey_hash,
            fee_per_kb=rate,
            params=REGTEST,
        )
        assert built.change == 0
        assert len(built.transaction.outputs) == 1
        assert built.fee == 10**8 - amount

    def test_outputs_must_be_positive(self):
        key = PrivateKey.generate()
        pubkey_hash = key.public_key().hash160()
        with pytest.raises(ValueError, match="positive"):
            build_transaction(
                spendable_coins=_coins(10**8, pubkey_hash=pubkey_hash),
                keys={pubkey_hash: key},
                outputs=[(pubkey_hash, 0)],
                change_hash=pubkey_hash,
                fee_per_kb=1000,
                params=REGTEST,
            )

    def test_an_address_from_another_network_is_refused(self):
        key = PrivateKey.generate()
        pubkey_hash = key.public_key().hash160()
        mainnet_address = key.address(63)
        with pytest.raises(ValueError, match="does not belong to the regtest network"):
            build_transaction(
                spendable_coins=_coins(10**8, pubkey_hash=pubkey_hash),
                keys={pubkey_hash: key},
                outputs=[(mainnet_address, 10**7)],
                change_hash=pubkey_hash,
                fee_per_kb=1000,
                params=REGTEST,
            )

    def test_a_missing_key_is_refused(self):
        key = PrivateKey.generate()
        pubkey_hash = key.public_key().hash160()
        with pytest.raises(ValueError, match="no private key"):
            build_transaction(
                spendable_coins=_coins(10**8, pubkey_hash=pubkey_hash),
                keys={},
                outputs=[(pubkey_hash, 10**7)],
                change_hash=pubkey_hash,
                fee_per_kb=1000,
                params=REGTEST,
            )

    def test_built_transactions_are_valid_and_signed(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = spend(
            chain, key, other_key.address(REGTEST.address_version), 10**8, mempool=pool
        )
        transaction.check_sanity()
        coin = chain.get_coin(transaction.inputs[0].prevout)
        assert coin is not None
        assert transaction.verify_input_signature(0, coin.value, coin.payload)
