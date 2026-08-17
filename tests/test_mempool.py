"""Tests for the mempool of unconfirmed anonymous transactions."""

from __future__ import annotations

import os
import random

import pytest
from ecdsa import SECP256k1

from scarletcoin.core.mempool import MempoolError
from scarletcoin.core.params import REGTEST
from scarletcoin.core.template import create_block_template
from scarletcoin.core.transaction import Transaction, TxInput, TxOutput
from scarletcoin.core.validation import MissingInputError
from scarletcoin.crypto.hash_to_point import hash_to_point
from scarletcoin.crypto.ringsig import ring_sign
from scarletcoin.crypto.schnorr import schnorr_point_to_bytes
from scarletcoin.crypto.stealth import derive_ephemeral, derive_one_time_public
from tests.helpers import (
    decoy_outputs,
    make_node_state,
    mine_and_add,
    owned_coins,
    stealth_address,
)

_G = SECP256k1.generator
_N = SECP256k1.order


def _point() -> bytes:
    value = int.from_bytes(os.urandom(32), "big") % _N or 1
    return schnorr_point_to_bytes(value * _G)


def _spend_coin(chain, keypair, otk, sk, value, destination, amount, *, fee=1000):
    exclude = {otk}
    decoys = decoy_outputs(chain, value, exclude=exclude)
    ring = [otk, *decoys[:1]]
    assert len(ring) >= 2, "not enough decoys to form a ring"
    random.shuffle(ring)
    secret_idx = ring.index(otk)

    R_point, r_scalar = derive_ephemeral(os.urandom(32))
    outputs = [
        TxOutput(amount, schnorr_point_to_bytes(derive_one_time_public(r_scalar, destination)))
    ]
    change = value - amount - fee
    if change > 0:
        change_otk = schnorr_point_to_bytes(
            derive_one_time_public(r_scalar, stealth_address(keypair, chain.params))
        )
        outputs.append(TxOutput(change, change_otk))

    key_image = schnorr_point_to_bytes(sk * hash_to_point(otk))
    tx = Transaction(
        version=2,
        inputs=(TxInput(tuple(ring), key_image),),
        outputs=tuple(outputs),
        lock_time=0,
        tx_public_key=schnorr_point_to_bytes(R_point),
    )
    sig = ring_sign(ring, secret_idx, sk, tx.signature_hash(0))
    return tx.signed_with(0, sig)


def _pay(chain, keypair, destination, amount, *, fee=1000):
    """Spend the first mature owned output."""
    otk, sk, value = owned_coins(chain, keypair)[0]
    return _spend_coin(chain, keypair, otk, sk, value, destination, amount, fee=fee)


class TestMempool:
    def test_a_payment_is_accepted_and_ordered_by_fee(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        destination = stealth_address(other_key, chain.params)
        owned = owned_coins(chain, key)
        cheap = _spend_coin(chain, key, *owned[0][:3], destination, 10**8, fee=1000)
        expensive = _spend_coin(chain, key, *owned[1][:3], destination, 10**8, fee=50_000)
        pool.add(cheap)
        pool.add(expensive)
        assert len(pool) == 2
        assert [entry.txid for entry in pool.entries()] == [expensive.txid(), cheap.txid()]

    def test_the_same_transaction_twice_is_refused(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = _pay(chain, key, stealth_address(other_key, chain.params), 10**8)
        pool.add(transaction)
        with pytest.raises(MempoolError, match="already in the mempool"):
            pool.add(transaction)

    def test_double_spend_of_same_key_image_is_refused(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        destination = stealth_address(other_key, chain.params)
        owned = owned_coins(chain, key)
        otk, sk, value = owned[0]
        first = _spend_coin(chain, key, otk, sk, value, destination, 10 * 10**8, fee=10_000)
        second = _spend_coin(chain, key, otk, sk, value, destination, 10 * 10**8, fee=20_000)
        assert first.inputs[0].key_image == second.inputs[0].key_image
        pool.add(first)
        with pytest.raises(MempoolError, match="already spent"):
            pool.add(second)

    def test_a_transaction_below_the_relay_fee_is_refused(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        destination = stealth_address(other_key, chain.params)
        owned = owned_coins(chain, key)
        transaction = _spend_coin(chain, key, *owned[0][:3], destination, 10**8, fee=0)
        with pytest.raises(MempoolError, match="below the"):
            pool.add(transaction)

    def test_a_coinbase_cannot_be_relayed(self, chain_and_pool, key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=1)
        coinbase = chain.get_block_by_height(1).coinbase
        with pytest.raises(MempoolError, match="coinbase"):
            pool.add(coinbase)

    def test_an_orphan_transaction_is_refused(self, chain_and_pool, key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        orphan = Transaction(
            version=2,
            inputs=(TxInput((_point(), _point()), _point()),),
            outputs=(TxOutput(10**8, _point()),),
            tx_public_key=_point(),
        )
        with pytest.raises(MissingInputError):
            pool.add(orphan)

    def test_add_and_remove(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = _pay(chain, key, stealth_address(other_key, chain.params), 10**8)
        pool.add(transaction)
        assert transaction.txid() in pool
        removed = pool.remove(transaction.txid())
        assert removed == [transaction.txid()]
        assert len(pool) == 0

    def test_block_connected_removes_transactions(self, chain_and_pool, key, other_key):
        chain, pool = chain_and_pool
        mine_and_add(chain, key, pool, count=4)
        transaction = _pay(chain, key, stealth_address(other_key, chain.params), 10**8)
        pool.add(transaction)
        template = create_block_template(chain, pool)
        assert [tx.txid() for tx in template.transactions] == [transaction.txid()]
        assert template.coinbase_value > REGTEST.subsidy(template.height)

        mine_and_add(chain, key, pool, count=1)
        assert len(pool) == 0
        assert pool.total_bytes == 0

    def test_eviction_keeps_the_pool_bounded(self, key, other_key):
        chain, pool = make_node_state()
        pool.max_bytes = 400
        mine_and_add(chain, key, pool, count=4)
        destination = stealth_address(other_key, chain.params)
        owned = owned_coins(chain, key)
        first = _spend_coin(chain, key, *owned[0][:3], destination, 10**8, fee=1000)
        second = _spend_coin(chain, key, *owned[1][:3], destination, 10**8, fee=100_000)
        pool.add(first)
        pool.add(second)
        assert pool.total_bytes <= 400 * 2
        assert len(pool) == 1
        assert second.txid() in pool
        chain.storage.close()

    def test_minimum_fee_grows_with_size(self, chain_and_pool):
        _, pool = chain_and_pool
        assert pool.minimum_fee(1000) == REGTEST.min_relay_fee_per_kb
        assert pool.minimum_fee(2000) == REGTEST.min_relay_fee_per_kb * 2
        assert pool.minimum_fee(1) >= 1
