"""Tests for serialisation, proof of work, transactions and blocks."""

from __future__ import annotations

import pytest

from scarletcoin.core.block import Block, BlockError, BlockHeader, merkle_root
from scarletcoin.core.coinbase import build_coinbase, coinbase_height, encode_coinbase_data
from scarletcoin.core.params import MAINNET, NETWORKS, REGTEST, TESTNET, get_params
from scarletcoin.core.pow import (
    bits_to_target,
    block_work,
    check_proof_of_work,
    difficulty,
    next_bits,
    target_to_bits,
)
from scarletcoin.core.serialize import Reader, SerializationError, Writer
from scarletcoin.core.transaction import (
    COINBASE_OUTPOINT,
    MAX_COINBASE_DATA,
    OutPoint,
    Transaction,
    TransactionError,
    TxInput,
    TxOutput,
)
from scarletcoin.crypto.keys import PrivateKey
from scarletcoin.miner.solver import solve_block
from scarletcoin.units import format_amount, parse_amount


class TestSerialize:
    def test_integer_round_trip(self):
        writer = Writer()
        writer.uint8(0xFF).uint16(0xFFFF).uint32(0xFFFFFFFF).uint64(2**64 - 1).int32(-5)
        reader = Reader(writer.getvalue())
        assert reader.uint8() == 0xFF
        assert reader.uint16() == 0xFFFF
        assert reader.uint32() == 0xFFFFFFFF
        assert reader.uint64() == 2**64 - 1
        assert reader.int32() == -5
        reader.expect_end()

    @pytest.mark.parametrize("value", [0, 1, 0xFC, 0xFD, 0xFFFF, 0x10000, 0xFFFFFFFF, 2**40])
    def test_varint_round_trip(self, value):
        assert Reader(Writer().varint(value).getvalue()).varint() == value

    @pytest.mark.parametrize("encoded", [b"\xfd\x00\x00", b"\xfd\xfc\x00", b"\xfe\x00\x00\x00\x00"])
    def test_non_canonical_varints_are_refused(self, encoded):
        with pytest.raises(SerializationError, match="non-canonical"):
            Reader(encoded).varint()

    def test_varbytes_and_varstr(self):
        writer = Writer().varbytes(b"\x01\x02").varstr("scarlet")
        reader = Reader(writer.getvalue())
        assert reader.varbytes() == b"\x01\x02"
        assert reader.varstr() == "scarlet"

    def test_truncated_stream_is_detected(self):
        with pytest.raises(SerializationError, match="truncated"):
            Reader(b"\x01").uint32()

    def test_trailing_bytes_are_detected(self):
        reader = Reader(b"\x01\x02")
        reader.uint8()
        with pytest.raises(SerializationError, match="trailing"):
            reader.expect_end()

    def test_length_limits_are_enforced(self):
        data = Writer().varbytes(b"x" * 10).getvalue()
        with pytest.raises(SerializationError, match="too long"):
            Reader(data).varbytes(max_length=5)

    def test_invalid_utf8_is_refused(self):
        data = Writer().varbytes(b"\xff\xfe").getvalue()
        with pytest.raises(SerializationError, match="UTF-8"):
            Reader(data).varstr()


