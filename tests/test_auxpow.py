"""Tests for AuxPoW (merged mining) structures and validation."""

from __future__ import annotations

from dataclasses import replace

import pytest

from scarletcoin.core.auxpow import (
    MAX_MERKLE_BRANCH_DEPTH,
    MERGED_MINING_HEADER,
    AuxPoW,
    AuxPoWCommitment,
    AuxPoWError,
    ParentBlockHeader,
    build_auxpow_commitment,
    check_merkle_branch,
    get_expected_index,
    parse_auxpow_commitment,
    validate_auxpow,
)
from scarletcoin.core.block import Block, merkle_root
from scarletcoin.core.chain import BlockStatus
from scarletcoin.core.coinbase import build_coinbase, encode_coinbase_data
from scarletcoin.core.params import REGTEST
from scarletcoin.core.pow import bits_to_target
from scarletcoin.core.template import create_aux_block
from scarletcoin.core.transaction import COINBASE_OUTPOINT, Transaction, TxInput, TxOutput
from scarletcoin.crypto.hashing import hash256
from scarletcoin.crypto.keys import PrivateKey
from tests.helpers import make_chain, make_node_state, mine_and_add, mine_block

# ------------------------------------------------------------------- helpers


def _make_coinbase_tx(height: int, value: int, script_data: bytes) -> Transaction:
    """Build a minimal parent (Bitcoin-style) coinbase."""
    return Transaction(
        version=1,
        inputs=(TxInput(COINBASE_OUTPOINT),),
        outputs=(TxOutput.p2pkh(value, b"\x01" * 20),),
        coinbase_data=encode_coinbase_data(height, script_data),
    )


def _make_parent_header(
    prev_hash: bytes | None = None,
    merkle_root: bytes | None = None,
    bits: int = 0x207FFFFF,
    nonce: int = 0,
) -> ParentBlockHeader:
    """Build a parent Bitcoin-style header with dummy fields."""
    return ParentBlockHeader(
        version=1,
        prev_hash=prev_hash or b"\x11" * 32,
        merkle_root=merkle_root or b"\x22" * 32,
        timestamp=1_700_000_000,
        bits=bits,
        nonce=nonce,
    )


def _solve_parent(ph: ParentBlockHeader, target: int) -> ParentBlockHeader:
    """Find a nonce that makes ``ph.hash()`` <= ``target`` (trivial for regtest)."""
    nonce = 0
    while nonce < 1_000_000:
        candidate = replace(ph, nonce=nonce)
        if int.from_bytes(candidate.hash(), "little") <= target:
            return candidate
        nonce += 1
    raise RuntimeError("could not solve parent header")


def _sc_block_hash(key: PrivateKey) -> tuple[bytes, int]:
    """Build a minimal ScarletCoin block and return (hash, target)."""
    sc_cb = build_coinbase(
        height=1,
        reward=50 * 10**8,
        pubkey_hash=key.public_key().hash160(),
        extra=b"test",
    )
    sc_block = Block.create(
        prev_hash=REGTEST.genesis_hash,
        transactions=[sc_cb],
        bits=REGTEST.pow_limit_bits,
        timestamp=1_700_000_001,
        version=1,
        nonce=0,
    )
    return sc_block.hash(), bits_to_target(sc_block.header.bits)


# ------------------------------------------------------- Merkle branch tests


class TestMerkleBranch:
    def test_empty_branch_returns_leaf(self):
        leaf = b"\xaa" * 32
        assert check_merkle_branch(leaf, (), 0) == leaf

    def test_single_level_left(self):
        left = b"\x11" * 32
        right = b"\x22" * 32
        root = hash256(left + right)
        assert check_merkle_branch(left, (right,), 0) == root

    def test_single_level_right(self):
        left = b"\x11" * 32
        right = b"\x22" * 32
        root = hash256(left + right)
        assert check_merkle_branch(right, (left,), 1) == root

    def test_two_levels(self):
        a, b, c, d = (bytes([i] * 32) for i in (0xAA, 0xBB, 0xCC, 0xDD))
        left = hash256(a + b)
        right = hash256(c + d)
        root = hash256(left + right)
        # a is at index 0: branch is [b, right]
        branch = (b, right)
        assert check_merkle_branch(a, branch, 0) == root
        # d is at index 3: branch is [c, left]
        branch = (c, left)
        assert check_merkle_branch(d, branch, 3) == root

    def test_branch_too_deep_raises(self):
        branch = tuple(b"\x00" * 32 for _ in range(MAX_MERKLE_BRANCH_DEPTH + 1))
        with pytest.raises(AuxPoWError, match=r"branch.*levels deep"):
            check_merkle_branch(b"\x00" * 32, branch, 0)

    def test_non_32_byte_hash_raises(self):
        with pytest.raises(AuxPoWError, match="must be 32 bytes"):
            check_merkle_branch(b"\x00" * 31, (), 0)


