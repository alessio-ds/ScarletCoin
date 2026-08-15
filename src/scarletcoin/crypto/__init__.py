"""Cryptographic primitives used by ScarletCoin."""

from scarletcoin.crypto.base58 import (
    Base58Error,
    b58check_decode,
    b58check_encode,
    b58decode,
    b58encode,
)
from scarletcoin.crypto.hashing import hash160, hash256, sha256
from scarletcoin.crypto.keys import (
    Address,
    InvalidKeyError,
    InvalidSignatureError,
    PrivateKey,
    PublicKey,
)

__all__ = [
    "Address",
    "Base58Error",
    "InvalidKeyError",
    "InvalidSignatureError",
    "PrivateKey",
    "PublicKey",
    "b58check_decode",
    "b58check_encode",
    "b58decode",
    "b58encode",
    "hash160",
    "hash256",
    "sha256",
]