class TestProofOfWork:
    def test_compact_target_round_trip(self):
        for bits in (0x1D00FFFF, 0x1E0FFFFF, 0x207FFFFF, 0x1B0404CB):
            assert target_to_bits(bits_to_target(bits)) == bits

    def test_known_expansion(self):
        # The classic Bitcoin difficulty-1 target.
        assert bits_to_target(0x1D00FFFF) == 0x00FFFF * 256 ** (0x1D - 3)

    def test_negative_and_oversized_targets_are_refused(self):
        with pytest.raises(ValueError, match="negative"):
            bits_to_target(0x00800000 | 0x1D000000)
        with pytest.raises(ValueError, match="overflow"):
            bits_to_target(0x21000001)

    def test_work_grows_as_the_target_shrinks(self):
        assert block_work(0x1D00FFFF) > block_work(0x1E0FFFFF)
        # 0x1d00ffff is harder than the 0x1e0fffff limit, so its difficulty is above 1.
        assert difficulty(0x1D00FFFF, pow_limit=bits_to_target(0x1E0FFFFF)) > 1
        assert difficulty(0x1E0FFFFF, pow_limit=bits_to_target(0x1E0FFFFF)) == 1

    def test_check_proof_of_work(self):
        easy = 0x207FFFFF
        limit = bits_to_target(easy)
        assert check_proof_of_work(b"\x00" * 32, easy, pow_limit=limit)
        assert not check_proof_of_work(b"\xff" * 32, easy, pow_limit=limit)

    def test_a_target_easier_than_the_limit_is_refused(self):
        limit = bits_to_target(0x1E0FFFFF)
        assert not check_proof_of_work(b"\x00" * 32, 0x207FFFFF, pow_limit=limit)

    def test_retarget_speeds_up_when_blocks_were_slow(self):
        limit = bits_to_target(0x207FFFFF)
        slower = next_bits(0x1E0FFFFF, 7200, target_timespan=3600, pow_limit=limit)
        assert bits_to_target(slower) > bits_to_target(0x1E0FFFFF)

    def test_retarget_slows_down_when_blocks_were_fast(self):
        limit = bits_to_target(0x207FFFFF)
        faster = next_bits(0x1E0FFFFF, 900, target_timespan=3600, pow_limit=limit)
        assert bits_to_target(faster) < bits_to_target(0x1E0FFFFF)

    def test_retarget_is_clamped(self):
        limit = bits_to_target(0x207FFFFF)
        extreme = next_bits(0x1D00FFFF, 10**9, target_timespan=3600, pow_limit=limit)
        clamped = next_bits(0x1D00FFFF, 3600 * 4, target_timespan=3600, pow_limit=limit)
        assert extreme == clamped

    def test_retarget_never_exceeds_the_pow_limit(self):
        limit = bits_to_target(0x1E0FFFFF)
        assert (
            bits_to_target(next_bits(0x1E0FFFFF, 10**9, target_timespan=3600, pow_limit=limit))
            <= limit
        )


class TestParams:
    @pytest.mark.parametrize("name", list(NETWORKS))
    def test_genesis_is_valid(self, name):
        params = get_params(name)
        genesis = params.genesis_block
        genesis.check_sanity(pow_limit=params.pow_limit, max_block_size=params.max_block_size)
        assert genesis.header.prev_hash == b"\x00" * 32
        assert coinbase_height(genesis.coinbase) == 0

    def test_networks_have_distinct_genesis_blocks_and_magic(self):
        hashes = {params.genesis_hash for params in NETWORKS.values()}
        magics = {params.magic for params in NETWORKS.values()}
        assert len(hashes) == len(NETWORKS)
        assert len(magics) == len(NETWORKS)

    def test_unknown_network(self):
        with pytest.raises(KeyError, match="unknown network"):
            get_params("dogecoin")

    def test_subsidy_halves(self):
        assert MAINNET.subsidy(0) == 50 * 10**8
        assert MAINNET.subsidy(MAINNET.halving_interval - 1) == 50 * 10**8
        assert MAINNET.subsidy(MAINNET.halving_interval) == 25 * 10**8
        assert MAINNET.subsidy(MAINNET.halving_interval * 2) == 1250000000
        assert MAINNET.subsidy(MAINNET.halving_interval * 64) == 0

    def test_total_supply_is_bounded(self):
        total = sum(
            MAINNET.subsidy(halving * MAINNET.halving_interval) * MAINNET.halving_interval
            for halving in range(64)
        )
        assert total <= 21_000_000 * 10**8

    def test_address_prefixes(self):
        key = PrivateKey.generate()
        assert str(key.address(MAINNET.address_version)).startswith("S")
        assert str(key.address(TESTNET.address_version)).startswith("t")
        assert TESTNET.address_version == REGTEST.address_version


