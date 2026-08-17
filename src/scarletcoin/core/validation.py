"""Transaction validation against a set of unspent outputs."""

from __future__ import annotations

from scarletcoin.core.params import ChainParams
from scarletcoin.core.script import MAX_SCRIPT_SIZE, ScriptError, evaluate_script
from scarletcoin.core.transaction import MAX_MONEY, OUTPUT_P2PKH, OUTPUT_P2SH, Transaction
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
    """Raised when a block cannot be judged yet because it is ahead of our clock.

    This is the one rejection that says nothing about the block and nothing about
    the peer that sent it: a timestamp is only "too far in the future" relative to
    the clock of the machine doing the checking. A node whose clock is slow would
    otherwise reject every honest block on the network, punish the peers serving
    them, and end up alone at height zero — so this is kept separate from a real
    consensus violation, is never cached, and is never held against a peer.
    """


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


def _verify_p2sh(transaction: Transaction, index: int, prevout_value: int) -> bool:
    txin = transaction.inputs[index]
    if not txin.witness or len(txin.witness[0]) > MAX_SCRIPT_SIZE:
        return False
    redeem_script = txin.witness[0]
    arguments = list(txin.witness[1:])
    digest = transaction.signature_hash(index, prevout_value, redeem_script)
    try:
        return evaluate_script(redeem_script, arguments, digest)
    except ScriptError:
        return False


def check_transaction_inputs(
    transaction: Transaction,
    view: CoinView,
    *,
    height: int,
    params: ChainParams,
) -> int:
    """Validate a non-coinbase transaction's inputs and return the fee it pays.

    Every input must reference an existing unspent output, be old enough if that
    output came from a coinbase, and satisfy the output's lock: reveal a matching
    public key and signature for P2PKH, or run a valid redeem script for P2SH.
    The total value spent must be at least the total value created.

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
        if coin.output_type not in (OUTPUT_P2PKH, OUTPUT_P2SH):
            raise ValidationError(f"input {index} has an unknown output type {coin.output_type}")

        if coin.output_type == OUTPUT_P2PKH:
            valid = transaction.verify_input_signature(index, coin.value, coin.payload)
        elif coin.output_type == OUTPUT_P2SH:
            valid = _verify_p2sh(transaction, index, coin.value)
        else:  # pragma: no cover - checked just above
            raise ValidationError(f"input {index} has an unknown output type {coin.output_type}")
        if not valid:
            raise ValidationError(f"input {index} has an invalid signature")
        total_in += coin.value
        if total_in > MAX_MONEY:
            raise ValidationError("input values sum to more than the maximum money supply")

    total_out = transaction.total_output()
    if total_in < total_out:
        raise ValidationError(f"transaction spends {total_out} but only provides {total_in} scar")
    return total_in - total_out
