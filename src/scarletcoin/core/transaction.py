"""Transactions.

ScarletCoin uses the UTXO model.  A transaction spends whole *unspent outputs*
created by earlier transactions and creates new ones.  There is no scripting
language: every output simply commits to a public-key hash, and an input is
authorised by revealing the matching compressed public key together with a
signature over the transaction's signature hash.  This keeps validation small
enough to audit while giving the same security properties as Bitcoin's
pay-to-public-key-hash.

Serialisation is split in two parts:

* the **body** — version, inputs' outpoints, outputs, lock time and (for a
  coinbase) the miner's arbitrary data;
* the **witnesses** — the public key and signature of each input.

The transaction id is the double SHA-256 of the *body only*.  Signatures are
therefore not covered by the txid, which makes ids immune to signature
malleability while still committing to everything that matters economically.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Final

from scarletcoin.core.serialize import Reader, SerializationError, Writer
from scarletcoin.crypto.hashing import PUBKEY_HASH_LENGTH, hash256
from scarletcoin.crypto.keys import PUBLIC_KEY_LENGTH, SIGNATURE_LENGTH, Address, PublicKey

__all__ = [
    "COINBASE_OUTPOINT",
    "MAX_COINBASE_DATA",
    "MAX_MONEY",
    "OutPoint",
    "Transaction",
    "TransactionError",
    "TxInput",
    "TxOutput",
]

#: Domain separation tag mixed into every signature hash.
_SIGHASH_TAG: Final[bytes] = b"ScarletCoin/sighash/1"

#: Largest amount that may ever exist, in the smallest unit ("scar").
MAX_MONEY: Final[int] = 21_000_000 * 100_000_000

#: Maximum length of the arbitrary data a miner may embed in a coinbase.
MAX_COINBASE_DATA: Final[int] = 100

_NULL_HASH: Final[bytes] = b"\x00" * 32
_NULL_INDEX: Final[int] = 0xFFFFFFFF


class TransactionError(ValueError):
    """Raised when a transaction is structurally invalid."""


@dataclass(frozen=True, slots=True, order=True)
class OutPoint:
    """A reference to one output of an earlier transaction."""

    txid: bytes
    index: int

    def __post_init__(self) -> None:
        if len(self.txid) != 32:
            raise TransactionError("outpoint txid must be 32 bytes")
        if not 0 <= self.index <= 0xFFFFFFFF:
            raise TransactionError(f"outpoint index out of range: {self.index}")

    @property
    def is_null(self) -> bool:
        """``True`` for the special outpoint used by coinbase inputs."""
        return self.txid == _NULL_HASH and self.index == _NULL_INDEX

    def __str__(self) -> str:
        return f"{self.txid.hex()}:{self.index}"


#: The outpoint every coinbase input must reference.
COINBASE_OUTPOINT: Final[OutPoint] = OutPoint(_NULL_HASH, _NULL_INDEX)


@dataclass(frozen=True, slots=True)
class TxInput:
    """An authorisation to spend one previous output."""

    prevout: OutPoint
    public_key: bytes = b""
    signature: bytes = b""

    def with_witness(self, public_key: bytes, signature: bytes) -> TxInput:
        """Return a copy of this input carrying the given witness data."""
        return replace(self, public_key=bytes(public_key), signature=bytes(signature))

    def check_witness_shape(self) -> None:
        """Validate the witness sizes before touching any curve arithmetic.

        Raises:
            TransactionError: if the public key or signature has the wrong length.
        """
        if len(self.public_key) != PUBLIC_KEY_LENGTH:
            raise TransactionError(
                f"input public key must be {PUBLIC_KEY_LENGTH} bytes, got {len(self.public_key)}"
            )
        if len(self.signature) != SIGNATURE_LENGTH:
            raise TransactionError(
                f"input signature must be {SIGNATURE_LENGTH} bytes, got {len(self.signature)}"
            )


@dataclass(frozen=True, slots=True)
class TxOutput:
    """A spendable amount locked to a public-key hash."""

    value: int
    pubkey_hash: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TransactionError("output value must be an integer")
        if self.value < 0:
            raise TransactionError("output value must not be negative")
        if self.value > MAX_MONEY:
            raise TransactionError("output value exceeds the maximum money supply")
        if len(self.pubkey_hash) != PUBKEY_HASH_LENGTH:
            raise TransactionError(
                f"output pubkey hash must be {PUBKEY_HASH_LENGTH} bytes,"
                f" got {len(self.pubkey_hash)}"
            )

    @classmethod
    def to_address(cls, address: Address, value: int) -> TxOutput:
        """Create an output paying ``value`` to ``address``."""
        return cls(value, address.hash)

    def address(self, version: int) -> Address:
        """Return the address this output pays, for the given network version."""
        return Address(version, self.pubkey_hash)


@dataclass(frozen=True, slots=True)
class Transaction:
    """A signed transfer of value."""

    version: int = 1
    inputs: tuple[TxInput, ...] = field(default_factory=tuple)
    outputs: tuple[TxOutput, ...] = field(default_factory=tuple)
    lock_time: int = 0
    coinbase_data: bytes = b""

    def __post_init__(self) -> None:
        if not 0 <= self.version <= 0xFFFFFFFF:
            raise TransactionError(f"transaction version out of range: {self.version}")
        if not 0 <= self.lock_time <= 0xFFFFFFFF:
            raise TransactionError(f"lock time out of range: {self.lock_time}")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "coinbase_data", bytes(self.coinbase_data))

    # ------------------------------------------------------------------ shape

    @property
    def is_coinbase(self) -> bool:
        """``True`` if this transaction mints the block reward."""
        return len(self.inputs) == 1 and self.inputs[0].prevout.is_null

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
            if len(self.coinbase_data) > MAX_COINBASE_DATA:
                raise TransactionError(f"coinbase data must be at most {MAX_COINBASE_DATA} bytes")
            if self.inputs[0].public_key or self.inputs[0].signature:
                raise TransactionError("coinbase input must not carry a witness")
            return

        if self.coinbase_data:
            raise TransactionError("only a coinbase transaction may carry coinbase data")
        seen: set[OutPoint] = set()
        for txin in self.inputs:
            if txin.prevout.is_null:
                raise TransactionError("only the first input of a coinbase may be null")
            if txin.prevout in seen:
                raise TransactionError(f"input {txin.prevout} is spent twice in one transaction")
            seen.add(txin.prevout)
            txin.check_witness_shape()

    # ---------------------------------------------------------- serialisation

    def serialize_body(self) -> bytes:
        """Serialise everything the transaction id commits to."""
        writer = Writer()
        writer.uint32(self.version)
        writer.varint(len(self.inputs))
        for txin in self.inputs:
            writer.hash32(txin.prevout.txid).uint32(txin.prevout.index)
        writer.varint(len(self.outputs))
        for txout in self.outputs:
            writer.uint64(txout.value).raw(txout.pubkey_hash)
        writer.uint32(self.lock_time)
        writer.varbytes(self.coinbase_data)
        return writer.getvalue()

    def serialize(self) -> bytes:
        """Serialise the transaction, witnesses included (wire and disk format)."""
        writer = Writer()
        writer.raw(self.serialize_body())
        for txin in self.inputs:
            writer.varbytes(txin.public_key).varbytes(txin.signature)
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
        if input_count > reader.remaining // 36 + 1:
            raise SerializationError("input count is larger than the remaining data")
        prevouts = [OutPoint(reader.hash32(), reader.uint32()) for _ in range(input_count)]
        output_count = reader.varint()
        if output_count == 0:
            raise SerializationError("transaction has no outputs")
        if output_count > reader.remaining // 28 + 1:
            raise SerializationError("output count is larger than the remaining data")
        outputs = tuple(
            TxOutput(reader.uint64(), reader.raw(PUBKEY_HASH_LENGTH)) for _ in range(output_count)
        )
        lock_time = reader.uint32()
        coinbase_data = reader.varbytes(max_length=MAX_COINBASE_DATA)
        inputs = tuple(
            TxInput(
                prevout,
                reader.varbytes(max_length=PUBLIC_KEY_LENGTH),
                reader.varbytes(max_length=SIGNATURE_LENGTH),
            )
            for prevout in prevouts
        )
        return cls(version, inputs, outputs, lock_time, coinbase_data)

    # ----------------------------------------------------------------- hashes

    def txid(self) -> bytes:
        """Return the transaction id: double SHA-256 of the body."""
        return hash256(self.serialize_body())

    def txid_hex(self) -> str:
        """Return the transaction id as a big-endian hex string (display order)."""
        return self.txid()[::-1].hex()

    def signature_hash(self, input_index: int, prevout_value: int) -> bytes:
        """Return the digest input ``input_index`` must sign.

        The digest commits to the whole body plus the index and value of the
        output being spent, so a signature cannot be replayed on another input,
        another transaction, or an output of a different size.
        """
        if not 0 <= input_index < len(self.inputs):
            raise TransactionError(f"no input at index {input_index}")
        writer = Writer()
        writer.varbytes(_SIGHASH_TAG)
        writer.raw(self.serialize_body())
        writer.uint32(input_index)
        writer.uint64(prevout_value)
        return hash256(writer.getvalue())

    def size(self) -> int:
        """Serialised size in bytes."""
        return len(self.serialize())

    # ------------------------------------------------------------------ misc

    def signed_with(self, witnesses: dict[int, tuple[bytes, bytes]]) -> Transaction:
        """Return a copy with witness data attached to the given input indexes."""
        inputs = list(self.inputs)
        for index, (public_key, signature) in witnesses.items():
            inputs[index] = inputs[index].with_witness(public_key, signature)
        return replace(self, inputs=tuple(inputs))

    def verify_input_signature(self, input_index: int, prevout_value: int) -> bool:
        """Check one input's signature against the public key it reveals."""
        txin = self.inputs[input_index]
        txin.check_witness_shape()
        public_key = PublicKey.from_bytes(txin.public_key)
        digest = self.signature_hash(input_index, prevout_value)
        return public_key.verify(digest, txin.signature)

    def to_dict(self, address_version: int) -> dict:
        """Return a JSON-friendly representation, for RPC and the explorer."""
        return {
            "txid": self.txid_hex(),
            "version": self.version,
            "size": self.size(),
            "lock_time": self.lock_time,
            "coinbase": self.is_coinbase,
            "coinbase_data": self.coinbase_data.hex(),
            "inputs": [
                {"coinbase": True}
                if txin.prevout.is_null
                else {
                    "txid": txin.prevout.txid[::-1].hex(),
                    "index": txin.prevout.index,
                    "public_key": txin.public_key.hex(),
                }
                for txin in self.inputs
            ],
            "outputs": [
                {
                    "index": index,
                    "value": txout.value,
                    "address": str(txout.address(address_version)),
                }
                for index, txout in enumerate(self.outputs)
            ],
        }