# --------------------------------------------------- ParentBlockHeader tests


class TestParentBlockHeader:
    def test_serialize_is_exactly_80_bytes(self):
        ph = _make_parent_header()
        assert len(ph.serialize()) == 80

    def test_hash_is_sha256d(self):
        ph = _make_parent_header()
        expected = hash256(ph.serialize())
        assert ph.hash() == expected

    def test_round_trip(self):
        ph = _make_parent_header(nonce=42)
        ph2 = ParentBlockHeader.deserialize(ph.serialize())
        assert ph2 == ph
        assert ph2.hash() == ph.hash()

    def test_rejects_non_32_byte_hashes(self):
        with pytest.raises(AuxPoWError, match="must be 32 bytes"):
            _make_parent_header(prev_hash=b"\x00" * 31)

    def test_hash_hex_is_big_endian(self):
        ph = _make_parent_header()
        assert ph.hash_hex() == ph.hash()[::-1].hex()

    def test_rejects_out_of_range_fields(self):
        """Dataclass post_init rejects out-of-range uint32 fields."""
        with pytest.raises(AuxPoWError):
            ParentBlockHeader(-1, b"\x00" * 32, b"\x00" * 32, 0, 0, 0)
        with pytest.raises(AuxPoWError):
            ParentBlockHeader(1, b"\x00" * 32, b"\x00" * 32, -1, 0, 0)


# ---------------------------------------------------------- commitment tests


class TestCommitment:
    def test_build_and_parse_round_trip(self):
        aux_root = b"\x99" * 32
        commitment_bytes = build_auxpow_commitment(aux_root, tree_size=1, nonce=42)
        coinbase_data = b"prefix" + commitment_bytes + b"suffix"
        parsed = parse_auxpow_commitment(coinbase_data)
        assert parsed is not None
        assert parsed.aux_root == aux_root
        assert parsed.tree_size == 1
        assert parsed.nonce == 42

    def test_starts_with_magic(self):
        commitment = build_auxpow_commitment(b"\x00" * 32, 1, 0)
        assert commitment[:4] == MERGED_MINING_HEADER

    def test_parse_none_without_magic(self):
        assert parse_auxpow_commitment(b"hello world") is None

    def test_parse_none_on_empty(self):
        assert parse_auxpow_commitment(b"") is None

    def test_duplicate_commitment_raises(self):
        aux_root = b"\x99" * 32
        c1 = build_auxpow_commitment(aux_root, 1, 0)
        data = c1 + c1
        with pytest.raises(AuxPoWError, match="found 2"):
            parse_auxpow_commitment(data)

    def test_truncated_commitment_skipped(self):
        # Magic marker followed by too few bytes
        data = MERGED_MINING_HEADER + b"\x00" * 10
        assert parse_auxpow_commitment(data) is None

    def test_commitment_validates_range(self):
        c = AuxPoWCommitment(b"\x00" * 32, tree_size=1, nonce=0)
        assert c.tree_size == 1
        with pytest.raises(AuxPoWError):
            AuxPoWCommitment(b"\x00" * 32, tree_size=0, nonce=0)

    def test_aux_tree_height(self):
        assert AuxPoWCommitment(b"\x00" * 32, 1, 0).aux_tree_height == 0
        assert AuxPoWCommitment(b"\x00" * 32, 2, 0).aux_tree_height == 1
        assert AuxPoWCommitment(b"\x00" * 32, 4, 0).aux_tree_height == 2
        assert AuxPoWCommitment(b"\x00" * 32, 8, 0).aux_tree_height == 3


# -------------------------------------------- deterministic index tests


