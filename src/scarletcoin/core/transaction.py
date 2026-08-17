"""Anonymous transactions (v2).

ScarletCoin v3 uses linkable ring signatures and stealth addresses so
every transaction is private by default. A transaction body (covered by
its txid) lists ring members and key images for each input, one-time
public keys for each output, and the ephemeral key R that lets the
recipient's wallet scan for owned outputs. Ring signatures live in the
witness and are therefore not covered by the txid, so they cannot be
malleated to change the transaction's identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Final

from scarletcoin.core.serialize import Reader, SerializationError, Writer
from scarletcoin.crypto.hashing import hash256
from scarletcoin.crypto.ringsig import ring_verify
from scarletcoin.crypto.schnorr import POINT_SIZE, SCALAR_SIZE, point_from_bytes

__all__ = [
    "MAX_COINBASE_DATA",
    "MAX_MONEY",
    "MAX_RING_SIZE",
    "MIN_RING_SIZE",
    "Transaction",
    "TransactionError",
    "TxInput",
    "TxOutput",
]

_SIGHASH_TAG: Final[bytes] = b"ScarletCoin/sighash/2"

MAX_MONEY: Final[int] = 21_000_000 * 100_000_000
MAX_COINBASE_DATA: Final[int] = 100
MIN_RING_SIZE: Final[int] = 2
MAX_RING_SIZE: Final[int] = 32

#: Domain separation for ephemeral key generation.
_EPHEMERAL_TAG: Final[bytes] = b"ScarletCoin/stealth/ephemeral/1"


class TransactionError(ValueError):
    """Raised when a transaction is structurally invalid."""


@dataclass(frozen=True, slots=True)
class TxOutput:
    """A spendable amount locked to a one-time public key."""

    value: int
    one_time_key: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TransactionError("output value must be an integer")
        if self.value < 0:
            raise TransactionError("output value must not be negative")
        if self.value > MAX_MONEY:
            raise TransactionError("output value exceeds the maximum money supply")
        if len(self.one_time_key) != POINT_SIZE:
            raise TransactionError(
                f"one-time key must be {POINT_SIZE} bytes, got {len(self.one_time_key)}"
            )


@dataclass(frozen=True, slots=True)
class TxInput:
    """An anonymous authorisation to spend one output of a ring."""

    ring: tuple[bytes, ...]
    key_image: bytes
    signature: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "ring", tuple(self.ring))
        for member in self.ring:
            if len(member) != POINT_SIZE:
                raise TransactionError(f"ring member must be {POINT_SIZE} bytes")
        if self.ring and len(self.key_image) != POINT_SIZE:
            raise TransactionError(
                f"key image must be {POINT_SIZE} bytes, got {len(self.key_image)}"
            )
        if self.ring and self.key_image == b"\x00" * POINT_SIZE:
            raise TransactionError("key image must not be all zeros")

    @property
    def is_coinbase_input(self) -> bool:
        """``True`` when this input represents the coinbase (empty ring)."""
        return len(self.ring) == 0

    def with_signature(self, signature: bytes) -> TxInput:
        """Return a copy carrying the given ring signature."""
        return replace(self, signature=bytes(signature))


@dataclass(frozen=True, slots=True)
class Transaction:
    """A transfer of value authenticated by linkable ring signatures."""

    version: int = 2
    inputs: tuple[TxInput, ...] = field(default_factory=tuple)
    outputs: tuple[TxOutput, ...] = field(default_factory=tuple)
    lock_time: int = 0
    tx_public_key: bytes = b""
    extra: bytes = b""

    def __post_init__(self) -> None:
        if not 0 <= self.version <= 0xFFFFFFFF:
            raise TransactionError(f"transaction version out of range: {self.version}")
        if not 0 <= self.lock_time <= 0xFFFFFFFF:
            raise TransactionError(f"lock time out of range: {self.lock_time}")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "extra", bytes(self.extra))
        if len(self.tx_public_key) not in (0, POINT_SIZE):
            raise TransactionError(
                f"tx_public_key must be 0 or {POINT_SIZE} bytes, got {len(self.tx_public_key)}"
            )
        if self.tx_public_key:
            try:
                point_from_bytes(self.tx_public_key)
            except Exception as exc:
                raise TransactionError(f"tx_public_key is not a valid point: {exc}") from exc

    # ------------------------------------------------------------------ shape

    @property
    def is_coinbase(self) -> bool:
        """``True`` if this transaction mints the block reward."""
        return len(self.inputs) == 1 and self.inputs[0].is_coinbase_input

    def total_output(self) -> int:
        """Sum of all output values."""
        return sum(output.value for output in self.outputs)

    def check_sanity(self) -> None:
        """Validate everything that can be checked without the chain state.

        Raises:
            TransactionError: if the transaction is malformed.
        """
        if not self.inputs:
            raise TransactionError("transaction has no inputs")
        if not self.outputs:
            raise TransactionError("transaction has no outputs")
        if self.total_output() > MAX_MONEY:
            raise TransactionError("total output value exceeds the maximum money supply")

        if self.is_coinbase:
            if len(self.extra) > MAX_COINBASE_DATA:
                raise TransactionError(f"coinbase data must be at most {MAX_COINBASE_DATA} bytes")
            if self.inputs[0].signature:
                raise TransactionError("coinbase input must not carry a ring signature")
            if len(self.tx_public_key) != POINT_SIZE:
                raise TransactionError("coinbase must carry a tx_public_key")
            return

        if self.extra:
            raise TransactionError("only a coinbase transaction may carry extra data")
        if len(self.tx_public_key) != POINT_SIZE:
            raise TransactionError("transaction must carry a tx_public_key")

        for txin in self.inputs:
            n = len(txin.ring)
            if n < MIN_RING_SIZE:
                raise TransactionError(f"ring has {n} members, the minimum is {MIN_RING_SIZE}")
            if n > MAX_RING_SIZE:
                raise TransactionError(f"ring has {n} members, the maximum is {MAX_RING_SIZE}")
            if len(set(txin.ring)) != len(txin.ring):
                raise TransactionError("ring contains duplicate members")

    # ---------------------------------------------------------- serialisation

    def serialize_body(self) -> bytes:
        """Serialise everything the transaction id commits to."""
        writer = Writer()
        writer.uint32(self.version)
        writer.varint(len(self.inputs))
        for txin in self.inputs:
            writer.varint(len(txin.ring))
            for member in txin.ring:
                writer.raw(member)
            writer.raw(txin.key_image)
        writer.varint(len(self.outputs))
        for txout in self.outputs:
            writer.uint64(txout.value).raw(txout.one_time_key)
        writer.uint32(self.lock_time)
        writer.raw(self.tx_public_key if self.tx_public_key else b"\x00" * POINT_SIZE)
        writer.varbytes(self.extra)
        return writer.getvalue()

    def serialize(self) -> bytes:
        """Serialise the transaction, witnesses included (wire and disk format)."""
        writer = Writer()
        writer.raw(self.serialize_body())
        for txin in self.inputs:
            writer.varbytes(txin.signature)
        return writer.getvalue()

    @classmethod
    def deserialize(cls, data: bytes) -> Transaction:
        """Parse a transaction from its wire format.

        Raises:
            SerializationError: if the stream is truncated or has trailing bytes.
        """
        reader = Reader(data)
        transaction = cls.read(reader)
        reader.expect_end()
        return transaction

    @classmethod
    def read(cls, reader: Reader) -> Transaction:
        """Parse one transaction from ``reader``."""
        version = reader.uint32()
        input_count = reader.varint()
        if input_count == 0:
            raise SerializationError("transaction has no inputs")

        inputs: list[TxInput] = []
        for _ in range(input_count):
            ring_size = reader.varint()
            ring = tuple(reader.raw(POINT_SIZE) for _ in range(ring_size))
            key_image = reader.raw(POINT_SIZE) if ring else b""
            inputs.append(TxInput(ring, key_image))

        output_count = reader.varint()
        if output_count == 0:
            raise SerializationError("transaction has no outputs")
        outputs = tuple(
            TxOutput(reader.uint64(), reader.raw(POINT_SIZE))
            for _ in range(output_count)
        )

        lock_time = reader.uint32()
        tx_public_key = reader.raw(POINT_SIZE)
        if tx_public_key == b"\x00" * POINT_SIZE:
            tx_public_key = b""
        extra = reader.varbytes(max_length=MAX_COINBASE_DATA)

        # witnesses: ring signatures
        for i, txin in enumerate(inputs):
            if txin.is_coinbase_input:
                reader.varbytes(max_length=0)
            else:
                sig = reader.varbytes(max_length=MAX_RING_SIZE * (SCALAR_SIZE + 32) + 100)
                inputs[i] = txin.with_signature(sig)

        return cls(version, tuple(inputs), outputs, lock_time, tx_public_key, extra)

    # ----------------------------------------------------------------- hashes

    def txid(self) -> bytes:
        """Return the transaction id: double SHA-256 of the body."""
        return hash256(self.serialize_body())

    def txid_hex(self) -> str:
        """Return the transaction id as a big-endian hex string (display order)."""
        return self.txid()[::-1].hex()

    def signature_hash(self, input_index: int) -> bytes:
        """Return the digest input ``input_index`` must sign.

        Commits to the entire body plus which input is being signed.
        """
        if not 0 <= input_index < len(self.inputs):
            raise TransactionError(f"no input at index {input_index}")
        writer = Writer()
        writer.varbytes(_SIGHASH_TAG)
        writer.raw(self.serialize_body())
        writer.uint32(input_index)
        return hash256(writer.getvalue())

    def size(self) -> int:
        """Serialised size in bytes."""
        return len(self.serialize())

    # ------------------------------------------------------------------ verify

    def verify_input_signature(self, input_index: int) -> bool:
        """Verify one input's ring signature."""
        txin = self.inputs[input_index]
        if txin.is_coinbase_input:
            raise TransactionError("cannot verify the signature of a coinbase input")
        digest = self.signature_hash(input_index)
        return ring_verify(list(txin.ring), digest, txin.signature)

    def signed_with(self, input_index: int, signature: bytes) -> Transaction:
        """Return a copy with a ring signature attached to one input."""
        inputs = list(self.inputs)
        inputs[input_index] = inputs[input_index].with_signature(signature)
        return replace(self, inputs=tuple(inputs))

    # -------------------------------------------------------------------- misc

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation, for RPC and the explorer."""
        return {
            "txid": self.txid_hex(),
            "version": self.version,
            "size": self.size(),
            "lock_time": self.lock_time,
            "coinbase": self.is_coinbase,
            "tx_public_key": self.tx_public_key.hex() if self.tx_public_key else "",
            "extra": self.extra.hex(),
            "inputs": [
                {
                    "coinbase": True,
                    "ring_size": 0,
                }
                if txin.is_coinbase_input
                else {
                    "ring_size": len(txin.ring),
                    "ring": [r.hex() for r in txin.ring],
                    "key_image": txin.key_image.hex(),
                }
                for txin in self.inputs
            ],
            "outputs": [
                {
                    "index": index,
                    "value": txout.value,
                    "one_time_key": txout.one_time_key.hex(),
                }
                for index, txout in enumerate(self.outputs)
            ],
        }