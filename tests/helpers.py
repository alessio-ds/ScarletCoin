"""Shared helpers for the test suite (anonymous v2)."""

from __future__ import annotations

import itertools
import os
import random
from dataclasses import replace

from scarletcoin.core.block import Block
from scarletcoin.core.chain import Blockchain
from scarletcoin.core.coinbase import build_coinbase
from scarletcoin.core.mempool import Mempool
from scarletcoin.core.params import REGTEST, ChainParams
from scarletcoin.core.storage import Storage
from scarletcoin.core.template import create_block_template
from scarletcoin.core.transaction import Transaction, TxInput, TxOutput
from scarletcoin.crypto.hash_to_point import hash_to_point
from scarletcoin.crypto.keys import StealthKeyPair, generate_stealth_keys
from scarletcoin.crypto.ringsig import ring_sign
from scarletcoin.crypto.schnorr import point_from_bytes, schnorr_point_to_bytes
from scarletcoin.crypto.stealth import (
    StealthAddress,
    derive_ephemeral,
    derive_one_time_public,
    recognize_output,
    spend_key_for_output,
)
from scarletcoin.miner.solver import solve_block

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


def make_stealth_key() -> StealthKeyPair:
    """A throwaway dual-key pair."""
    return generate_stealth_keys()


def stealth_address(keypair: StealthKeyPair, params: ChainParams) -> StealthAddress:
    """The StealthAddress for ``keypair`` on ``params``' network."""
    return keypair.address(params.stealth_version)


def coinbase_output(keypair: StealthKeyPair, params: ChainParams) -> tuple[bytes, bytes]:
    """Return ``(one_time_key, tx_public_key)`` paying a fresh coinbase to ``keypair``."""
    address = stealth_address(keypair, params)
    R, r = derive_ephemeral(os.urandom(32))
    P = derive_one_time_public(r, address)
    return schnorr_point_to_bytes(P), schnorr_point_to_bytes(R)


def mine_block(
    chain: Blockchain,
    keypair: StealthKeyPair,
    mempool: Mempool | None = None,
    *,
    timestamp: int | None = None,
    extra: bytes | None = None,
) -> Block:
    """Mine one block on top of the current tip and return it (not yet submitted)."""
    template = create_block_template(chain, mempool, timestamp=timestamp)
    one_time_key, tx_public_key = coinbase_output(keypair, chain.params)
    if extra is None:
        extra = f"test-{next(_counter)}".encode()
    candidate = template.build_block(
        one_time_key=one_time_key, tx_public_key=tx_public_key, extra=extra, timestamp=timestamp
    )
    solved = solve_block(candidate)
    assert solved is not None, "regtest blocks are always solvable"
    return solved


def make_block(
    chain: Blockchain,
    keypair: StealthKeyPair,
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
    """Build a block by hand, bypassing the template's safety clamps."""
    tip = chain.tip
    height = tip.height + 1 if height is None else height
    one_time_key, tx_public_key = coinbase_output(keypair, chain.params)
    coinbase = build_coinbase(
        height=height,
        reward=chain.params.subsidy(height) if reward is None else reward,
        one_time_key=one_time_key,
        tx_public_key=tx_public_key,
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
    keypair: StealthKeyPair,
    mempool: Mempool | None = None,
    *,
    count: int = 1,
    timestamp: int | None = None,
) -> list[Block]:
    """Mine ``count`` blocks and connect them to ``chain``."""
    blocks = []
    for index in range(count):
        stamp = None if timestamp is None else timestamp + index
        block = mine_block(chain, keypair, mempool, timestamp=stamp)
        result = chain.add_block(block)
        assert result.status.value == "connected", result
        blocks.append(block)
    return blocks


def owned_coins(chain: Blockchain, keypair: StealthKeyPair) -> list[tuple[bytes, int, int]]:
    """Return ``(one_time_key, spend_key, value)`` for every output owned by ``keypair``."""
    address = stealth_address(keypair, chain.params)
    owned: list[tuple[bytes, int, int]] = []
    for height in range(chain.height + 1):
        block = chain.get_block_by_height(height)
        if block is None:
            continue
        for tx in block.transactions:
            R = point_from_bytes(tx.tx_public_key)
            for out in tx.outputs:
                P = point_from_bytes(out.one_time_key)
                if recognize_output(R, P, keypair.view_secret, address):
                    sk = spend_key_for_output(R, keypair.view_secret, keypair.spend_secret)
                    owned.append((out.one_time_key, sk, out.value))
    return owned


def decoy_outputs(chain: Blockchain, value: int, *, exclude: set[bytes]) -> list[bytes]:
    """Return one-time keys of outputs worth ``value`` that are not in ``exclude``."""
    return [
        key for key, v, *_ in chain.storage.all_outputs() if v == value and key not in exclude
    ]


def build_spend(
    chain: Blockchain,
    keypair: StealthKeyPair,
    destination: StealthAddress,
    amount: int,
    *,
    fee: int = 1000,
    ring_size: int = 2,
) -> Transaction:
    """Build a signed anonymous transaction spending one of ``keypair``'s outputs."""
    owned = owned_coins(chain, keypair)
    spendable = [
        (otk, sk, value)
        for otk, sk, value in owned
        if value >= amount + fee
        and chain.storage.get_coin(otk).is_spendable_at(
            chain.height + 1, chain.params.coinbase_maturity
        )
    ]
    assert spendable, "no spendable coin available"
    one_time_key, spend_key, value = spendable[0]

    exclude = {otk for otk, _, _ in owned}
    decoys = decoy_outputs(chain, value, exclude=exclude)
    ring = [one_time_key, *decoys[: max(0, ring_size - 1)]]
    assert len(ring) >= 2, "not enough outputs of the same value to form a ring"
    random.shuffle(ring)
    secret_idx = ring.index(one_time_key)

    R, r = derive_ephemeral(os.urandom(32))
    dest_otk = schnorr_point_to_bytes(derive_one_time_public(r, destination))
    outputs = [TxOutput(amount, dest_otk)]
    change = value - amount - fee
    if change > 0:
        change_otk = schnorr_point_to_bytes(
            derive_one_time_public(r, stealth_address(keypair, chain.params))
        )
        outputs.append(TxOutput(change, change_otk))

    key_image = schnorr_point_to_bytes(spend_key * hash_to_point(one_time_key))
    tx = Transaction(
        version=2,
        inputs=(TxInput(tuple(ring), key_image),),
        outputs=tuple(outputs),
        lock_time=0,
        tx_public_key=schnorr_point_to_bytes(R),
    )
    sig = ring_sign(ring, secret_idx, spend_key, tx.signature_hash(0))
    return tx.signed_with(0, sig)


def spend(
    chain: Blockchain,
    keypair: StealthKeyPair,
    destination: StealthAddress,
    amount: int,
    *,
    fee_per_kb: int = 1000,
    mempool: Mempool | None = None,
) -> Transaction:
    """Build a transaction spending ``keypair``'s coins and optionally pool it."""
    tx = build_spend(chain, keypair, destination, amount, fee=fee_per_kb)
    if mempool is not None:
        mempool.add(tx)
    return tx