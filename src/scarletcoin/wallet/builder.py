"""Coin selection and transaction building.

This is the only place in the code base that creates spending transactions, so
fee estimation, change handling and signing all live together and cannot drift
apart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from scarletcoin.core.params import ChainParams
from scarletcoin.core.transaction import OutPoint, Transaction, TxInput, TxOutput
from scarletcoin.core.utxo import Coin
from scarletcoin.crypto.keys import Address, PrivateKey

__all__ = [
    "PER_INPUT_BYTES",
    "PER_OUTPUT_BYTES",
    "BuiltTransaction",
    "InsufficientFundsError",
    "build_transaction",
    "dust_threshold",
    "estimate_size",
    "select_coins",
]

#: Serialised cost of one input: 36-byte outpoint + 34-byte public key + 65-byte signature.
PER_INPUT_BYTES = 135
#: Serialised cost of one output: 8-byte amount + 20-byte public-key hash.
PER_OUTPUT_BYTES = 28
#: Version, input and output counts, lock time and the empty coinbase-data field.
#: Exact while a transaction has fewer than 253 inputs and outputs.
BASE_BYTES = 11


class InsufficientFundsError(ValueError):
    """Raised when the selected coins cannot cover the payment and its fee."""


def estimate_size(input_count: int, output_count: int) -> int:
    """Return the expected serialised size of a signed transaction."""
    return BASE_BYTES + input_count * PER_INPUT_BYTES + output_count * PER_OUTPUT_BYTES


def fee_for_size(size: int, fee_per_kb: int) -> int:
    """Return the fee for a transaction of ``size`` bytes, rounded up."""
    return max(1, (size * fee_per_kb + 999) // 1000) if fee_per_kb > 0 else 0


def dust_threshold(fee_per_kb: int) -> int:
    """Return the value below which an output costs more to spend than it holds."""
    return fee_for_size(PER_INPUT_BYTES, fee_per_kb) * 3


@dataclass(frozen=True, slots=True)
class BuiltTransaction:
    """A signed transaction plus the numbers that produced it."""

    transaction: Transaction
    fee: int
    change: int
    total_input: int
    coins: tuple[tuple[OutPoint, Coin], ...]

    @property
    def size(self) -> int:
        """Actual serialised size."""
        return self.transaction.size()

    @property
    def fee_rate(self) -> float:
        """Fee in scar per kilobyte."""
        return self.fee * 1000 / self.size if self.size else 0.0


def select_coins(
    coins: Sequence[tuple[OutPoint, Coin]],
    amount: int,
    *,
    fee_per_kb: int,
    output_count: int,
) -> tuple[list[tuple[OutPoint, Coin]], int]:
    """Choose coins covering ``amount`` plus the resulting fee.

    A single coin that covers the payment on its own is preferred (it keeps the
    transaction small); otherwise the largest coins are accumulated first, which
    minimises the number of inputs and therefore the fee.

    Returns:
        The chosen coins and the fee for the resulting transaction, assuming one
        change output.

    Raises:
        InsufficientFundsError: if the coins are not enough.
    """
    if amount < 0:
        raise ValueError("amount must not be negative")

    def required(count: int) -> int:
        return amount + fee_for_size(estimate_size(count, output_count + 1), fee_per_kb)

    usable = sorted(coins, key=lambda item: item[1].value, reverse=True)
    exact = [item for item in usable if item[1].value >= required(1)]
    if exact:
        chosen = [min(exact, key=lambda item: item[1].value)]
        return chosen, fee_for_size(estimate_size(1, output_count + 1), fee_per_kb)

    chosen: list[tuple[OutPoint, Coin]] = []
    total = 0
    for item in usable:
        chosen.append(item)
        total += item[1].value
        if total >= required(len(chosen)):
            return chosen, fee_for_size(estimate_size(len(chosen), output_count + 1), fee_per_kb)
    raise InsufficientFundsError(
        f"need {required(max(len(chosen), 1))} scar (payment plus fee)"
        f" but only {total} scar is available"
    )


def _resolve(destination: Address | bytes, params: ChainParams) -> bytes:
    if isinstance(destination, Address):
        if destination.version != params.address_version:
            raise ValueError(f"address {destination} does not belong to the {params.name} network")
        return destination.hash
    if len(destination) != 20:
        raise ValueError("a destination must be an Address or a 20-byte public-key hash")
    return bytes(destination)


def build_sweep_transaction(
    *,
    spendable_coins: Sequence[tuple[OutPoint, Coin]],
    keys: Mapping[bytes, PrivateKey],
    destination: Address | bytes,
    fee_per_kb: int,
    params: ChainParams,
    lock_time: int = 0,
) -> BuiltTransaction:
    """Spend *every* given coin to one destination, with no change output.

    Raises:
        InsufficientFundsError: if there are no coins, or they do not cover the fee.
        ValueError: if the destination is invalid or a key is missing.
    """
    if not spendable_coins:
        raise InsufficientFundsError("there are no coins to spend")
    pubkey_hash = _resolve(destination, params)
    total = sum(coin.value for _, coin in spendable_coins)
    fee = fee_for_size(estimate_size(len(spendable_coins), 1), fee_per_kb)
    amount = total - fee
    if amount <= 0:
        raise InsufficientFundsError(
            f"the {total} scar available does not cover the {fee} scar fee"
        )
    unsigned = Transaction(
        version=1,
        inputs=tuple(TxInput(outpoint) for outpoint, _ in spendable_coins),
        outputs=(TxOutput(amount, pubkey_hash),),
        lock_time=lock_time,
    )
    return BuiltTransaction(
        transaction=_sign_inputs(unsigned, spendable_coins, keys),
        fee=fee,
        change=0,
        total_input=total,
        coins=tuple(spendable_coins),
    )


def _sign_inputs(
    unsigned: Transaction,
    coins: Sequence[tuple[OutPoint, Coin]],
    keys: Mapping[bytes, PrivateKey],
) -> Transaction:
    """Sign every input of ``unsigned`` with the key owning the matching coin."""
    witnesses: dict[int, tuple[bytes, bytes]] = {}
    for index, (outpoint, coin) in enumerate(coins):
        key = keys.get(coin.pubkey_hash)
        if key is None:
            raise ValueError(f"no private key for coin {outpoint}")
        digest = unsigned.signature_hash(index, coin.value)
        witnesses[index] = (key.public_key().to_bytes(), key.sign(digest))
    return unsigned.signed_with(witnesses)


def build_transaction(
    *,
    spendable_coins: Sequence[tuple[OutPoint, Coin]],
    keys: Mapping[bytes, PrivateKey],
    outputs: Sequence[tuple[Address | bytes, int]],
    change_hash: bytes,
    fee_per_kb: int,
    params: ChainParams,
    lock_time: int = 0,
) -> BuiltTransaction:
    """Select coins, build and sign a transaction.

    Args:
        spendable_coins: Coins that may be spent, as ``(outpoint, coin)`` pairs.
        keys: Private keys by public-key hash; every selected coin needs one.
        outputs: ``(destination, amount)`` pairs to pay.
        change_hash: Where to send the change.
        fee_per_kb: Fee rate in scar per kilobyte.
        params: Chain parameters, used to validate addresses.
        lock_time: Optional height before which the transaction is invalid.

    Returns:
        The signed transaction and its fee, change and inputs.

    Raises:
        InsufficientFundsError: if the coins cannot cover payment plus fee.
        ValueError: if an output is invalid or a key is missing.
    """
    if not outputs:
        raise ValueError("a transaction must pay at least one output")
    targets = [(_resolve(destination, params), amount) for destination, amount in outputs]
    for _, amount in targets:
        if amount <= 0:
            raise ValueError("output amounts must be positive")
    amount = sum(value for _, value in targets)

    chosen, fee = select_coins(
        spendable_coins, amount, fee_per_kb=fee_per_kb, output_count=len(targets)
    )
    total_input = sum(coin.value for _, coin in chosen)
    change = total_input - amount - fee
    if change < 0:  # pragma: no cover - select_coins guarantees this
        raise InsufficientFundsError("selected coins do not cover the fee")

    tx_outputs = [TxOutput(value, pubkey_hash) for pubkey_hash, value in targets]
    if change > dust_threshold(fee_per_kb):
        tx_outputs.append(TxOutput(change, bytes(change_hash)))
    else:
        # Too small to be worth its own output: leave it to the miner as extra fee.
        fee += change
        change = 0

    unsigned = Transaction(
        version=1,
        inputs=tuple(TxInput(outpoint) for outpoint, _ in chosen),
        outputs=tuple(tx_outputs),
        lock_time=lock_time,
    )

    return BuiltTransaction(
        transaction=_sign_inputs(unsigned, chosen, keys),
        fee=fee,
        change=change,
        total_input=total_input,
        coins=tuple(chosen),
    )