class TestDeterministicIndex:
    def test_single_chain_always_index_0(self):
        assert get_expected_index(0, 1, 0) == 0
        assert get_expected_index(42, 1, 0) == 0
        assert get_expected_index(0xFFFFFFFF, 1, 0) == 0

    def test_multi_chain_gives_valid_range(self):
        for nonce in (0, 1, 42, 0xFFFFFFFF):
            index = get_expected_index(nonce, 1, 3)  # height 3 -> 8 slots
            assert 0 <= index < 8

    def test_different_chain_ids_give_different_indices(self):
        idx1 = get_expected_index(42, 1, 3)
        idx2 = get_expected_index(42, 2, 3)
        assert idx1 != idx2  # nearly always; almost impossible to collide

    def test_deterministic(self):
        a = get_expected_index(12345, 3, 4)
        b = get_expected_index(12345, 3, 4)
        assert a == b


# --------------------------------------------------- AuxPoW serialisation


class TestAuxPoWSerialisation:
    def test_round_trip_single_chain(self):
        coinbase = _make_coinbase_tx(100, 50 * 10**8, b"data")
        parent = _make_parent_header()

        auxpow = AuxPoW(
            coinbase_tx=coinbase,
            coinbase_merkle_branch=(),
            coinbase_index=0,
            aux_merkle_branch=(),
            aux_chain_index=0,
            parent_header=parent,
        )
        data = auxpow.serialize()
        auxpow2 = AuxPoW.deserialize(data)
        assert auxpow2.coinbase_tx == coinbase
        assert auxpow2.coinbase_merkle_branch == ()
        assert auxpow2.coinbase_index == 0
        assert auxpow2.aux_merkle_branch == ()
        assert auxpow2.aux_chain_index == 0
        assert auxpow2.parent_header == parent

    def test_round_trip_with_branches(self):
        coinbase = _make_coinbase_tx(100, 50 * 10**8, b"data")
        parent = _make_parent_header()
        siblings = tuple(bytes([i] * 32) for i in range(3))

        auxpow = AuxPoW(
            coinbase_tx=coinbase,
            coinbase_merkle_branch=siblings,
            coinbase_index=2,
            aux_merkle_branch=siblings,
            aux_chain_index=1,
            parent_header=parent,
        )
        data = auxpow.serialize()
        auxpow2 = AuxPoW.deserialize(data)
        assert auxpow2 == auxpow

    def test_coinbase_hash_property(self):
        coinbase = _make_coinbase_tx(100, 50 * 10**8, b"data")
        auxpow = AuxPoW(coinbase, (), 0, (), 0, _make_parent_header())
        assert auxpow.coinbase_hash == coinbase.txid()

    def test_to_dict(self):
        auxpow = AuxPoW(
            _make_coinbase_tx(100, 50 * 10**8, b"data"),
            (),
            0,
            (),
            0,
            _make_parent_header(),
        )
        d = auxpow.to_dict()
        assert "parent_block" in d
        assert "coinbase_txid" in d
        assert "coinbase_index" in d
        assert "aux_chain_index" in d


# ------------------------------------------------------- validation tests


