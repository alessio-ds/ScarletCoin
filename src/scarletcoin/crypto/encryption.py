"""Password-based encryption for wallet files.

A wallet's secret material is sealed with AES-256-GCM using a key derived from
the user's password with scrypt.  The resulting envelope is JSON-friendly (all
binary fields are hex) so it can be embedded directly in the wallet file.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["DecryptionError", "decrypt_blob", "encrypt_blob"]

_KDF = "scrypt"
_CIPHER = "aes-256-gcm"
_KEY_LENGTH = 32
_SALT_LENGTH = 16
_NONCE_LENGTH = 12
# ~64 MiB of memory per attempt: slow enough to frustrate brute force, fast
# enough (<1 s) to unlock a wallet interactively.
_SCRYPT_N = 2**16
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAX_MEMORY = 256 * 1024 * 1024


class DecryptionError(ValueError):
    """Raised when a wallet cannot be decrypted (wrong password or tampering)."""


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_KEY_LENGTH,
        maxmem=_MAX_MEMORY,
    )


def encrypt_blob(
    password: str, plaintext: bytes, *, associated_data: bytes = b""
) -> dict[str, Any]:
    """Encrypt ``plaintext`` with ``password`` and return a JSON-serialisable envelope."""
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(_SALT_LENGTH)
    nonce = os.urandom(_NONCE_LENGTH)
    key = _derive_key(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, associated_data)
    return {
        "kdf": _KDF,
        "kdf_params": {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P, "salt": salt.hex()},
        "cipher": _CIPHER,
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
    }


def decrypt_blob(password: str, envelope: dict[str, Any], *, associated_data: bytes = b"") -> bytes:
    """Decrypt an envelope produced by :func:`encrypt_blob`.

    Raises:
        DecryptionError: if the envelope is malformed, uses an unknown algorithm,
            or the password is wrong.
    """
    if envelope.get("kdf") != _KDF or envelope.get("cipher") != _CIPHER:
        raise DecryptionError("unsupported wallet encryption parameters")
    try:
        params = envelope["kdf_params"]
        salt = bytes.fromhex(params["salt"])
        n, r, p = int(params["n"]), int(params["r"]), int(params["p"])
        nonce = bytes.fromhex(envelope["nonce"])
        ciphertext = bytes.fromhex(envelope["ciphertext"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DecryptionError(f"malformed encrypted wallet: {exc}") from exc
    if n <= 1 or n & (n - 1) or r < 1 or p < 1 or n * r * 128 * p > _MAX_MEMORY:
        raise DecryptionError("refusing unreasonable scrypt parameters")
    key = _derive_key(password, salt, n, r, p)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data)
    except InvalidTag as exc:
        raise DecryptionError("wrong password, or the wallet file was modified") from exc