class TestTransaction:
    def _payment(self, key: PrivateKey) -> Transaction:
        unsigned = Transaction(
            inputs=(TxInput(OutPoint(b"\x11" * 32, 0)),),
            outputs=(TxOutput.p2pkh(1000, key.public_key().hash160()),),
        )
        digest = unsigned.signature_hash(
            0, 5000, unsigned.p2pkh_script_code(key.public_key().hash160())
        )
        return unsigned.signed_with({0: (key.public_key().to_bytes(), key.sign(digest))})

    def test_serialisation_round_trip(self, key):
        transaction = self._payment(key)
        assert Transaction.deserialize(transaction.serialize()) == transaction

    def test_txid_ignores_the_signature(self, key):
        first = self._payment(key)
        second = self._payment(key)
        assert first.txid() == second.txid()

    def test_txid_display_order_is_reversed(self, key):
        transaction = self._payment(key)
        assert transaction.txid_hex() == transaction.txid()[::-1].hex()

    def test_signature_verifies(self, key):
        transaction = self._payment(key)
        assert transaction.verify_input_signature(0, 5000, key.public_key().hash160())

    def test_signature_is_bound_to_the_spent_value(self, key):
        transaction = self._payment(key)
        assert not transaction.verify_input_signature(0, 5001, key.public_key().hash160())

    def test_signature_is_bound_to_the_input_index(self, key):
        unsigned = Transaction(
            inputs=(TxInput(OutPoint(b"\x11" * 32, 0)), TxInput(OutPoint(b"\x22" * 32, 1))),
            outputs=(TxOutput.p2pkh(1000, key.public_key().hash160()),),
        )
        signature = key.sign(
            unsigned.signature_hash(0, 5000, unsigned.p2pkh_script_code(key.public_key().hash160()))
        )
        moved = unsigned.signed_with({1: (key.public_key().to_bytes(), signature)})
        assert not moved.verify_input_signature(1, 5000, key.public_key().hash160())

    def test_signature_covers_the_outputs(self, key):
        transaction = self._payment(key)
        tampered = Transaction(
            version=transaction.version,
            inputs=transaction.inputs,
            outputs=(TxOutput.p2pkh(999_999, key.public_key().hash160()),),
            lock_time=transaction.lock_time,
        )
        assert not tampered.verify_input_signature(0, 5000, key.public_key().hash160())

    def test_sanity_rejects_empty_transactions(self):
        with pytest.raises(TransactionError, match="no inputs"):
            Transaction(outputs=(TxOutput.p2pkh(1, b"\x00" * 20),)).check_sanity()
        with pytest.raises(TransactionError, match="no outputs"):
            Transaction(inputs=(TxInput(OutPoint(b"\x11" * 32, 0)),)).check_sanity()

    def test_sanity_rejects_duplicate_inputs(self, key):
        prevout = OutPoint(b"\x11" * 32, 0)
        witness = (key.public_key().to_bytes(), b"\x01" * 64)
        transaction = Transaction(
            inputs=(
                TxInput(prevout, witness=witness),
                TxInput(prevout, witness=witness),
            ),
            outputs=(TxOutput.p2pkh(1, b"\x00" * 20),),
        )
        with pytest.raises(TransactionError, match="spent twice"):
            transaction.check_sanity()

    def test_negative_and_oversized_outputs_are_impossible(self):
        with pytest.raises(TransactionError, match="negative"):
            TxOutput.p2pkh(-1, b"\x00" * 20)
        with pytest.raises(TransactionError, match="maximum money supply"):
            TxOutput.p2pkh(21_000_001 * 10**8, b"\x00" * 20)

    def test_output_values_must_be_integers(self):
        with pytest.raises(TransactionError, match="integer"):
            TxOutput.p2pkh(1.5, b"\x00" * 20)

    def test_coinbase_detection(self):
        coinbase = build_coinbase(height=7, reward=100, pubkey_hash=b"\x01" * 20)
        assert coinbase.is_coinbase
        assert coinbase_height(coinbase) == 7
        coinbase.check_sanity()

    def test_coinbase_data_is_size_limited(self):
        with pytest.raises(TransactionError, match="coinbase data"):
            encode_coinbase_data(1, b"x" * MAX_COINBASE_DATA)

    def test_only_a_coinbase_may_carry_coinbase_data(self, key):
        transaction = Transaction(
            inputs=(
                TxInput(
                    OutPoint(b"\x11" * 32, 0),
                    witness=(key.public_key().to_bytes(), b"\x00" * 64),
                ),
            ),
            outputs=(TxOutput.p2pkh(1, b"\x00" * 20),),
            coinbase_data=b"hello",
        )
        with pytest.raises(TransactionError, match="only a coinbase"):
            transaction.check_sanity()

    def test_coinbase_must_not_carry_a_witness(self, key):
        transaction = Transaction(
            inputs=(
                TxInput(
                    COINBASE_OUTPOINT,
                    witness=(key.public_key().to_bytes(), b"\x00" * 64),
                ),
            ),
            outputs=(TxOutput.p2pkh(1, b"\x00" * 20),),
            coinbase_data=encode_coinbase_data(1),
        )
        with pytest.raises(TransactionError, match="must not carry a witness"):
            transaction.check_sanity()

    def test_deserialise_refuses_trailing_data(self, key):
        raw = self._payment(key).serialize()
        with pytest.raises(SerializationError):
            Transaction.deserialize(raw + b"\x00")

    def test_to_dict(self, key):
        data = self._payment(key).to_dict(127)
        assert data["outputs"][0]["address"].startswith("t")
        assert data["inputs"][0]["index"] == 0


