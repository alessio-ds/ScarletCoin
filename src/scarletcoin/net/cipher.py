"""Peer-to-peer link encryption.

After the ``version`` handshake, two peers exchange ephemeral secp256k1 public
keys and derive a shared ChaCha20-Poly1305 key through ECDH and HKDF.  Every
message after that is authenticated and encrypted.  Peers that do not offer an
ephemeral key (protocol version 1) stay in the clear.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "NONCE_SIZE",
    "TAG_SIZE",
    "P2PCipher",
    "derive_shared_key",
    "generate_ephemeral_key",
]

NONCE_SIZE = 12
TAG_SIZE = 16
_KEY_SIZE = 32

_HKDF_INFO = b"scarletcoin-p2p/1"


def generate_ephemeral_key() -> ec.EllipticCurvePrivateKey:
    """Return a fresh ephemeral secp256k1 key pair."""
    return ec.generate_private_key(ec.SECP256K1())


def public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    """Return the 33-byte compressed public key of an ephemeral key."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return key.public_key().public_bytes(Encoding.X962, PublicFormat.CompressedPoint)


def derive_shared_key(
    private_key: ec.EllipticCurvePrivateKey, peer_public_key: bytes
) -> bytes:
    """Derive the 32-byte session key from our ephemeral key and the peer's."""
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), peer_public_key)
    shared = private_key.exchange(ec.ECDH(), peer)
    return HKDF(
        algorithm=hashes.SHA256(), length=_KEY_SIZE, salt=b"", info=_HKDF_INFO
    ).derive(shared)


class P2PCipher:
    """A pair of ChaCha20-Poly1305 ciphers, one per direction.

    Each direction has its own monotonically increasing nonce, so a replayed
    message is rejected outright.
    """

    __slots__ = ("_recv", "_recv_nonce", "_send", "_send_nonce")

    def __init__(self, key: bytes) -> None:
        self._send = ChaCha20Poly1305(key)
        self._recv = ChaCha20Poly1305(key)
        self._send_nonce = 0
        self._recv_nonce = 0

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt one outgoing message."""
        nonce = self._send_nonce.to_bytes(NONCE_SIZE, "big")
        self._send_nonce += 1
        return nonce + self._send.encrypt(nonce, plaintext, None)

    def decrypt(self, ciphertext: bytes) -> bytes | None:
        """Decrypt one incoming message; ``None`` if it is forged or replayed."""
        if len(ciphertext) < NONCE_SIZE + TAG_SIZE:
            return None
        nonce = ciphertext[:NONCE_SIZE]
        try:
            plaintext = self._recv.decrypt(nonce, ciphertext[NONCE_SIZE:], None)
        except Exception:  # pragma: no cover - InvalidTag
            return None
        self._recv_nonce += 1
        return plaintext
