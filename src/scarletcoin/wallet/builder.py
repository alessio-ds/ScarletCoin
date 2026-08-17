"""Coin selection and anonymous transaction building (v2).

Every outgoing transaction uses linkable ring signatures. The builder
selects coins, picks decoy outputs from the chain, assigns one-time
public keys to every destination, signs the ring for each input, and
optionally splits change into standard denominations.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass

from scarletcoin.core.params import ChainParams
from scarletcoin.core.transaction import MAX_MONEY, Transaction, TxInput, TxOutput
from scarletcoin.core.utxo import Coin
from scarletcoin.crypto.hash_to_point import hash_to_point
from scarletcoin.crypto.ringsig import ring_sign
from scarletcoin.crypto.schnorr import schnorr_point_to_bytes
from scarletcoin.crypto.stealth import (
    StealthAddress,
    StealthError,
    derive_ephemeral,
    derive_one_time_public,
)
from scarletcoin.net.client import RpcClient

__all__ = [
    "DEFAULT_RING_SIZE",
    "BuiltTransaction",
    "InsufficientFundsError",
    "build_anonymous_transaction",
    "estimate_size_v2",
    "fee_for_size",
    "select_decoy_outputs",
]

DEFAULT_RING_SIZE = 16

# Per-input body: varint ring size + `ring_size` members + key image
# Per-input witness: varbytes header + LSAG sig
#   sig = varint n + c0 + n·r + K  ⇒  1 + 32 + n·32 + 33 = 66 + 32·n
#   witness total = 1 (varbytes) + 66 + 32·n = 67 + 32·n
# Body per input = 1 (varint) + n·33 + 33 (key image) = 34 + 33·n
# Total per input  = 34 + 33·n + 67 + 32·n = 101 + 65·n
#
# Base: version (4) + input-count varint (1) + output-count varint (1)
#       + lock-time (4) + tx_public_key (33) + extra varint-0 (1) = 44
# Per output: value uint64 (8) + one_time_key (33) = 41

BASE_BYTES_V2 = 44


def estimate_size_v2(
    input_count: int, output_count: int, ring_size: int = DEFAULT_RING_SIZE
) -> int:
    """Return the expected serialised size of a signed v2 transaction."""
    return BASE_BYTES_V2 + input_count * (65 * ring_size + 101) + output_count * 41


def fee_for_size(size: int, fee_per_kb: int) -> int:
    """Return the fee for a transaction of ``size`` bytes, rounded up."""
    return max(1, (size * fee_per_kb + 999) // 1000) if fee_per_kb > 0 else 0


class InsufficientFundsError(ValueError):
    """Raised when the selected coins cannot cover the payment and its fee."""


@dataclass(frozen=True, slots=True)
class BuiltTransaction:
    """A signed transaction plus the numbers that produced it."""

    transaction: Transaction
    fee: int
    change: int
    total_input: int

    @property
    def size(self) -> int:
        """Actual serialised size."""
        return self.transaction.size()

    @property
    def fee_rate(self) -> float:
        """Fee in scar per kilobyte."""
        return self.fee * 1000 / self.size if self.size else 0.0


# ------------------------------------------------------------------- decoys


def select_decoy_outputs(
    client: RpcClient, value: int, count: int, *, params: ChainParams | None = None
) -> list[bytes]:
    """Return ``count`` one-time-key bytes for outputs worth ``value``.

    Candidates are fetched from the node and sampled with a preference for
    recent blocks so the ring looks plausible to an outside observer. Immature
    coinbase outputs are skipped: a ring member that has not matured would make
    the node reject the whole transaction.
    """
    if count <= 0:
        return []
    maturity = params.coinbase_maturity if params is not None else 0
    height = client.getblockcount() if params is not None else None
    rows = []
    for item in client.getoutputs():
        if int(item["value"]) != value:
            continue
        item_height = int(item.get("height", 0))
        if (
            maturity
            and bool(item.get("coinbase", False))
            and item_height + maturity > height
        ):
            continue
        rows.append((bytes.fromhex(item["one_time_key"]), item_height))
    if not rows:
        return []

    rows.sort(key=lambda r: r[1], reverse=True)
    if len(rows) <= count:
        return [key for key, _ in rows]

    pool = list(rows)
    chosen: list[bytes] = []

    def _weight(i: int) -> float:
        return 1.0 / (i + 1)

    for _ in range(count):
        if not pool:
            break
        weights = [_weight(i) for i in range(len(pool))]
        idx = random.choices(range(len(pool)), weights=weights, k=1)[0]
        chosen.append(pool.pop(idx)[0])
    return chosen


# -------------------------------------------------------------- denominations


def split_denominations(amount: int) -> list[int]:
    """Break ``amount`` scar into powers-of-ten denominations.

    1234 → ``[1000, 100, 100, 10, 10, 10, 1, 1, 1, 1]``
    """
    parts: list[int] = []
    power = 1
    while power * 10 <= amount:
        power *= 10
    remaining = amount
    while power >= 1:
        q, remaining = divmod(remaining, power)
        parts.extend([power] * q)
        power //= 10
    return parts


# --------------------------------------------------------------- ring helper


def _build_ring(
    real_key: bytes,
    value: int,
    ring_size: int,
    client: RpcClient,
    params: ChainParams,
) -> tuple[list[bytes], int]:
    """Return a ring of one-time keys and the index of *real_key* inside it."""
    decoys = select_decoy_outputs(client, value, ring_size - 1, params=params)
    members: list[bytes] = [real_key]
    for d in decoys:
        if d != real_key and d not in members:
            members.append(d)
    if len(members) < 2:
        raise InsufficientFundsError(
            f"not enough outputs worth {value} scar to form a ring"
        )
    random.shuffle(members)
    return members, members.index(real_key)


# ------------------------------------------------------------ address helpers


def _resolve_stealth(address: StealthAddress | str, params: ChainParams) -> StealthAddress:
    if isinstance(address, StealthAddress):
        if address.version != params.stealth_version:
            raise ValueError(
                f"address {address} does not belong to the {params.name} network"
            )
        return address
    if isinstance(address, str):
        try:
            addr = StealthAddress.decode(address, expected_version=params.stealth_version)
        except StealthError as exc:
            raise ValueError(str(exc)) from exc
        return addr
    raise ValueError("a destination must be a StealthAddress or a stealth address string")


def _derive_outputs(
    r: int,
    targets: list[tuple[StealthAddress, int]],
    change_addr: StealthAddress,
    change_outs: list[int],
) -> list[TxOutput]:
    outputs: list[TxOutput] = []
    for addr, amount in targets:
        p = derive_one_time_public(r, addr)
        outputs.append(TxOutput(amount, schnorr_point_to_bytes(p)))
    for value in change_outs:
        p = derive_one_time_public(r, change_addr)
        outputs.append(TxOutput(value, schnorr_point_to_bytes(p)))
    return outputs


# ------------------------------------------------------------- assembly core


def _try_build(
    selected: list[tuple[bytes, Coin, int]],
    total_input: int,
    targets: list[tuple[StealthAddress, int]],
    change_addr: StealthAddress,
    fee_per_kb: int,
    params: ChainParams,
    ring_size: int,
    client: RpcClient,
) -> BuiltTransaction | None:
    total_amount = sum(amount for _, amount in targets)
    fee = 0
    change = 0
    change_outs: list[int] = []

    for _ in range(64):
        change = total_input - total_amount - fee
        if change < 0:
            return None
        fresh = split_denominations(change)
        output_count = len(targets) + len(fresh)
        new_fee = fee_for_size(
            estimate_size_v2(len(selected), output_count, ring_size), fee_per_kb
        )
        if new_fee == fee and change_outs == fresh:
            # Converged.
            r_seed = os.urandom(32)
            R_point, r_scalar = derive_ephemeral(r_seed)
            tx_public_key = schnorr_point_to_bytes(R_point)

            tx_outputs = _derive_outputs(r_scalar, targets, change_addr, change_outs)

            tx_inputs: list[TxInput] = []
            ring_indices: list[int] = []
            for one_time_key, coin, _spend_key in selected:
                ring_members, secret_idx = _build_ring(
                    one_time_key, coin.value, ring_size, client, params
                )
                key_image = schnorr_point_to_bytes(
                    _spend_key * hash_to_point(one_time_key)
                )
                tx_inputs.append(TxInput(tuple(ring_members), key_image))
                ring_indices.append(secret_idx)

            tx = Transaction(
                version=2,
                inputs=tuple(tx_inputs),
                outputs=tuple(tx_outputs),
                lock_time=0,
                tx_public_key=tx_public_key,
            )

            for idx, (_otk, _coin, spk) in enumerate(selected):
                ring_members = list(tx.inputs[idx].ring)
                sighash = tx.signature_hash(idx)
                sig = ring_sign(ring_members, ring_indices[idx], spk, sighash)
                tx = tx.signed_with(idx, sig)

            return BuiltTransaction(tx, new_fee, sum(change_outs), total_input)

        fee = new_fee
        change_outs = fresh

    return None


# --------------------------------------------------------------- public API


def build_anonymous_transaction(
    wallet,  # Wallet (duck-typed: .coins() -> [(bytes, Coin, int)], .client -> RpcClient)
    outputs: list[tuple[StealthAddress | str, int]],
    change_addr: StealthAddress | str,
    fee_per_kb: int,
    params: ChainParams,
) -> BuiltTransaction:
    """Build and sign an anonymous v2 transaction.

    Args:
        wallet: Provides ``.coins()`` and ``.client``.
        outputs: ``(stealth_address, scar_amount)`` pairs.
        change_addr: Where change, if any, is returned.
        fee_per_kb: Fee rate in scar per kilobyte.
        params: Chain parameters for network validation.

    Returns:
        The signed transaction, its fee, the change amount and total input.

    Raises:
        InsufficientFundsError: if the coins cannot cover payment plus fee.
        ValueError: if an output is invalid.
    """
    if not outputs:
        raise ValueError("a transaction must pay at least one output")

    targets: list[tuple[StealthAddress, int]] = []
    for address, amount in outputs:
        addr = _resolve_stealth(address, params)
        if amount <= 0:
            raise ValueError("output amounts must be positive")
        if amount > MAX_MONEY:
            raise ValueError("output amount exceeds the maximum money supply")
        targets.append((addr, amount))

    change = _resolve_stealth(change_addr, params)
    spendable = wallet.coins()

    usable = sorted(spendable, key=lambda item: item[1].value, reverse=True)

    selected: list[tuple[bytes, Coin, int]] = []
    total_input = 0
    for item in usable:
        selected.append(item)
        total_input += item[1].value
        result = _try_build(
            selected,
            total_input,
            targets,
            change,
            fee_per_kb,
            params,
            DEFAULT_RING_SIZE,
            wallet.client,
        )
        if result is not None:
            return result

    raise InsufficientFundsError(
        f"need more than {total_input} scar to cover the payment and its fee"
    )