class TestValidateAuxPoW:
    def test_valid_single_chain_proof(self):
        """Build a complete valid AuxPoW chain and validate it."""
        key = PrivateKey.generate()
        chain_id = 3  # regtest

        sc_hash, sc_target = _sc_block_hash(key)

        # Build commitment
        commitment_data = build_auxpow_commitment(sc_hash, tree_size=1, nonce=0)

        # Build parent coinbase with the commitment
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)

        # Build parent header whose Merkle root includes the coinbase
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)

        # Find a nonce that satisfies ScarletCoin's target
        solved_parent = _solve_parent(parent_header, sc_target)

        # Assemble AuxPoW
        auxpow = AuxPoW(
            coinbase_tx=parent_cb,
            coinbase_merkle_branch=(),
            coinbase_index=0,
            aux_merkle_branch=(),
            aux_chain_index=0,
            parent_header=solved_parent,
        )

        # Validate
        validate_auxpow(auxpow, sc_hash, sc_target, chain_id=chain_id)

    def test_wrong_chain_id(self, key):
        sc_hash, sc_target = _sc_block_hash(key)
        # Use tree_size=16 (height=4, 16 slots) so the index depends on chain_id
        commitment_data = build_auxpow_commitment(sc_hash, tree_size=16, nonce=42)
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)
        solved = _solve_parent(parent_header, sc_target)
        # Compute expected index for the correct chain_id (3)
        expected_idx = get_expected_index(42, 3, 4)
        # Build AuxPoW with that index
        auxpow = AuxPoW(parent_cb, (), 0, (), expected_idx, solved)

        # Validate with a different chain_id — the expected index will differ
        wrong_idx = get_expected_index(42, 999, 4)
        if wrong_idx == expected_idx:
            # Try different chain_id until we get a different index
            for cid in (1, 2, 4, 5, 6, 7):
                wrong_idx = get_expected_index(42, cid, 4)
                if wrong_idx != expected_idx:
                    break
        with pytest.raises(AuxPoWError, match="chain index"):
            validate_auxpow(auxpow, sc_hash, sc_target, chain_id=999)

    def test_missing_commitment(self):
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, b"no commitment here")
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)
        auxpow = AuxPoW(parent_cb, (), 0, (), 0, parent_header)

        with pytest.raises(AuxPoWError, match="does not contain"):
            validate_auxpow(auxpow, b"\x00" * 32, 2**256 - 1, chain_id=3)

    def test_parent_coinbase_not_coinbase(self):
        from scarletcoin.core.transaction import OutPoint

        non_cb = Transaction(
            version=1,
            inputs=(TxInput(OutPoint(b"\x01" * 32, 0)),),
            outputs=(TxOutput.p2pkh(1000, b"\x01" * 20),),
        )
        assert not non_cb.is_coinbase

        auxpow = AuxPoW(non_cb, (), 0, (), 0, _make_parent_header())
        with pytest.raises(AuxPoWError, match="not a coinbase"):
            validate_auxpow(auxpow, b"\x00" * 32, 2**256 - 1, chain_id=3)

    def test_wrong_aux_root(self, key):
        sc_hash, sc_target = _sc_block_hash(key)
        # Commit to a DIFFERENT hash
        wrong_root = b"\xff" * 32
        commitment_data = build_auxpow_commitment(wrong_root, 1, 0)
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)
        solved = _solve_parent(parent_header, sc_target)
        auxpow = AuxPoW(parent_cb, (), 0, (), 0, solved)

        with pytest.raises(AuxPoWError, match="does not match"):
            validate_auxpow(auxpow, sc_hash, sc_target, chain_id=3)

    def test_wrong_coinbase_merkle_branch(self, key):
        sc_hash, _sc_target = _sc_block_hash(key)
        commitment_data = build_auxpow_commitment(sc_hash, 1, 0)
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)

        # Build a Merkle tree with TWO transactions so the coinbase needs a branch
        from scarletcoin.core.transaction import OutPoint

        tx2 = Transaction(
            version=1,
            inputs=(TxInput(OutPoint(b"\x02" * 32, 0)),),
            outputs=(TxOutput.p2pkh(1000, b"\x03" * 20),),
        )
        real_root = merkle_root([parent_cb.txid(), tx2.txid()])
        parent_header = _make_parent_header(merkle_root=real_root)

        # Give empty branch (wrong — should include tx2.txid() as sibling)
        auxpow = AuxPoW(parent_cb, (), 0, (), 0, parent_header)

        with pytest.raises(AuxPoWError, match="does not match"):
            validate_auxpow(auxpow, sc_hash, 2**256 - 1, chain_id=3)

    def test_parent_pow_above_target(self, key):
        sc_hash, _sc_target = _sc_block_hash(key)
        commitment_data = build_auxpow_commitment(sc_hash, 1, 0)
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)

        # Use a target that is impossibly low (0)
        auxpow = AuxPoW(parent_cb, (), 0, (), 0, parent_header)
        with pytest.raises(AuxPoWError, match="does not satisfy"):
            validate_auxpow(auxpow, sc_hash, 0, chain_id=3)

    def test_chain_id_zero_raises(self):
        auxpow = AuxPoW(_make_coinbase_tx(1, 50 * 10**8, b""), (), 0, (), 0, _make_parent_header())
        with pytest.raises(AuxPoWError, match="not configured"):
            validate_auxpow(auxpow, b"\x00" * 32, 2**256 - 1, chain_id=0)

    def test_non_power_of_two_tree(self, key):
        sc_hash, sc_target = _sc_block_hash(key)
        # tree_size=3 (not power of two)
        commitment_data = build_auxpow_commitment(sc_hash, 3, 0)
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)
        solved = _solve_parent(parent_header, sc_target)
        auxpow = AuxPoW(parent_cb, (), 0, (), 0, solved)

        with pytest.raises(AuxPoWError, match="power of two"):
            validate_auxpow(auxpow, sc_hash, sc_target, chain_id=3)


