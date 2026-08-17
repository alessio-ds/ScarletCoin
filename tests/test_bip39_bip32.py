"""Tests for BIP-0039 mnemonics and BIP-0032 hierarchical derivation."""

from __future__ import annotations

import pytest

from scarletcoin.crypto.bip32 import HARDENED, Bip32Error, ExtendedKey, seed_to_master
from scarletcoin.crypto.bip39 import (
    MnemonicError,
    entropy_to_mnemonic,
    generate_mnemonic,
    mnemonic_to_seed,
    validate_mnemonic,
)


class TestBip39:
    def test_known_vector_entropy(self):
        entropy = bytes.fromhex("00000000000000000000000000000000")
        expected = (
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon"
            " abandon abandon about"
        )
        assert entropy_to_mnemonic(entropy) == expected

    def test_known_vector_seed(self):
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon"
            " abandon abandon about"
        )
        seed = mnemonic_to_seed(mnemonic, "TREZOR")
        assert seed.hex() == (
            "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e5349553"
            "1f09a6987599d18264c1e1c92f2cf141630c7a3c4ab7c81b2f001698e7463b04"
        )

    def test_round_trip_all_sizes(self):
        for strength in (128, 160, 192, 224, 256):
            mnemonic = generate_mnemonic(strength)
            assert len(mnemonic.split()) == strength // 32 * 3
            validate_mnemonic(mnemonic)

    def test_checksum_catches_a_wrong_word(self):
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon abandon abandon abandon"
            " abandon abandon about"
        )
        broken = mnemonic.replace("about", "abandon")
        with pytest.raises(MnemonicError, match="checksum"):
            validate_mnemonic(broken)

    def test_unknown_word_is_refused(self):
        words = " ".join(["zzzzzz"] * 12)
        with pytest.raises(MnemonicError, match="unknown mnemonic word"):
            validate_mnemonic(words)

    def test_wrong_word_count_is_refused(self):
        with pytest.raises(MnemonicError, match="12, 15, 18, 21, 24"):
            validate_mnemonic("abandon abandon abandon")

    def test_bad_entropy_size_is_refused(self):
        with pytest.raises(ValueError, match="entropy must be"):
            entropy_to_mnemonic(b"\x00" * 15)

    def test_generated_mnemonics_are_valid(self):
        for _ in range(10):
            validate_mnemonic(generate_mnemonic())


class TestBip32:
    def test_master_from_test_vector_1(self):
        seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        master = seed_to_master(seed)
        assert master.private_key_bytes().hex() == (
            "e8f32e723decf4051aefac8e2c93c9c5b214313817cdb01a1494b917c8436b35"
        )
        assert master.chain_code.hex() == (
            "873dff81c02f525623fd1fe5167eac3a55a049de3d314bb42ee227ffed37d508"
        )
        assert master.public_key_bytes().hex() == (
            "0339a36013301597daef41fbe593a02cc513d0b55527ec2df1050e2e8ff49c85c2"
        )
        assert master.depth == 0 and master.index == 0
        assert master.parent_fingerprint == b"\x00" * 4

    def test_hardened_child_from_test_vector_1(self):
        seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        child = seed_to_master(seed).derive(HARDENED)
        assert child.private_key_bytes().hex() == (
            "edb2e14f9ee77d26dd93b4ecede8d16ed408ce149b6cd80b0715a2d911a0afea"
        )
        assert child.chain_code.hex() == (
            "47fdacbd0f1097043b78c63c20c34ef4ed9a111d980047ad16282c7ae6236141"
        )
        assert child.public_key_bytes().hex() == (
            "035a784662a4a20a65bf6aab9ae98a6c068a81c52e4b032c0fb5400c706cfccc56"
        )
        assert child.depth == 1 and child.index == HARDENED
        assert child.parent_fingerprint == master_fingerprint(seed)

    def test_public_derivation_matches_private(self):
        seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        master = seed_to_master(seed)
        for index in (0, 1, 7, 1000):
            from_priv = master.derive(index).public_key_bytes()
            from_pub = master.public().derive(index).public_key_bytes()
            assert from_priv == from_pub

    def test_hardened_derivation_needs_the_private_key(self):
        seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        with pytest.raises(Bip32Error, match="hardened"):
            seed_to_master(seed).public().derive(HARDENED)

    def test_xprv_round_trip(self):
        seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        master = seed_to_master(seed)
        text = master.serialize()
        assert text.startswith("xprv")
        decoded = ExtendedKey.deserialize(text)
        assert decoded.is_private
        assert decoded.private_key_bytes() == master.private_key_bytes()
        assert decoded.chain_code == master.chain_code

    def test_xpub_round_trip(self):
        seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        pub = seed_to_master(seed).public()
        text = pub.serialize()
        assert text.startswith("xpub")
        decoded = ExtendedKey.deserialize(text)
        assert not decoded.is_private
        assert decoded.public_key_bytes() == pub.public_key_bytes()

    def test_derive_path(self):
        seed = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        master = seed_to_master(seed)
        by_path = master.derive_path("m/44H/0H/0H/0/0")
        manual = master.derive(44 + HARDENED).derive(HARDENED).derive(HARDENED).derive(0).derive(0)
        assert by_path.private_key_bytes() == manual.private_key_bytes()

    def test_unknown_version_is_refused(self):
        from scarletcoin.crypto.base58 import b58encode
        from scarletcoin.crypto.hashing import hash256

        payload = b"\x00" + b"\x00" * 4 + b"\x00" * 4 + b"\x00" * 32 + b"\x00" * 33
        body = b"\xde\xad\xbe\xef" + payload
        forged = b58encode(body + hash256(body)[:4])
        with pytest.raises(Bip32Error, match="unknown extended key version"):
            ExtendedKey.deserialize(forged)


def master_fingerprint(seed: bytes) -> bytes:
    from scarletcoin.crypto.hashing import hash256

    master = seed_to_master(seed)
    return hash256(master.public_key_bytes())[:4]
