"""Tests for peer-to-peer link encryption."""

from __future__ import annotations

from scarletcoin.core.params import REGTEST
from scarletcoin.net import cipher, protocol


class TestCipher:
    def test_round_trip(self):
        a = cipher.generate_ephemeral_key()
        b = cipher.generate_ephemeral_key()
        key = cipher.derive_shared_key(a, cipher.public_bytes(b))
        same = cipher.derive_shared_key(b, cipher.public_bytes(a))
        assert key == same

        c = cipher.P2PCipher(key)
        encrypted = c.encrypt(b"hello, encrypted world")
        assert encrypted != b"hello, encrypted world"
        assert c.decrypt(encrypted) == b"hello, encrypted world"

    def test_tampering_is_detected(self):
        key = cipher.derive_shared_key(
            cipher.generate_ephemeral_key(), cipher.public_bytes(cipher.generate_ephemeral_key())
        )
        c = cipher.P2PCipher(key)
        encrypted = bytearray(c.encrypt(b"secret"))
        encrypted[-1] ^= 0x01
        assert c.decrypt(bytes(encrypted)) is None

    def test_nonces_advance_per_direction(self):
        key = b"\x01" * 32
        c = cipher.P2PCipher(key)
        first = c.encrypt(b"a")
        second = c.encrypt(b"b")
        assert first != second
        assert c.decrypt(first) == b"a"
        assert c.decrypt(second) == b"b"


class TestVersionEphemeralKey:
    def test_round_trip(self):
        key = cipher.generate_ephemeral_key()
        message = protocol.Version(
            version=2,
            user_agent="/test/",
            start_height=0,
            nonce=1,
            listen_port=20333,
            timestamp=1700000000,
            ephemeral_pubkey=cipher.public_bytes(key),
        )
        decoded = protocol.Version.decode(message.encode())
        assert decoded.ephemeral_pubkey == cipher.public_bytes(key)

    def test_version_1_payload_without_key_still_decodes(self):
        # A legacy payload that simply stops before the ephemeral key.
        from scarletcoin.core.serialize import Writer

        writer = Writer()
        writer.uint32(1)
        writer.varstr("/old/")
        writer.uint32(0)
        writer.uint64(1)
        writer.uint16(20333)
        writer.uint64(1700000000)
        decoded = protocol.Version.decode(writer.getvalue())
        assert decoded.version == 1
        assert decoded.ephemeral_pubkey == b""

    def test_framing_encrypts_only_the_payload(self):
        frame = protocol.encode_payload(REGTEST.magic, b"ping", b"data")
        assert frame[:4] == REGTEST.magic
        assert frame[4:16] == b"ping".ljust(12, b"\x00")
        length = int.from_bytes(frame[16:20], "little")
        assert length == 4
        assert frame[-4:] == b"data"