# ----------------------------------------- Block-level AuxPoW integration


class TestAuxPoWBlock:
    def test_block_serialization_without_auxpow(self, key):
        """A block without AuxPoW still serializes and deserializes correctly."""
        block = mine_block(make_chain(), key)
        data = block.serialize()
        block2 = Block.deserialize(data)
        assert block2.header == block.header
        assert block2.has_auxpow is False
        assert block2.auxpow is None

    def test_block_serialization_with_auxpow(self, key):
        """Block with AuxPoW payload round-trips through serialization."""
        from scarletcoin.core.auxpow import AuxPoW

        chain = make_chain()
        block = mine_block(chain, key)

        # Attach a minimal AuxPoW
        auxpow = AuxPoW(
            _make_coinbase_tx(100, 50 * 10**8, b"test"),
            (),
            0,
            (),
            0,
            _make_parent_header(),
        )
        block_with_aux = block.with_auxpow(auxpow)

        data = block_with_aux.serialize()
        block2 = Block.deserialize(data)
        assert block2.has_auxpow is True
        assert block2.auxpow is not None
        assert block2.header == block.header
        chain.storage.close()

    def test_block_to_dict_shows_proof_type(self, key):
        chain = make_chain()
        block = mine_block(chain, key)
        d = block.to_dict(REGTEST.address_version)
        assert d["proof_type"] == "native"

        from scarletcoin.core.auxpow import AuxPoW

        auxpow = AuxPoW(
            _make_coinbase_tx(100, 50 * 10**8, b"test"),
            (),
            0,
            (),
            0,
            _make_parent_header(),
        )
        block_aux = block.with_auxpow(auxpow)
        d2 = block_aux.to_dict(REGTEST.address_version)
        assert d2["proof_type"] == "auxpow"
        assert "auxpow" in d2
        chain.storage.close()


# ----------------------------------------- Chain-level AuxPoW integration


class TestChainAuxPoW:
    def test_native_block_still_valid_after_activation(self, key):
        """After AuxPoW activation, native-PoW blocks are still accepted."""
        chain, pool = make_node_state()
        # regtest has auxpow_activation_height=0, so AuxPoW is active immediately
        block = mine_block(chain, key, pool)
        result = chain.add_block(block)
        assert result.status is BlockStatus.CONNECTED, result.reason
        assert chain.height == 1
        chain.storage.close()

    def test_auxpow_block_accepted_after_activation(self, key):
        """A valid AuxPoW block is accepted after activation."""
        chain, pool = make_node_state()

        # Build the ScarletCoin block components
        template = create_aux_block(chain, pool, pubkey_hash=key.public_key().hash160())
        sc_block = template.build_block()
        sc_hash = sc_block.hash()
        sc_target = bits_to_target(sc_block.header.bits)

        # Build commitment
        commitment_data = build_auxpow_commitment(sc_hash, tree_size=1, nonce=0)

        # Parent coinbase
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_cb_hash = parent_cb.txid()

        # Parent header
        parent_merkle_root = merkle_root([parent_cb_hash])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)
        solved_parent = _solve_parent(parent_header, sc_target)

        # Assemble AuxPoW
        from scarletcoin.core.auxpow import AuxPoW as AP

        auxpow = AP(parent_cb, (), 0, (), 0, solved_parent)
        block_with_aux = sc_block.with_auxpow(auxpow)

        result = chain.add_block(block_with_aux)
        assert result.status is BlockStatus.CONNECTED, f"rejected: {result.reason}"
        assert chain.height == 1
        chain.storage.close()

    def test_auxpow_block_with_bad_proof_rejected(self, key):
        """AuxPoW block with a wrong commitment root is rejected."""
        chain, pool = make_node_state()

        template = create_aux_block(chain, pool, pubkey_hash=key.public_key().hash160())
        sc_block = template.build_block()

        # Commit to a WRONG hash
        wrong_hash = b"\xff" * 32
        commitment_data = build_auxpow_commitment(wrong_hash, tree_size=1, nonce=0)
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)

        from scarletcoin.core.auxpow import AuxPoW as AP

        auxpow = AP(parent_cb, (), 0, (), 0, parent_header)

        result = chain.add_block(sc_block.with_auxpow(auxpow))
        assert result.status is BlockStatus.INVALID, result.reason
        assert "does not match" in result.reason or "AuxPoW" in result.reason
        chain.storage.close()

    def test_native_bad_pow_still_rejected(self, key):
        """Non-AuxPoW blocks with bad native PoW are still rejected after activation."""
        chain, _ = make_node_state()

        sc_cb = build_coinbase(
            height=1,
            reward=50 * 10**8,
            pubkey_hash=key.public_key().hash160(),
            extra=b"test",
        )
        block = Block.create(
            prev_hash=REGTEST.genesis_hash,
            transactions=[sc_cb],
            bits=0x01000001,  # very hard target, nonce=0 won't pass
            timestamp=1_700_000_001,
            nonce=0,
        )
        result = chain.add_block(block)
        # Without AuxPoW, native PoW must still pass
        assert result.status is BlockStatus.INVALID, result.reason
        assert "proof-of-work" in result.reason.lower() or "does not meet" in result.reason.lower()
        chain.storage.close()

    def test_auxpow_block_before_activation_rejected(self, key):
        """AuxPoW before activation height is rejected."""
        params = replace(REGTEST, auxpow_activation_height=10)
        chain = make_chain(params=params)

        # Mine one native block to get past genesis
        mine_and_add(chain, key, count=1)

        # Now try an AuxPoW block at height 2 (< 10)
        template = create_aux_block(chain, pubkey_hash=key.public_key().hash160())
        sc_block = template.build_block()
        sc_hash = sc_block.hash()
        sc_target = bits_to_target(sc_block.header.bits)

        commitment_data = build_auxpow_commitment(sc_hash, tree_size=1, nonce=0)
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)
        solved_parent = _solve_parent(parent_header, sc_target)

        from scarletcoin.core.auxpow import AuxPoW as AP

        auxpow = AP(parent_cb, (), 0, (), 0, solved_parent)
        block_with_aux = sc_block.with_auxpow(auxpow)

        result = chain.add_block(block_with_aux)
        assert result.status is BlockStatus.INVALID
        assert "not active" in result.reason
        chain.storage.close()


