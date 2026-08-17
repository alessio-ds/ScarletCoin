"""The wallet file: a seed plus any imported private keys, optionally encrypted.

Version 2 stores a BIP-0039/BIP-0032 seed instead of a list of independent keys::

    {
      "version": 2,
      "network": "mainnet",
      "encrypted": true,
      "seed_fingerprint": "a1b2c3d4",
      "next_index": 3,
      "addresses": [{"address": "S...", "label": "main", "created": 0, "path": "m/44'/0'/0'/0/0"}],
      "crypto": { ...scrypt + AES-256-GCM envelope holding {"seed": "...", "imported": [...]}... }
    }

A wallet created by version 1 (a plain list of WIF keys) still loads; it is
written back in version 2 format with its old keys kept as imported keys.
Addresses stay in the clear even when the wallet is encrypted, so balances and
history can be shown while the wallet is locked; only spending needs the
password.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from scarletcoin.core.params import ChainParams, get_params
from scarletcoin.crypto.bip32 import seed_to_master
from scarletcoin.crypto.bip39 import MnemonicError, generate_mnemonic, mnemonic_to_seed
from scarletcoin.crypto.encryption import DecryptionError, decrypt_blob, encrypt_blob
from scarletcoin.crypto.keys import Address, InvalidKeyError, PrivateKey

__all__ = ["KeyRecord", "Keystore", "WalletError", "WalletLocked"]

WALLET_VERSION = 2


class WalletError(Exception):
    """Raised when a wallet file is unusable."""


class WalletLocked(WalletError):
    """Raised when an operation needs the password of an encrypted wallet."""


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """One key in the wallet."""

    private_key: PrivateKey
    label: str
    created: int
    path: str | None = None
    """Derivation path of a seed-derived key; ``None`` for imported keys."""

    def address(self, params: ChainParams) -> Address:
        """Return this key's address on ``params``' network."""
        return self.private_key.address(params.address_version)

    def pubkey_hash(self) -> bytes:
        """Return the public-key hash this key controls."""
        return self.private_key.public_key().hash160()


@dataclass(frozen=True, slots=True)
class AddressRecord:
    """The public half of a key, readable without the password."""

    address: str
    label: str
    created: int
    path: str | None = None


class Keystore:
    """A wallet file: a seed, derived addresses and imported keys."""

    def __init__(
        self,
        path: Path,
        params: ChainParams,
        *,
        seed: bytes | None = None,
        imported: list[KeyRecord] | None = None,
        addresses: list[AddressRecord] | None = None,
        next_index: int = 0,
        envelope: dict | None = None,
        password: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.params = params
        self._seed = seed
        self._imported = list(imported or [])
        self._addresses = list(addresses or [])
        self._next_index = next_index
        self._envelope = envelope
        self._password = password
        self.new_mnemonic: str | None = None
        """Set when this object created a fresh seed; never written to disk."""

    # ---------------------------------------------------------------- creation

    @staticmethod
    def _derive(params: ChainParams, seed: bytes, index: int) -> tuple[KeyRecord, str]:
        path = f"m/44'/{params.bip44_coin_type}'/0'/0/{index}"
        key = seed_to_master(seed, mainnet=params.name == "mainnet")
        secret = key.derive_path(path).private_key_bytes()
        return KeyRecord(PrivateKey(secret), "", int(time.time()), path), path

    @classmethod
    def create(
        cls,
        path: Path | str,
        network: str,
        *,
        password: str | None = None,
        mnemonic: str | None = None,
    ) -> Keystore:
        """Create a new wallet file with one fresh seed-derived address.

        Returns:
            The wallet; the BIP-0039 mnemonic it was built from is available as
            ``keystore.new_mnemonic`` and must be shown to the user exactly once.

        Raises:
            WalletError: if the file already exists, or ``mnemonic`` is invalid.
        """
        path = Path(path)
        if path.exists():
            raise WalletError(f"{path} already exists; refusing to overwrite a wallet")
        params = get_params(network)
        if mnemonic is None:
            mnemonic = generate_mnemonic()
        try:
            seed = mnemonic_to_seed(mnemonic)
        except MnemonicError as exc:
            raise WalletError(str(exc)) from exc
        keystore = cls(path, params, seed=seed, password=password)
        keystore.new_mnemonic = mnemonic
        record, derivation = keystore._derive(params, seed, 0)
        keystore._addresses.append(
            AddressRecord(str(record.address(params)), "main", record.created, derivation)
        )
        keystore._next_index = 1
        keystore.save()
        return keystore

    @classmethod
    def restore(
        cls,
        path: Path | str,
        network: str,
        mnemonic: str,
        *,
        password: str | None = None,
    ) -> Keystore:
        """Rebuild a wallet from its BIP-0039 mnemonic sentence."""
        return cls.create(path, network, password=password, mnemonic=mnemonic)

    @classmethod
    def load(cls, path: Path | str, *, password: str | None = None) -> Keystore:
        """Open an existing wallet file (version 1 or 2).

        Args:
            path: The wallet file.
            password: Needed only for encrypted wallets; without it the wallet is
                loaded locked (addresses only).

        Raises:
            WalletError: if the file is missing or malformed.
        """
        path = Path(path)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError as exc:
            raise WalletError(f"no wallet at {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise WalletError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") not in (1, WALLET_VERSION):
            raise WalletError(f"{path} is not a version 1 or {WALLET_VERSION} ScarletCoin wallet")
        try:
            params = get_params(str(raw["network"]))
        except KeyError as exc:
            raise WalletError(f"{path} does not say which network it belongs to") from exc

        addresses = [
            AddressRecord(
                str(item["address"]),
                str(item.get("label", "")),
                int(item.get("created", 0)),
                str(item["path"]) if item.get("path") is not None else None,
            )
            for item in raw.get("addresses", [])
        ]
        keystore = cls(path, params, addresses=addresses, next_index=int(raw.get("next_index", 0)))

        if raw.get("encrypted"):
            keystore._envelope = raw.get("crypto")
            if not isinstance(keystore._envelope, dict):
                raise WalletError(f"{path} is marked encrypted but has no encrypted data")
            if password is not None:
                keystore.unlock(password)
            return keystore

        payload = raw.get("keys")
        if payload is not None:
            keystore._imported = keystore._decode_keys(payload)
        else:
            keystore._seed = keystore._decode_secret_payload(raw)
            keystore._imported = keystore._decode_keys(raw.get("imported", []))
        return keystore

    # ----------------------------------------------------------------- locking

    @property
    def encrypted(self) -> bool:
        """``True`` if the wallet file is password protected."""
        return self._envelope is not None

    @property
    def locked(self) -> bool:
        """``True`` if the secret material is not currently available."""
        return self.encrypted and self._password is None

    def unlock(self, password: str) -> None:
        """Decrypt the seed and imported keys.

        Raises:
            WalletError: if the password is wrong or the data is corrupt.
        """
        if self._envelope is None:
            return
        try:
            plaintext = decrypt_blob(
                password, self._envelope, associated_data=self._associated_data()
            )
            payload = json.loads(plaintext.decode("utf-8"))
        except DecryptionError as exc:
            raise WalletError(str(exc)) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise WalletError(f"the wallet's encrypted data is corrupt: {exc}") from exc
        try:
            self._seed = self._decode_secret_payload(payload)
            self._imported = self._decode_keys(payload.get("imported", []))
        except WalletError as exc:
            raise WalletError(f"the wallet's encrypted data is corrupt: {exc}") from exc
        self._password = password

    def lock(self) -> None:
        """Forget the password, the seed and the imported keys."""
        if self.encrypted:
            self._seed = None
            self._imported = []
            self._password = None

    def set_password(self, password: str | None) -> None:
        """Encrypt the wallet with ``password``, or remove encryption with ``None``.

        Raises:
            WalletLocked: if the wallet is currently locked.
        """
        self._require_keys()
        self._password = password
        self._envelope = {} if password else None
        self.save()

    def _require_keys(self) -> None:
        if self.locked:
            raise WalletLocked("this wallet is encrypted; a password is required")

    def _associated_data(self) -> bytes:
        return f"scarletcoin-wallet-v{WALLET_VERSION}:{self.params.name}".encode()

    def _decode_keys(self, payload: object) -> list[KeyRecord]:
        if not isinstance(payload, list):
            raise WalletError("the wallet's key list is malformed")
        records: list[KeyRecord] = []
        for item in payload:
            try:
                key = PrivateKey.from_wif(
                    str(item["wif"]), expected_version=self.params.wif_version
                )
            except (KeyError, TypeError, InvalidKeyError) as exc:
                raise WalletError(f"the wallet contains an unusable key: {exc}") from exc
            records.append(
                KeyRecord(
                    key,
                    str(item.get("label", "")),
                    int(item.get("created", 0)),
                    str(item["path"]) if item.get("path") is not None else None,
                )
            )
        return records

    def _decode_secret_payload(self, payload: object) -> bytes | None:
        if payload is None or not isinstance(payload, dict):
            return None
        seed_hex = payload.get("seed")
        if seed_hex is None:
            return None
        try:
            seed = bytes.fromhex(str(seed_hex))
        except ValueError as exc:
            raise WalletError(f"the wallet's seed is malformed: {exc}") from exc
        if len(seed) != 64:
            raise WalletError(f"the wallet's seed must be 64 bytes, got {len(seed)}")
        return seed

    def _secret_payload(self) -> dict:
        payload: dict = {}
        if self._seed is not None:
            payload["seed"] = self._seed.hex()
        payload["imported"] = [
            {
                "wif": record.private_key.to_wif(self.params.wif_version),
                "label": record.label,
                "created": record.created,
                "path": record.path,
            }
            for record in self._imported
        ]
        return payload

    # ------------------------------------------------------------------ storage

    def save(self) -> None:
        """Write the wallet to disk atomically.

        Raises:
            WalletLocked: if the secret material is not available to write.
        """
        self._require_keys()
        document: dict = {
            "version": WALLET_VERSION,
            "network": self.params.name,
            "encrypted": bool(self._password),
            "next_index": self._next_index,
            "addresses": [
                {
                    "address": record.address,
                    "label": record.label,
                    "created": record.created,
                    "path": record.path,
                }
                for record in self.addresses()
            ],
        }
        payload = self._secret_payload()
        if self._password:
            document["crypto"] = encrypt_blob(
                self._password,
                json.dumps(payload).encode("utf-8"),
                associated_data=self._associated_data(),
            )
        else:
            document.update(payload)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, indent=1), "utf-8")
        with contextlib.suppress(OSError):  # some filesystems have no permission bits
            temporary.chmod(0o600)
        os.replace(temporary, self.path)
        if self._password:
            self._envelope = document["crypto"]

    # --------------------------------------------------------------------- keys

    def _derive_record(self, index: int) -> KeyRecord:
        if self._seed is None:
            raise WalletError("this wallet has no seed; import or restore a wallet first")
        return self._derive(self.params, self._seed, index)[0]

    def new_key(self, label: str = "") -> Address:
        """Generate a new address and return it.

        Seed-backed wallets derive the next address in the BIP-0044 sequence; a
        legacy wallet generates an independent random key.
        """
        self._require_keys()
        if self._seed is not None:
            record, derivation = self._derive(self.params, self._seed, self._next_index)
            self._next_index += 1
        else:
            derivation = None
            record = KeyRecord(PrivateKey.generate(), label, int(time.time()))
            self._imported.append(record)
        if label:
            record = KeyRecord(record.private_key, label, record.created, record.path)
        self._addresses.append(
            AddressRecord(
                str(record.address(self.params)), record.label, record.created, derivation
            )
        )
        return record.address(self.params)

    def import_wif(self, wif: str, label: str = "imported") -> Address:
        """Import a private key in wallet-import format.

        Raises:
            WalletError: if the key is malformed, from another network, or already
                present.
        """
        self._require_keys()
        try:
            key = PrivateKey.from_wif(wif, expected_version=self.params.wif_version)
        except InvalidKeyError as exc:
            raise WalletError(str(exc)) from exc
        if any(record.private_key == key for record in self.all_keys()):
            raise WalletError("that key is already in this wallet")
        record = KeyRecord(key, label, int(time.time()))
        self._imported.append(record)
        self._addresses.append(
            AddressRecord(str(record.address(self.params)), label, record.created)
        )
        return record.address(self.params)

    def export_wif(self, address: str) -> str:
        """Return the private key of ``address`` in wallet-import format.

        Raises:
            WalletError: if the address is not in this wallet.
            WalletLocked: if the wallet is locked.
        """
        self._require_keys()
        for record in self.all_keys():
            if str(record.address(self.params)) == address:
                return record.private_key.to_wif(self.params.wif_version)
        raise WalletError(f"{address} is not in this wallet")

    def all_keys(self) -> list[KeyRecord]:
        """Return every key: the seed-derived ones plus the imported ones."""
        derived = [
            self._derive_record(self._path_index(record))
            for record in self._addresses
            if record.path is not None
        ]
        return derived + list(self._imported)

    @staticmethod
    def _path_index(record: AddressRecord) -> int:
        try:
            return int(record.path.rsplit("/", 1)[1]) if record.path else 0
        except (ValueError, IndexError):
            return 0

    @property
    def keys(self) -> list[KeyRecord]:
        """The wallet's keys.

        Raises:
            WalletLocked: if the wallet is locked.
        """
        self._require_keys()
        return self.all_keys()

    def keys_by_hash(self) -> dict[bytes, PrivateKey]:
        """Return every private key indexed by its public-key hash."""
        return {record.pubkey_hash(): record.private_key for record in self.keys}

    def addresses(self) -> list[AddressRecord]:
        """Return the wallet's addresses; available even when locked."""
        return list(self._addresses)

    def address_strings(self) -> list[str]:
        """Return the wallet's addresses as strings."""
        return [record.address for record in self.addresses()]

    def default_address(self) -> str:
        """Return the address new payments should go to.

        Raises:
            WalletError: if the wallet has no addresses.
        """
        addresses = self.address_strings()
        if not addresses:
            raise WalletError("this wallet has no addresses")
        return addresses[0]

    def set_label(self, address: str, label: str) -> None:
        """Rename an address.

        Raises:
            WalletError: if the address is not in this wallet.
        """
        self._require_keys()
        for index, record in enumerate(self._addresses):
            if record.address == address:
                self._addresses[index] = AddressRecord(
                    record.address, label, record.created, record.path
                )
                return
        raise WalletError(f"{address} is not in this wallet")
