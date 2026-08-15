"""Shared helpers for the test suite."""

from __future__ import annotations

import itertools
from dataclasses import replace

from scarletcoin.core.block import Block
from scarletcoin.core.chain import Blockchain
from scarletcoin.core.coinbase import build_coinbase
from scarletcoin.core.mempool import Mempool
from scarletcoin.core.params import REGTEST, ChainParams
from scarletcoin.core.storage import Storage
from scarletcoin.core.template import create_block_template
from scarletcoin.core.transaction import Transaction
from scarletcoin.crypto.keys import PrivateKey
from scarletcoin.miner.solver import solve_block
from scarletcoin.wallet.builder import build_transaction

_counter = itertools.count()


def regtest_params(**overrides: object) -> ChainParams:
    """Return regtest parameters, optionally tweaked."""
    return replace(REGTEST, **overrides) if overrides else REGTEST


def make_chain(tmp_path=None, params: ChainParams | None = None) -> Blockchain:
    """Create a fresh in-memory (or on-disk) regtest chain."""
    params = params or REGTEST
    path = ":memory:" if tmp_path is None else tmp_path / "chain.sqlite3"
    return Blockchain(Storage(path), params)


def make_node_state(tmp_path=None, params: ChainParams | None = None) -> tuple[Blockchain, Mempool]:
    """Create a chain plus a mempool wired as a chain listener."""
    chain = make_chain(tmp_path, params)
    mempool = Mempool(chain, chain.params)
    chain.add_listener(mempool)
    return chain, mempool


def mine_block(
    chain: Blockchain,
    key: PrivateKey,
    mempool: Mempool | None = None,
    *,
    timestamp: int | None = None,
    extra: bytes | None = None,
) -> Block:
    """Mine one block on top of the current tip and return it (not yet submitted)."""
    template = create_block_template(chain, mempool, timestamp=timestamp)
    if extra is None:
        extra = f"test-{next(_counter)}".encode()
    candidate = template.build_block(
        pubkey_hash=key.public_key().hash160(), extra=extra, timestamp=timestamp
    )
    solved = solve_block(candidate)
    assert solved is not None, "regtest blocks are always solvable"
    return solved


def make_block(
    chain: Blockchain,
    key: PrivateKey,
    *,
    transactions: list[Transaction] | None = None,
    timestamp: int | None = None,
    bits: int | None = None,
    prev_hash: bytes | None = None,
    height: int | None = None,
    reward: int | None = None,
    extra: bytes = b"",
    solve: bool = True,
) -> Block:
    """Build a block by hand, bypassing the template's safety clamps.

    Useful for tests that need a block breaking a specific rule.
    """
    tip = chain.tip
    height = tip.height + 1 if height is None else height
    coinbase = build_coinbase(
        height=height,
        reward=chain.params.subsidy(height) if reward is None else reward,
        pubkey_hash=key.public_key().hash160(),
        extra=extra or f"hand-{next(_counter)}".encode(),
    )
    candidate = Block.create(
        prev_hash=tip.hash if prev_hash is None else prev_hash,
        transactions=[coinbase, *(transactions or [])],
        bits=tip.bits if bits is None else bits,
        timestamp=tip.timestamp + 1 if timestamp is None else timestamp,
    )
    if not solve:
        return candidate
    solved = solve_block(candidate)
    assert solved is not None, "regtest blocks are always solvable"
    return solved


def mine_and_add(
    chain: Blockchain,
    key: PrivateKey,
    mempool: Mempool | None = None,
    *,
    count: int = 1,
    timestamp: int | None = None,
) -> list[Block]:
    """Mine ``count`` blocks and connect them to ``chain``."""
    blocks = []
    for index in range(count):
        stamp = None if timestamp is None else timestamp + index
        block = mine_block(chain, key, mempool, timestamp=stamp)
        result = chain.add_block(block)
        assert result.status.value == "connected", result
        blocks.append(block)
    return blocks


def spend(
    chain: Blockchain,
    key: PrivateKey,
    destination,
    amount: int,
    *,
    fee_per_kb: int = 1000,
    mempool: Mempool | None = None,
) -> Transaction:
    """Build (and optionally submit) a transaction spending ``key``'s coins."""
    coins = chain.storage.coins_of(key.public_key().hash160())
    spendable = [
        (outpoint, coin)
        for outpoint, coin in coins
        if coin.is_spendable_at(chain.height + 1, chain.params.coinbase_maturity)
        and not (mempool is not None and mempool.is_spent(outpoint))
    ]
    transaction = build_transaction(
        spendable_coins=spendable,
        keys={key.public_key().hash160(): key},
        outputs=[(destination, amount)],
        change_hash=key.public_key().hash160(),
        fee_per_kb=fee_per_kb,
        params=chain.params,
    ).transaction
    if mempool is not None:
        mempool.add(transaction)
    return transaction