# ----------------------------------------------- AuxPoW candidate tests


class TestAuxBlockCandidate:
    def test_create_aux_block(self, key):
        chain, pool = make_node_state()
        candidate = create_aux_block(chain, pool, pubkey_hash=key.public_key().hash160())
        assert candidate.height == 1
        assert candidate.prev_hash == REGTEST.genesis_hash
        assert candidate.chain_id == REGTEST.auxpow_chain_id
        assert candidate.target == bits_to_target(candidate.bits)
        assert isinstance(candidate.commitment_nonce, int)
        chain.storage.close()

    def test_candidate_to_dict_and_back(self, key):
        chain, pool = make_node_state()
        candidate = create_aux_block(chain, pool, pubkey_hash=key.public_key().hash160())
        d = candidate.to_dict()
        assert "hash" in d
        assert "chainid" in d
        assert d["hash"] == candidate.aux_block_hash[::-1].hex()
        chain.storage.close()

    def test_candidate_build_block(self, key):
        chain, pool = make_node_state()
        candidate = create_aux_block(chain, pool, pubkey_hash=key.public_key().hash160())
        block = candidate.build_block()
        assert block.coinbase.is_coinbase
        assert block.header.prev_hash == candidate.prev_hash
        assert block.hash() == candidate.aux_block_hash
        chain.storage.close()

    def test_candidate_rejected_when_not_configured(self, key):
        params = replace(REGTEST, auxpow_chain_id=0)
        chain = make_chain(params=params)
        with pytest.raises(ValueError, match="not configured"):
            create_aux_block(chain, pubkey_hash=key.public_key().hash160())
        chain.storage.close()


# ------------------------------------------------------- RPC integration tests


