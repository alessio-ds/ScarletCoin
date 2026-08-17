"""Anonymous transaction validation (v2).

Every input is a ring of one-time public keys plus a key image and a
linkable ring signature. The validator cannot tell which ring member is
actually being spent, so the following consensus rules apply:

* All ring members must exist in the output set and have the **same value**.
  The input value is that common value.
* Every ring member must satisfy the coinbase-maturity rule, because the
  validator does not know which one is real.
* The key image must not have been spent before.
* The ring signature must verify.
"""

from __future__ import annotations

from scarletcoin.core.params import ChainParams
from scarletcoin.core.transaction import MAX_MONEY, Transaction
from scarletcoin.core.utxo import CoinView

__all__ = [
    "MissingInputError",
    "PrematureBlockError",
    "ValidationError",
    "check_transaction_final",
    "check_transaction_inputs",
]


class ValidationError(Exception):
    """Raised when a transaction or block breaks a consensus rule."""


class PrematureBlockError(ValidationError):
    """Raised when a block cannot be judged yet because it is ahead of our clock."""


class MissingInputError(ValidationError):
    """Raised when a ring member refers to an output that does not exist."""


def check_transaction_final(transaction: Transaction, height: int) -> bool:
    """Return ``True`` if ``transaction`` may be included in a block at ``height``."""
    return transaction.lock_time == 0 or transaction.lock_time <= height


def check_transaction_inputs(
    transaction: Transaction,
    view: CoinView,
    *,
    height: int,
    params: ChainParams,
) -> int:
    """Validate a non-coinbase transaction's inputs and return the fee it pays.

    Args:
        transaction: The transaction to check.
        view: The outputs available to it.
        height: Height of the block the transaction would be included in.
        params: Chain parameters.

    Returns:
        The fee, in scar.

    Raises:
        MissingInputError: if a ring member is unknown.
        ValidationError: if any other consensus rule is broken.
    """
    if transaction.is_coinbase:
        raise ValidationError("check_transaction_inputs must not be used on a coinbase")

    total_in = 0
    for index, txin in enumerate(transaction.inputs):
        if not txin.ring:
            raise ValidationError(f"input {index} has an empty ring")

        first = view.get_coin(txin.ring[0])
        if first is None:
            raise MissingInputError(f"input {index}: ring member 0 is unknown")
        if not first.is_spendable_at(height, params.coinbase_maturity):
            raise ValidationError(
                f"input {index}: coinbase output at height {first.height}"
                f" is not mature (needs {params.coinbase_maturity} confirmations)"
            )
        ring_value = first.value

        for i, member in enumerate(txin.ring[1:], 1):
            coin = view.get_coin(member)
            if coin is None:
                raise MissingInputError(f"input {index}: ring member {i} is unknown")
            if coin.value != ring_value:
                raise ValidationError(
                    f"input {index}: ring members have different values"
                    f" ({ring_value} vs {coin.value} at position {i})"
                )
            if not coin.is_spendable_at(height, params.coinbase_maturity):
                raise ValidationError(
                    f"input {index}: ring member {i} (coinbase at height {coin.height})"
                    f" is not mature"
                )

        if view.has_key_image(txin.key_image):
            raise ValidationError(f"input {index}: key image has already been spent")

        if not transaction.verify_input_signature(index):
            raise ValidationError(f"input {index} has an invalid ring signature")

        total_in += ring_value
        if total_in > MAX_MONEY:
            raise ValidationError("input values sum to more than the maximum money supply")

    total_out = transaction.total_output()
    if total_in < total_out:
        raise ValidationError(
            f"transaction spends {total_out} but only provides {total_in} scar"
        )
    return total_in - total_out