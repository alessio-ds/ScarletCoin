"""Tests for the crypto layer: Base58, hashes, keys, signatures and encryption."""

from __future__ import annotations

import pytest

from scarletcoin.crypto.base58 import (
    Base58Error,
    b58check_decode,
    b58check_encode,
    b58decode,
    b58encode,
)
from scarletcoin.crypto.encryption import DecryptionError, decrypt_blob, encrypt_blob
from scarletcoin.crypto.hashing import hash160, hash256, sha256
from scarletcoin.crypto.keys import (
    CURVE_ORDER,
    Address,
    InvalidKeyError,
    InvalidSignatureError,
    PrivateKey,
    PublicKey,
)


class TestHashing:
    def test_sha256_known_vector(self):
        assert sha256(b"abc").hex() == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )

    def test_hash256_is_double_sha256(self):
        assert hash256(b"hello") == sha256(sha256(b"hello"))

    def test_hash160_is_20_bytes_of_hash256(self):
        assert hash160(b"hello") == hash256(b"hello")[:20]
        assert len(hash160(b"")) == 20


class TestBase58:
    @pytest.mark.parametrize(
        ("raw", "encoded"),
        [
            (b"", ""),
            (b"\x00", "1"),
            (b"\x00\x00", "11"),
            (b"hello world", "StV1DL6CwTryKyV"),
            (bytes(range(5)), "12VfUX"),
        ],
    )
    def test_round_trip(self, raw, encoded):
        assert b58encode(raw) == encoded
        assert b58decode(encoded) == raw

    def test_rejects_ambiguous_characters(self):
        with pytest.raises(Base58Error, match="invalid Base58 character"):
            b58decode("hello0world")

    def test_check_round_trip(self):
        text = b58check_encode(63, b"\x11" * 20)
        assert b58check_decode(text) == (63, b"\x11" * 20)

    def test_check_detects_a_typo(self):
        text = b58check_encode(63, b"\x11" * 20)
        broken = text[:-2] + ("2" if text[-2] != "2" else "3") + text[-1]
        with pytest.raises(Base58Error, match="bad checksum"):
            b58check_decode(broken)

    def test_check_rejects_the_wrong_network(self):
        text = b58check_encode(63, b"\x11" * 20)
        with pytest.raises(Base58Error, match="unexpected version"):
            b58check_decode(text, expected_version=127)

    def test_check_rejects_short_strings(self):
        with pytest.raises(Base58Error, match="too short"):
            b58check_decode("1111")


class TestKeys:
    def test_generate_produces_usable_keys(self):
        key = PrivateKey.generate()
        assert len(key.to_bytes()) == 32
        assert len(key.public_key().to_bytes()) == 33
        assert key.public_key().to_bytes()[0] in (2, 3)

    def test_wif_round_trip(self):
        key = PrivateKey.generate()
        wif = key.to_wif(191)
        assert PrivateKey.from_wif(wif, expected_version=191) == key

    def test_wif_from_another_network_is_refused(self):
        key = PrivateKey.generate()
        with pytest.raises(InvalidKeyError):
            PrivateKey.from_wif(key.to_wif(191), expected_version=239)

    @pytest.mark.parametrize("secret", [b"", b"\x00" * 32, (CURVE_ORDER).to_bytes(32, "big")])
    def test_invalid_secrets_are_refused(self, secret):
        with pytest.raises(InvalidKeyError):
            PrivateKey(secret)

    def test_repr_hides_the_secret(self):
        assert "redacted" in repr(PrivateKey.generate())

    def test_public_key_must_be_compressed(self):
        key = PrivateKey.generate()
        uncompressed = b"\x04" + key.public_key().to_bytes()[1:] + b"\x00" * 32
        with pytest.raises(InvalidKeyError):
            PublicKey(uncompressed)

    def test_public_key_must_be_on_the_curve(self):
        with pytest.raises(InvalidKeyError):
            PublicKey(b"\x02" + b"\xff" * 32)

    def test_sign_and_verify(self):
        key = PrivateKey.generate()
        digest = sha256(b"a message")
        signature = key.sign(digest)
        assert len(signature) == 64
        assert key.public_key().verify(digest, signature)

    def test_signature_does_not_verify_another_message(self):
        key = PrivateKey.generate()
        signature = key.sign(sha256(b"one"))
        assert not key.public_key().verify(sha256(b"two"), signature)

    def test_signature_does_not_verify_another_key(self):
        digest = sha256(b"a message")
        signature = PrivateKey.generate().sign(digest)
        assert not PrivateKey.generate().public_key().verify(digest, signature)

    def test_signatures_are_canonical_low_s(self):
        key = PrivateKey.generate()
        half = CURVE_ORDER // 2
        for index in range(20):
            signature = key.sign(sha256(f"message {index}".encode()))
            assert int.from_bytes(signature[32:], "big") <= half

    def test_signatures_are_deterministic_rfc6979(self):
        secret = bytes.fromhex("C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721")
        key = PrivateKey(secret)
        digest = sha256(b"sample")
        signature = key.sign(digest)
        assert signature == bytes.fromhex(
            "432310e32cb80eb6503a26ce83cc165c783b870845fb8aad6d970889fcd7a6c8"
            "530128b6b81c548874a6305d93ed071ca6e05074d85863d4056ce89b02bfab69"
        )
        assert key.sign(digest) == signature

    def test_high_s_signatures_are_rejected(self):
        key = PrivateKey.generate()
        digest = sha256(b"malleable")
        signature = key.sign(digest)
        r = signature[:32]
        s = int.from_bytes(signature[32:], "big")
        flipped = r + (CURVE_ORDER - s).to_bytes(32, "big")
        assert not key.public_key().verify(digest, flipped)

    def test_verify_rejects_zero_scalars(self):
        key = PrivateKey.generate()
        assert not key.public_key().verify(sha256(b"x"), b"\x00" * 64)

    def test_verify_rejects_wrong_length(self):
        key = PrivateKey.generate()
        with pytest.raises(InvalidSignatureError):
            key.public_key().verify(sha256(b"x"), b"\x01" * 63)

    def test_sign_requires_a_32_byte_digest(self):
        with pytest.raises(ValueError, match="32-byte digest"):
            PrivateKey.generate().sign(b"short")


