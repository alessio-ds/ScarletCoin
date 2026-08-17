"""Transactions.

ScarletCoin uses the UTXO model.  A transaction spends whole *unspent outputs*
created by earlier transactions and creates new ones.  An output is either a
pay-to-public-key-hash or a pay-to-script-hash (P2SH); spending one means
satisfying the lock with a *witness*: a stack of data items whose meaning
depends on the output type.

* A P2PKH output is spent by a witness of ``[public key, signature]``.
* A P2SH output is spent by ``[redeem script, …arguments]``; the redeem script
  is a small program (see :mod:`scarletcoin.core.script`) that must run to a
  truthy result.

Serialisation is split in two parts:

* the **body** — version, inputs' outpoints and sequence numbers, outputs,
  lock time and (for a coinbase) the miner's arbitrary data;
* the **witnesses** — the data items of each input.

The transaction id is the double SHA-256 of the *body only*.  Signatures are
therefore not covered by the txid, which makes ids immune to signature
malleability while still committing to everything that matters economically.
The ``sequence`` field enables replace-by-fee: an input with a sequence below
``SEQUENCE_FINAL`` signals that its transaction may be replaced by one paying a
higher fee rate.
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
    "MAX_WITNESS_ITEMS",
    "MAX_WITNESS_ITEM_SIZE",
    "OUTPUT_P2PKH",
    "OUTPUT_P2SH",
    "SEQUENCE_FINAL",
    "OutPoint",
    "Transaction",
    "TransactionError",
    "TxInput",
    "TxOutput",
]

#: Domain separation tag mixed into every signature hash.
_SIGHASH_TAG: Final[bytes] = b"ScarletCoin/sighash/2"

#: Output types.
OUTPUT_P2PKH: Final[int] = 0
OUTPUT_P2SH: Final[int] = 1

#: Sequence number of a final, non-replaceable input.
SEQUENCE_FINAL: Final[int] = 0xFFFFFFFF

#: Largest amount that may ever exist, in the smallest unit ("scar").
MAX_MONEY: Final[int] = 21_000_000 * 100_000_000

#: Maximum length of the arbitrary data a miner may embed in a coinbase.
MAX_COINBASE_DATA: Final[int] = 100

#: Limits on witness data.
MAX_WITNESS_ITEMS: Final[int] = 100
MAX_WITNESS_ITEM_SIZE: Final[int] = 520

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
    """One authorisation to spend a previous output."""

    prevout: OutPoint
    sequence: int = SEQUENCE_FINAL
    witness: tuple[bytes, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not 0 <= self.sequence <= 0xFFFFFFFF:
            raise TransactionError(f"sequence out of range: {self.sequence}")
        object.__setattr__(self, "witness", tuple(self.witness))

    @property
    def is_replaceable(self) -> bool:
        """``True`` if this input signals replace-by-fee eligibility."""
        return self.sequence < SEQUENCE_FINAL - 1

    def check_witness_shape(self) -> None:
        """Validate witness sizes before touching any curve arithmetic.

        Raises:
            TransactionError: if the witness has too many items or one is too long.
        """
        if len(self.witness) > MAX_WITNESS_ITEMS:
            raise TransactionError(
                f"witness has {len(self.witness)} items, the limit is {MAX_WITNESS_ITEMS}"
            )
        for item in self.witness:
            if len(item) > MAX_WITNESS_ITEM_SIZE:
                raise TransactionError(
                    f"witness item is {len(item)} bytes, the limit is {MAX_WITNESS_ITEM_SIZE}"
                )


@dataclass(frozen=True, slots=True)
class TxOutput:
    """A spendable amount locked to a public-key hash or a script hash."""

    type: int
    value: int
    payload: bytes

    def __post_init__(self) -> None:
        if self.type not in (OUTPUT_P2PKH, OUTPUT_P2SH):
            raise TransactionError(f"unknown output type: {self.type}")
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise TransactionError("output value must be an integer")
        if self.value < 0:
            raise TransactionError("output value must not be negative")
        if self.value > MAX_MONEY:
            raise TransactionError("output value exceeds the maximum money supply")
        if len(self.payload) != PUBKEY_HASH_LENGTH:
            raise TransactionError(
                f"output payload must be {PUBKEY_HASH_LENGTH} bytes, got {len(self.payload)}"
            )

    @classmethod
    def p2pkh(cls, value: int, pubkey_hash: bytes) -> TxOutput:
        """Create an output paying ``value`` to ``pubkey_hash``."""
        return cls(OUTPUT_P2PKH, value, pubkey_hash)

    @classmethod
    def p2sh(cls, value: int, script_hash: bytes) -> TxOutput:
        """Create an output paying ``value`` to ``script_hash``."""
        return cls(OUTPUT_P2SH, value, script_hash)

    @classmethod
    def to_address(cls, address: Address, value: int) -> TxOutput:
        """Create an output paying ``value`` to ``address``."""
        return cls(OUTPUT_P2PKH, value, address.hash)

    @property
    def is_p2sh(self) -> bool:
        """``True`` for a pay-to-script-hash output."""
        return self.type == OUTPUT_P2SH

    def address(self, version: int) -> Address:
        """Return the address this output pays, for the given network version.

        P2SH outputs should use the network's ``script_address_version``; P2PKH
        outputs use ``address_version``.
        """
        return Address(version, self.payload)


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

    @property
    def is_replaceable(self) -> bool:
        """``True`` if every input allows replace-by-fee."""
        return bool(self.inputs) and all(txin.is_replaceable for txin in self.inputs)

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
            if self.inputs[0].witness:
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
            writer.hash32(txin.prevout.txid).uint32(txin.prevout.index).uint32(txin.sequence)
        writer.varint(len(self.outputs))
        for txout in self.outputs:
            writer.uint8(txout.type).uint64(txout.value).raw(txout.payload)
        writer.uint32(self.lock_time)
        writer.varbytes(self.coinbase_data)
        return writer.getvalue()

    def serialize(self) -> bytes:
        """Serialise the transaction, witnesses included (wire and disk format)."""
        writer = Writer()
        writer.raw(self.serialize_body())
        for txin in self.inputs:
            writer.varint(len(txin.witness))
            for item in txin.witness:
                writer.varbytes(item)
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
        if input_count > reader.remaining // 40 + 1:
            raise SerializationError("input count is larger than the remaining data")
        inputs = tuple(
            TxInput(OutPoint(reader.hash32(), reader.uint32()), reader.uint32())
            for _ in range(input_count)
        )
        output_count = reader.varint()
        if output_count == 0:
            raise SerializationError("transaction has no outputs")
        if output_count > reader.remaining // 29 + 1:
            raise SerializationError("output count is larger than the remaining data")
        outputs = tuple(
            TxOutput(reader.uint8(), reader.uint64(), reader.raw(PUBKEY_HASH_LENGTH))
            for _ in range(output_count)
        )
        lock_time = reader.uint32()
        coinbase_data = reader.varbytes(max_length=MAX_COINBASE_DATA)
        inputs = tuple(
            TxInput(
                txin.prevout,
                txin.sequence,
                tuple(
                    reader.varbytes(max_length=MAX_WITNESS_ITEM_SIZE)
                    for _ in range(reader.varint())
                ),
            )
            for txin in inputs
        )
        return cls(version, inputs, outputs, lock_time, coinbase_data)

    # ----------------------------------------------------------------- hashes

    def txid(self) -> bytes:
        """Return the transaction id: double SHA-256 of the body."""
        return hash256(self.serialize_body())

    def txid_hex(self) -> str:
        """Return the transaction id as a big-endian hex string (display order)."""
        return self.txid()[::-1].hex()

    def signature_hash(self, input_index: int, prevout_value: int, script_code: bytes) -> bytes:
        """Return the digest input ``input_index`` must sign.

        The digest commits to the whole body, the index and value of the output
        being spent, and the *script code*: the type-and-payload of a P2PKH
        output or the full redeem script of a P2SH output.  A signature therefore
        cannot be replayed on another input, another transaction, another output
        of a different size, or a different kind of lock.
        """
        if not 0 <= input_index < len(self.inputs):
            raise TransactionError(f"no input at index {input_index}")
        writer = Writer()
        writer.varbytes(_SIGHASH_TAG)
        writer.raw(self.serialize_body())
        writer.uint32(input_index)
        writer.uint64(prevout_value)
        writer.varbytes(script_code)
        return hash256(writer.getvalue())

    @staticmethod
    def p2pkh_script_code(pubkey_hash: bytes) -> bytes:
        """Return the script code for a P2PKH output paying ``pubkey_hash``."""
        return bytes([OUTPUT_P2PKH]) + pubkey_hash

    def size(self) -> int:
        """Serialised size in bytes."""
        return len(self.serialize())

    # ------------------------------------------------------------------ misc

    def signed_with(self, witnesses: dict[int, tuple[bytes, ...]]) -> Transaction:
        """Return a copy with witness data attached to the given input indexes."""
        inputs = list(self.inputs)
        for index, witness in witnesses.items():
            inputs[index] = replace(inputs[index], witness=tuple(witness))
        return replace(self, inputs=tuple(inputs))

    def verify_input_signature(
        self, input_index: int, prevout_value: int, pubkey_hash: bytes
    ) -> bool:
        """Check one P2PKH input's signature against the public key it reveals.

        ``pubkey_hash`` is the 20-byte digest the output being spent commits to;
        the revealed public key must hash to it, and the signature must commit to
        it through the script code.
        """
        txin = self.inputs[input_index]
        if len(txin.witness) != 2:
            return False
        public_key_bytes, signature = txin.witness
        if len(public_key_bytes) != PUBLIC_KEY_LENGTH or len(signature) != SIGNATURE_LENGTH:
            return False
        try:
            public_key = PublicKey.from_bytes(public_key_bytes)
        except ValueError:
            return False
        if public_key.hash160() != pubkey_hash:
            return False
        digest = self.signature_hash(
            input_index, prevout_value, self.p2pkh_script_code(pubkey_hash)
        )
        return public_key.verify(digest, signature)

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
                    "sequence": txin.sequence,
                }
                for txin in self.inputs
            ],
            "outputs": [
                {
                    "index": index,
                    "value": txout.value,
                    "type": "p2pkh" if txout.type == OUTPUT_P2PKH else "p2sh",
                    "address": str(txout.address(address_version)),
                }
                for index, txout in enumerate(self.outputs)
            ],
        }