class TestRPCAuxPoW:
    def test_createauxblock_rpc(self, rpc, key):
        """The createauxblock RPC returns the expected fields."""
        node, _server, client = rpc
        address = str(key.address(node.params.address_version))
        result = client.call("createauxblock", address)
        assert "hash" in result
        assert "chainid" in result
        assert result["chainid"] == node.params.auxpow_chain_id
        assert "target" in result
        assert "bits" in result
        assert "height" in result
        assert result["height"] == 1
        assert "coinbasevalue" in result
        assert "tree_size" in result
        assert "nonce" in result

    def test_submitauxblock_success(self, rpc, key):
        """Full round-trip: createauxblock -> build AuxPoW -> submitauxblock."""
        node, _server, client = rpc
        address = str(key.address(node.params.address_version))

        aux = client.call("createauxblock", address)
        aux_hash = aux["hash"]
        aux_target = int(aux["target"], 16)

        sc_hash = bytes.fromhex(aux_hash)[::-1]
        commitment_data = build_auxpow_commitment(sc_hash, tree_size=1, nonce=aux["nonce"])
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)
        solved_parent = _solve_parent(parent_header, aux_target)

        from scarletcoin.core.auxpow import AuxPoW as AP

        auxpow = AP(parent_cb, (), 0, (), 0, solved_parent)
        auxpow_hex = auxpow.serialize().hex()

        result = client.call("submitauxblock", aux_hash, auxpow_hex)
        assert result["status"] == "connected"
        assert result["hash"] == aux_hash

    def test_submitauxblock_bad_proof_rejected(self, rpc, key):
        """Submitting an AuxPoW with a wrong commitment is rejected."""
        node, _server, client = rpc
        address = str(key.address(node.params.address_version))

        aux = client.call("createauxblock", address)
        aux_hash = aux["hash"]

        # Commit to a WRONG hash (not the actual ScarletCoin block hash)
        wrong_hash = b"\xff" * 32
        commitment_data = build_auxpow_commitment(wrong_hash, tree_size=1, nonce=aux["nonce"])
        parent_cb = _make_coinbase_tx(800000, 50 * 10**8, commitment_data)
        parent_merkle_root = merkle_root([parent_cb.txid()])
        parent_header = _make_parent_header(merkle_root=parent_merkle_root)
        # Solve for the regtest target so we know the "wrong root" is the issue
        solved = _solve_parent(parent_header, int(aux["target"], 16))

        from scarletcoin.core.auxpow import AuxPoW as AP

        auxpow = AP(parent_cb, (), 0, (), 0, solved)
        auxpow_hex = auxpow.serialize().hex()

        with pytest.raises(Exception) as exc_info:
            client.call("submitauxblock", aux_hash, auxpow_hex)
        assert "rejected" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()

    def test_submitauxblock_stale_candidate_rejected(self, rpc, key):
        """A candidate from a different tip is rejected."""
        node, _server, client = rpc
        address = str(key.address(node.params.address_version))

        aux = client.call("createauxblock", address)

        # Mine a native block to advance the tip
        mine_and_add(node.chain, key, node.mempool, count=1)

        # Now try to submit against the stale candidate
        from scarletcoin.core.auxpow import AuxPoW as AP

        auxpow = AP(_make_coinbase_tx(1, 50 * 10**8, b""), (), 0, (), 0, _make_parent_header())
        auxpow_hex = auxpow.serialize().hex()

        with pytest.raises(Exception) as exc_info:
            client.call("submitauxblock", aux["hash"], auxpow_hex)
        msg = str(exc_info.value).lower()
        assert "no auxpow candidate" in msg or "expired" in msg


# ------------------------------------------------------- property-based tests


def test_serialize_deserialize_round_trip_idempotent():
    """Round-trip through serialize -> deserialize preserves the AuxPoW."""
    from scarletcoin.core.auxpow import AuxPoW as AP

    coinbase = _make_coinbase_tx(100, 50 * 10**8, b"round-trip")
    parent = _make_parent_header()
    auxpow = AP(
        coinbase_tx=coinbase,
        coinbase_merkle_branch=(b"\x11" * 32, b"\x22" * 32),
        coinbase_index=3,
        aux_merkle_branch=(b"\x33" * 32,),
        aux_chain_index=1,
        parent_header=parent,
    )
    # First round-trip
    data = auxpow.serialize()
    auxpow2 = AP.deserialize(data)
    assert auxpow2 == auxpow
    # Second round-trip
    data2 = auxpow2.serialize()
    assert data2 == data


def test_merkle_branch_wrong_index_gives_wrong_root():
    """Using the wrong index produces a different Merkle root."""
    leaf = b"\xaa" * 32
    a, b = b"\x11" * 32, b"\x22" * 32
    branch = (a, b)
    root0 = check_merkle_branch(leaf, branch, 0)
    root1 = check_merkle_branch(leaf, branch, 1)
    # With the wrong index, the root should differ (almost always)
    assert root0 != root1
