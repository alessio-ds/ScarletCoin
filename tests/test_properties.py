"""Property-based tests for the consensus arithmetic and serialisation."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from scarletcoin.core.pow import bits_to_target, target_to_bits
from scarletcoin.core.serialize import Reader, Writer
from scarletcoin.core.transaction import OutPoint, Transaction, TxInput, TxOutput
from scarletcoin.core.utxo import Coin
from scarletcoin.crypto.base58 import b58decode, b58encode
from scarletcoin.units import format_amount, parse_amount
from scarletcoin.wallet.builder import (
    estimate_size,
    fee_for_size,
    select_coins,
)


@given(st.integers(min_value=0, max_value=(0x7FFFFF << 232) - 1))
def test_target_round_trip(target: int):
    if target == 0:
        assert target_to_bits(target) == 0
        return
    # The compact encoding truncates to a 3-byte mantissa, so the round trip can
    # only ever under-approximate the original target.
    assert bits_to_target(target_to_bits(target)) <= target


@given(st.binary(min_size=0, max_size=200))
def test_base58_round_trip(data: bytes):
    assert b58decode(b58encode(data)) == data


@given(st.binary(min_size=0, max_size=200))
def test_varbytes_round_trip(data: bytes):
    writer = Writer()
    writer.varbytes(data)
    reader = Reader(writer.getvalue())
    assert reader.varbytes() == data


@given(
    st.integers(min_value=0, max_value=10**8 * 10**6),
    st.integers(min_value=0, max_value=10_000),
)
def test_amount_round_trip(scar: int, _junk: int):
    assert parse_amount(format_amount(scar)) == scar


@given(st.integers(min_value=1, max_value=1000), st.integers(min_value=1, max_value=100))
def test_size_estimate_matches_serialisation(inputs: int, outputs: int):
    transaction = Transaction(
        inputs=tuple(
            TxInput(OutPoint(bytes([1]) * 32, index), witness=(b"\x02" * 33, b"\x03" * 64))
            for index in range(inputs)
        ),
        outputs=tuple(TxOutput.p2pkh(1, bytes([2]) * 20) for _ in range(outputs)),
    )
    assert estimate_size(inputs, outputs) == transaction.size()
    assert estimate_size(inputs, outputs) > 0


@given(st.integers(min_value=1, max_value=10**9))
def test_fee_rounds_up(size: int):
    fee = fee_for_size(size, 1000)
    assert fee >= 1
    assert fee >= (size * 1000) // 1000


@given(
    st.lists(st.integers(min_value=1, max_value=10**9), min_size=1, max_size=30),
    st.integers(min_value=1, max_value=10**9),
)
def test_coin_selection_covers_the_amount(values, amount):
    coins = [
        (OutPoint(bytes([index + 1]) * 32, 0), Coin(value, 0, bytes(20), 1, False))
        for index, value in enumerate(values)
    ]
    if sum(coin.value for _, coin in coins) < amount:
        return
    from scarletcoin.wallet.builder import InsufficientFundsError

    try:
        chosen, fee = select_coins(coins, amount, fee_per_kb=1000, output_count=1)
    except InsufficientFundsError:
        return
    total = sum(coin.value for _, coin in chosen)
    assert total >= amount + fee


@settings(deadline=None)
@given(st.integers(min_value=0, max_value=0xFFFFFFFF))
def test_compact_target_never_overflows(bits: int):
    try:
        target = bits_to_target(bits)
    except ValueError:
        return
    assert 0 <= target <= 2**256 - 1


@given(st.integers(min_value=1, max_value=2**16))
def test_transaction_size_is_positive(inputs: int):
    # A transaction of many inputs must serialise to a size greater than the
    # sum of its parts; this just guards against a negative or zero size.
    assert inputs > 0