class TestAddress:
    def test_round_trip(self):
        address = PrivateKey.generate().address(63)
        assert Address.decode(str(address)) == address
        assert str(address).startswith("S")

    def test_network_prefixes_differ(self):
        key = PrivateKey.generate()
        assert str(key.address(63))[0] == "S"
        assert str(key.address(127))[0] == "t"

    def test_decode_checks_the_network(self):
        address = str(PrivateKey.generate().address(63))
        with pytest.raises(InvalidKeyError):
            Address.decode(address, expected_version=127)

    def test_is_valid(self):
        address = str(PrivateKey.generate().address(63))
        assert Address.is_valid(address)
        assert not Address.is_valid("not-an-address")
        assert not Address.is_valid(address, expected_version=127)

    def test_hash_length_is_enforced(self):
        with pytest.raises(InvalidKeyError):
            Address(63, b"\x00" * 19)

    def test_whitespace_is_tolerated(self):
        address = PrivateKey.generate().address(63)
        assert Address.decode(f"  {address}\n") == address


class TestEncryption:
    def test_round_trip(self):
        envelope = encrypt_blob("hunter2", b"secret data")
        assert decrypt_blob("hunter2", envelope) == b"secret data"

    def test_wrong_password_is_detected(self):
        envelope = encrypt_blob("hunter2", b"secret data")
        with pytest.raises(DecryptionError, match="wrong password"):
            decrypt_blob("hunter3", envelope)

    def test_tampering_is_detected(self):
        envelope = encrypt_blob("hunter2", b"secret data")
        raw = bytearray(bytes.fromhex(envelope["ciphertext"]))
        raw[0] ^= 0x01
        envelope["ciphertext"] = bytes(raw).hex()
        with pytest.raises(DecryptionError):
            decrypt_blob("hunter2", envelope)

    def test_associated_data_is_bound(self):
        envelope = encrypt_blob("hunter2", b"data", associated_data=b"mainnet")
        with pytest.raises(DecryptionError):
            decrypt_blob("hunter2", envelope, associated_data=b"testnet")

    def test_each_envelope_uses_a_fresh_salt_and_nonce(self):
        first = encrypt_blob("hunter2", b"data")
        second = encrypt_blob("hunter2", b"data")
        assert first["kdf_params"]["salt"] != second["kdf_params"]["salt"]
        assert first["nonce"] != second["nonce"]
        assert first["ciphertext"] != second["ciphertext"]

    def test_empty_password_is_refused(self):
        with pytest.raises(ValueError, match="must not be empty"):
            encrypt_blob("", b"data")

    def test_unreasonable_parameters_are_refused(self):
        envelope = encrypt_blob("hunter2", b"data")
        envelope["kdf_params"]["n"] = 2**30
        with pytest.raises(DecryptionError, match="unreasonable"):
            decrypt_blob("hunter2", envelope)

    def test_unknown_algorithms_are_refused(self):
        envelope = encrypt_blob("hunter2", b"data")
        envelope["cipher"] = "rot13"
        with pytest.raises(DecryptionError, match="unsupported"):
            decrypt_blob("hunter2", envelope)