class TestBlock:
    def test_merkle_root_of_one(self):
        assert merkle_root([b"\x01" * 32]) == b"\x01" * 32

    def test_merkle_root_changes_with_order(self):
        first, second = b"\x01" * 32, b"\x02" * 32
        assert merkle_root([first, second]) != merkle_root([second, first])

    def test_merkle_root_duplicates_odd_nodes(self):
        from scarletcoin.crypto.hashing import hash256

        leaves = [b"\x01" * 32, b"\x02" * 32, b"\x03" * 32]
        left = hash256(leaves[0] + leaves[1])
        right = hash256(leaves[2] + leaves[2])
        assert merkle_root(leaves) == hash256(left + right)

    def test_empty_merkle_root_is_an_error(self):
        with pytest.raises(BlockError):
            merkle_root([])

    def test_header_round_trip(self):
        header = MAINNET.genesis_block.header
        assert BlockHeader.deserialize(header.serialize()) == header
        assert len(header.serialize()) == 80

    def test_header_fields_are_range_checked(self):
        with pytest.raises(BlockError, match="uint32"):
            BlockHeader(2**32, b"\x00" * 32, b"\x00" * 32, 0, 0, 0)
        with pytest.raises(BlockError, match="32 bytes"):
            BlockHeader(1, b"\x00", b"\x00" * 32, 0, 0, 0)

    def test_block_round_trip(self):
        block = REGTEST.genesis_block
        assert Block.deserialize(block.serialize()) == block

    def test_sanity_detects_a_wrong_merkle_root(self):
        genesis = REGTEST.genesis_block
        broken = genesis.with_header(
            BlockHeader(
                genesis.header.version,
                genesis.header.prev_hash,
                b"\x07" * 32,
                genesis.header.timestamp,
                genesis.header.bits,
                genesis.header.nonce,
            )
        )
        solved = solve_block(broken)
        assert solved is not None
        with pytest.raises(BlockError, match="Merkle root"):
            solved.check_sanity(pow_limit=REGTEST.pow_limit, max_block_size=1_000_000)

    def test_sanity_detects_bad_proof_of_work(self):
        genesis = MAINNET.genesis_block
        broken = genesis.with_header(genesis.header.with_nonce(genesis.header.nonce + 1))
        with pytest.raises(BlockError, match="proof-of-work"):
            broken.check_sanity(pow_limit=MAINNET.pow_limit, max_block_size=1_000_000)

    def test_sanity_enforces_the_size_limit(self):
        genesis = REGTEST.genesis_block
        with pytest.raises(BlockError, match="too large"):
            genesis.check_sanity(pow_limit=REGTEST.pow_limit, max_block_size=10)

    def test_a_block_needs_a_coinbase_first(self, key):
        genesis = REGTEST.genesis_block
        payment = Transaction(
            inputs=(
                TxInput(
                    OutPoint(b"\x11" * 32, 0),
                    witness=(key.public_key().to_bytes(), b"\x00" * 64),
                ),
            ),
            outputs=(TxOutput.p2pkh(1, b"\x00" * 20),),
        )
        block = solve_block(
            Block.create(
                prev_hash=genesis.hash(),
                transactions=[payment],
                bits=REGTEST.genesis_bits,
                timestamp=genesis.header.timestamp + 1,
            )
        )
        assert block is not None
        with pytest.raises(BlockError, match="must be the coinbase"):
            block.check_sanity(pow_limit=REGTEST.pow_limit, max_block_size=1_000_000)


class TestUnits:
    @pytest.mark.parametrize(
        ("scar", "text"),
        [(0, "0"), (1, "0.00000001"), (10**8, "1"), (1_234_500_000, "12.345")],
    )
    def test_format(self, scar, text):
        assert format_amount(scar) == text

    def test_format_with_symbol(self):
        assert format_amount(10**8, symbol=True) == "1 SCT"

    @pytest.mark.parametrize(
        ("text", "scar"),
        [("1", 10**8), ("0.00000001", 1), (" 12.345 SCT ", 1_234_500_000), ("0", 0)],
    )
    def test_parse(self, text, scar):
        assert parse_amount(text) == scar

    @pytest.mark.parametrize("text", ["", "abc", "-1", "0.000000001", "21000001"])
    def test_parse_rejects_junk(self, text):
        with pytest.raises(ValueError):
            parse_amount(text)

    def test_round_trip(self):
        for scar in (0, 1, 12345, 10**8, 21_000_000 * 10**8):
            assert parse_amount(format_amount(scar)) == scar
