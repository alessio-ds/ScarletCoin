"""Transaction validation against a set of unspent outputs."""

from __future__ import annotations

from scarletcoin.core.params import ChainParams
from scarletcoin.core.transaction import MAX_MONEY, Transaction
from scarletcoin.core.utxo import CoinView
from scarletcoin.crypto.hashing import hash160
from scarletcoin.crypto.keys import InvalidKeyError, InvalidSignatureError

__all__ = [
    "MissingInputError",
    "ValidationError",
    "check_transaction_final",
    "check_transaction_inputs",
]


class ValidationError(Exception):
    """Raised when a transaction or block breaks a consensus rule."""


class MissingInputError(ValidationError):
    """Raised when an input refers to an output that is unknown or already spent.

    This is worth its own type: a transaction can fail this way simply because it
    arrived before its parent, in which case it is an orphan rather than junk.
    """


def check_transaction_final(transaction: Transaction, height: int) -> bool:
    """Return ``True`` if ``transaction`` may be included in a block at ``height``.

    ``lock_time`` is interpreted as a block height: a transaction with
    ``lock_time = n`` becomes valid in the block of height ``n``.
    """
    return transaction.lock_time == 0 or transaction.lock_time <= height


def check_transaction_inputs(
    transaction: Transaction,
    view: CoinView,
    *,
    height: int,
    params: ChainParams,
) -> int:
    """Validate a non-coinbase transaction's inputs and return the fee it pays.

    Every input must reference an existing unspent output, be old enough if that
    output came from a coinbase, reveal the public key the output committed to,
    and carry a valid signature.  The total value spent must be at least the
    total value created.

    Args:
        transaction: The transaction to check.
        view: The coins available to it.
        height: Height of the block the transaction would be included in.
        params: Chain parameters (for the coinbase maturity rule).

    Returns:
        The fee, in scar.

    Raises:
        MissingInputError: if an input is unknown or already spent.
        ValidationError: if any other rule is broken.
    """
    if transaction.is_coinbase:
        raise ValidationError("check_transaction_inputs must not be used on a coinbase")

    total_in = 0
    for index, txin in enumerate(transaction.inputs):
        coin = view.get_coin(txin.prevout)
        if coin is None:
            raise MissingInputError(f"input {index} spends unknown output {txin.prevout}")
        if not coin.is_spendable_at(height, params.coinbase_maturity):
            raise ValidationError(
                f"input {index} spends a coinbase output from height {coin.height};"
                f" it matures at height {coin.height + params.coinbase_maturity}"
            )
        try:
            txin.check_witness_shape()
        except ValueError as exc:
            raise ValidationError(f"input {index}: {exc}") from exc
        if hash160(txin.public_key) != coin.pubkey_hash:
            raise ValidationError(f"input {index} reveals a public key with the wrong hash")
        try:
            valid = transaction.verify_input_signature(index, coin.value)
        except (InvalidKeyError, InvalidSignatureError, ValueError) as exc:
            raise ValidationError(f"input {index}: {exc}") from exc
        if not valid:
            raise ValidationError(f"input {index} has an invalid signature")
        total_in += coin.value
        if total_in > MAX_MONEY:
            raise ValidationError("input values sum to more than the maximum money supply")

    total_out = transaction.total_output()
    if total_in < total_out:
        raise ValidationError(f"transaction spends {total_out} but only provides {total_in} scar")
    return total_in - total_out
