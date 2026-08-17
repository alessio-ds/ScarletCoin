"""BIP-0032 hierarchical deterministic key derivation for secp256k1.

ScarletCoin uses ``hash256`` instead of ``hash160`` for key fingerprints, which
is the same deliberate deviation already made for addresses (see
:mod:`scarletcoin.crypto.hashing`).  Everything else follows BIP-0032 exactly,
so the test vectors agree on chain codes, keys and derived public keys; only the
fingerprint of a public key differs from Bitcoin's.

The serialisation format (xprv/xpub) reuses Bitcoin's version bytes: the
network is disambiguated by the BIP-0044 coin type in the derivation path, not
by the extended key prefix.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from ecdsa import SECP256k1
from ecdsa.ellipticcurve import INFINITY, PointJacobi

from scarletcoin.crypto.base58 import Base58Error, b58decode, b58encode
from scarletcoin.crypto.hashing import hash256

__all__ = [
    "BIP32_DERIVATION",
    "HARDENED",
    "Bip32Error",
    "ExtendedKey",
]

HARDENED: int = 1 << 31

_BIP32_SEED_KEY = b"Bitcoin seed"

_MAINNET_XPRV = bytes.fromhex("0488ADE4")
_MAINNET_XPUB = bytes.fromhex("0488B21E")
_TESTNET_XPRV = bytes.fromhex("04358394")
_TESTNET_XPUB = bytes.fromhex("043587CF")
_BIP32_PAYLOAD = 74
_BIP32_CHECKSUM = 4

_G = SECP256k1.generator
_N = SECP256k1.order

# Known good derivation paths through the test vectors.
BIP32_DERIVATION: tuple[int, ...] = (HARDENED,)


class Bip32Error(ValueError):
    """Raised when key derivation fails (out-of-range tweak, zero child key)."""


@dataclass(frozen=True, slots=True)
class ExtendedKey:
    """An extended private or public key with its chain code and derivation path."""

    key: bytes
    """32-byte secret or 33-byte compressed public key."""
    chain_code: bytes
    """32 bytes."""
    depth: int
    index: int
    parent_fingerprint: bytes
    """4 bytes, :func:`hash256` of the *parent's* public key."""
    version: bytes
    """4-byte version prefix (xprv / xpub / tprv / tpub)."""

    def __post_init__(self) -> None:
        if len(self.key) not in (32, 33):
            raise Bip32Error(f"key must be 32 or 33 bytes, got {len(self.key)}")
        if len(self.chain_code) != 32:
            raise Bip32Error(f"chain code must be 32 bytes, got {len(self.chain_code)}")
        if len(self.parent_fingerprint) != 4:
            raise Bip32Error(
                f"parent fingerprint must be 4 bytes, got {len(self.parent_fingerprint)}"
            )
        if len(self.version) != 4:
            raise Bip32Error(f"version must be 4 bytes, got {len(self.version)}")

    @property
    def is_private(self) -> bool:
        """``True`` if the key carries a 32-byte secret."""
        return len(self.key) == 32

    @property
    def is_mainnet(self) -> bool:
        """``True`` for mainnet version bytes."""
        return self.version in (_MAINNET_XPRV, _MAINNET_XPUB)

    def _fingerprint(self) -> bytes:
        return hash256(self.public_key_bytes())[:4]

    def public_key_bytes(self) -> bytes:
        """Return the 33-byte compressed public key."""
        if self.is_private:
            return (_G * int.from_bytes(self.key, "big")).to_bytes(encoding="compressed")
        return self.key

    def private_key_bytes(self) -> bytes:
        """Return the 32-byte secret.

        Raises:
            Bip32Error: if this is a public-only key.
        """
        if not self.is_private:
            raise Bip32Error("this is a public extended key and carries no private key")
        return self.key

    def public(self) -> ExtendedKey:
        """Return the public-only counterpart of this key."""
        version = _TESTNET_XPUB if self.version in (_TESTNET_XPRV, _TESTNET_XPUB) else _MAINNET_XPUB
        return ExtendedKey(
            key=self.public_key_bytes(),
            chain_code=self.chain_code,
            depth=self.depth,
            index=self.index,
            parent_fingerprint=self.parent_fingerprint,
            version=version,
        )

    def derive(self, index: int) -> ExtendedKey:
        """Return the child key at ``index``.

        Raises:
            Bip32Error: if ``index`` calls for normal derivation from a
                public-only key (which is possible), or when a derived key is
                out of range (vanishingly unlikely, but reset i and retry).
        """
        hardened = index >= HARDENED
        if hardened and not self.is_private:
            raise Bip32Error("cannot derive a hardened child from a public extended key")

        if self.is_private:
            return self._ckd_priv(index, hardened)
        return self._ckd_pub(index)

    def derive_path(self, path: str | tuple[int, ...]) -> ExtendedKey:
        """Derive the key at a BIP-0032 path like ``"m/44'/0'/0'/0/0"``."""
        if isinstance(path, str):
            parts = path.strip().split("/")
            if not parts or parts[0] not in ("m", "M"):
                raise Bip32Error(f"path must start with 'm' or 'M', got {path!r}")
            indices: tuple[int, ...] = tuple(
                int(p[:-1]) + HARDENED if p.endswith("'") or p.endswith("H") else int(p)
                for p in parts[1:]
                if p
            )
        else:
            indices = tuple(path)
        key = self
        for index in indices:
            key = key.derive(index)
        return key

    def _ckd_priv(self, index: int, hardened: bool) -> ExtendedKey:
        parent_priv = int.from_bytes(self.key, "big")
        data = _ckd_data(self.key if hardened else self.public_key_bytes(), index, hardened)
        il, ir = _hmac_split(self.chain_code, data)
        if il >= _N:
            raise Bip32Error(
                f"IL ({il:#x}) >= curve order; increment i and retry. This is extremely rare."
            )
        child_priv = (il + parent_priv) % _N
        if child_priv == 0:
            raise Bip32Error(
                "derived private key is zero; increment i and retry. This is extremely rare."
            )
        return ExtendedKey(
            key=child_priv.to_bytes(32, "big"),
            chain_code=ir,
            depth=self.depth + 1,
            index=index,
            parent_fingerprint=self._fingerprint(),
            version=self.version,
        )

    def _ckd_pub(self, index: int) -> ExtendedKey:
        if index >= HARDENED:
            raise Bip32Error("cannot derive a hardened child from a public extended key")
        data = _ckd_data(self.key, index, False)
        il, ir = _hmac_split(self.chain_code, data)
        if il >= _N:
            raise Bip32Error(
                f"IL ({il:#x}) >= curve order; increment i and retry. This is extremely rare."
            )
        parent_point = PointJacobi.from_bytes(SECP256k1.curve, self.key)
        child_point = _G * il + parent_point
        if child_point == INFINITY:
            raise Bip32Error("derived public key is the point at infinity; increment i and retry.")
        return ExtendedKey(
            key=child_point.to_bytes(encoding="compressed"),
            chain_code=ir,
            depth=self.depth + 1,
            index=index,
            parent_fingerprint=self._fingerprint(),
            version=_TESTNET_XPUB
            if self.version in (_TESTNET_XPRV, _TESTNET_XPUB)
            else _MAINNET_XPUB,
        )

    # ---------------------------------------------------------------- encoding

    def serialize(self) -> str:
        """Encode as a Base58Check string (xprv / xpub / tprv / tpub)."""
        payload = (
            bytes([self.depth])
            + self.parent_fingerprint
            + self.index.to_bytes(4, "big")
            + self.chain_code
            + (b"\x00" + self.key if self.is_private else self.key)
        )
        assert len(payload) == _BIP32_PAYLOAD
        body = self.version + payload
        return b58encode(body + hash256(body)[:_BIP32_CHECKSUM])

    @classmethod
    def deserialize(cls, text: str) -> ExtendedKey:
        """Parse an xprv / xpub / tprv / tpub string.

        Raises:
            Bip32Error: if the string is not a valid extended key.
        """
        try:
            raw = b58decode(text)
        except Base58Error as exc:
            raise Bip32Error(str(exc)) from exc
        if len(raw) < 4 + _BIP32_PAYLOAD + _BIP32_CHECKSUM:
            raise Bip32Error("extended key string is too short")
        body, checksum = raw[:-_BIP32_CHECKSUM], raw[-_BIP32_CHECKSUM:]
        if hash256(body)[:_BIP32_CHECKSUM] != checksum:
            raise Bip32Error("extended key checksum does not match")
        version = body[:4]
        if version not in (_MAINNET_XPRV, _MAINNET_XPUB, _TESTNET_XPRV, _TESTNET_XPUB):
            raise Bip32Error(f"unknown extended key version {version.hex()}")
        payload = body[4:]
        if len(payload) != _BIP32_PAYLOAD:
            raise Bip32Error(
                f"extended key payload is {len(payload)} bytes, expected {_BIP32_PAYLOAD}"
            )
        depth = payload[0]
        parent_fingerprint = payload[1:5]
        index = int.from_bytes(payload[5:9], "big")
        chain_code = payload[9:41]
        key = payload[41:]
        is_private = version in (_MAINNET_XPRV, _TESTNET_XPRV)
        if is_private:
            if key[0] != 0x00:
                raise Bip32Error("private extended key must start with a zero byte")
            key = key[1:]
        if len(key) != 32 + (0 if is_private else 1):
            raise Bip32Error(f"invalid key length in extended key: {len(key)}")
        if not is_private and key[0] not in (0x02, 0x03):
            raise Bip32Error(
                f"public extended key must carry a compressed public key, got prefix {key[0]:#04x}"
            )
        return cls(
            key=key,
            chain_code=chain_code,
            depth=depth,
            index=index,
            parent_fingerprint=parent_fingerprint,
            version=version,
        )


def _hmac_split(chain_code: bytes, data: bytes) -> tuple[int, bytes]:
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    return int.from_bytes(digest[:32], "big"), digest[32:]


def _ckd_data(parent_key: bytes, index: int, hardened: bool) -> bytes:
    if hardened:
        return b"\x00" + parent_key + index.to_bytes(4, "big")
    return parent_key + index.to_bytes(4, "big")


def seed_to_master(seed: bytes, *, mainnet: bool = True) -> ExtendedKey:
    """Derive the BIP-0032 master extended key from a 64-byte seed."""
    digest = hmac.new(_BIP32_SEED_KEY, seed, hashlib.sha512).digest()
    version = _MAINNET_XPRV if mainnet else _TESTNET_XPRV
    return ExtendedKey(
        key=digest[:32],
        chain_code=digest[32:],
        depth=0,
        index=0,
        parent_fingerprint=b"\x00\x00\x00\x00",
        version=version,
    )


def derive_from_path(seed: bytes, path: str, *, mainnet: bool = True) -> ExtendedKey:
    """Derive the key at ``path`` (e.g. ``"m/44'/0'/0'/0/0"``) from a seed."""
    return seed_to_master(seed, mainnet=mainnet).derive_path(path)
