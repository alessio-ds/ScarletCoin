"""The wallet file: a set of private keys, optionally encrypted.

The file is JSON so it can be inspected and backed up with ordinary tools::

    {
      "version": 1,
      "network": "mainnet",
      "encrypted": true,
      "addresses": [{"address": "S...", "label": "main", "created": 1700000000}],
      "crypto": { ...AES-256-GCM envelope holding the private keys... }
    }

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
from scarletcoin.crypto.encryption import DecryptionError, decrypt_blob, encrypt_blob
from scarletcoin.crypto.keys import Address, InvalidKeyError, PrivateKey

__all__ = ["KeyRecord", "Keystore", "WalletError", "WalletLocked"]

WALLET_VERSION = 1


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


class Keystore:
    """A wallet file and the keys inside it."""

    def __init__(
        self,
        path: Path,
        params: ChainParams,
        *,
        keys: list[KeyRecord] | None = None,
        addresses: list[AddressRecord] | None = None,
        envelope: dict | None = None,
        password: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.params = params
        self._keys = list(keys or [])
        self._addresses = list(addresses or [])
        self._envelope = envelope
        self._password = password

    # ------------------------------------------------------------------ creation

    @classmethod
    def create(cls, path: Path | str, network: str, *, password: str | None = None) -> Keystore:
        """Create a new wallet file with one fresh key.

        Raises:
            WalletError: if the file already exists.
        """
        path = Path(path)
        if path.exists():
            raise WalletError(f"{path} already exists; refusing to overwrite a wallet")
        keystore = cls(path, get_params(network), password=password)
        keystore.new_key("default")
        keystore.save()
        return keystore

    @classmethod
    def load(cls, path: Path | str, *, password: str | None = None) -> Keystore:
        """Open an existing wallet file.

        Args:
            path: The wallet file.
            password: Needed only for encrypted wallets; without it the wallet is
                loaded locked (addresses only).

        Raises:
            WalletError: if the file is missing or malformed.
            WalletLocked: never here — a wrong password raises :class:`WalletError`.
        """
        path = Path(path)
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError as exc:
            raise WalletError(f"no wallet at {path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise WalletError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("version") != WALLET_VERSION:
            raise WalletError(f"{path} is not a version {WALLET_VERSION} ScarletCoin wallet")
        try:
            params = get_params(str(raw["network"]))
        except KeyError as exc:
            raise WalletError(f"{path} does not say which network it belongs to") from exc

        addresses = [
            AddressRecord(
                str(item["address"]), str(item.get("label", "")), int(item.get("created", 0))
            )
            for item in raw.get("addresses", [])
        ]
        keystore = cls(path, params, addresses=addresses)

        if raw.get("encrypted"):
            keystore._envelope = raw.get("crypto")
            if not isinstance(keystore._envelope, dict):
                raise WalletError(f"{path} is marked encrypted but has no encrypted data")
            if password is not None:
                keystore.unlock(password)
        else:
            keystore._keys = keystore._decode_keys(raw.get("keys", []))
        return keystore

    # ------------------------------------------------------------------- locking

    @property
    def encrypted(self) -> bool:
        """``True`` if the wallet file is password protected."""
        return self._envelope is not None

    @property
    def locked(self) -> bool:
        """``True`` if the private keys are not currently available."""
        return self.encrypted and self._password is None

    def unlock(self, password: str) -> None:
        """Decrypt the private keys.

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
        self._keys = self._decode_keys(payload)
        self._password = password

    def lock(self) -> None:
        """Forget the password and the decrypted keys."""
        if self.encrypted:
            self._keys = []
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
            records.append(KeyRecord(key, str(item.get("label", "")), int(item.get("created", 0))))
        return records

    # -------------------------------------------------------------------- storage

    def save(self) -> None:
        """Write the wallet to disk atomically.

        Raises:
            WalletLocked: if the keys are not available to write.
        """
        self._require_keys()
        document: dict = {
            "version": WALLET_VERSION,
            "network": self.params.name,
            "encrypted": bool(self._password),
            "addresses": [
                {
                    "address": str(record.address(self.params)),
                    "label": record.label,
                    "created": record.created,
                }
                for record in self._keys
            ],
        }
        payload = [
            {
                "wif": record.private_key.to_wif(self.params.wif_version),
                "label": record.label,
                "created": record.created,
            }
            for record in self._keys
        ]
        if self._password:
            document["crypto"] = encrypt_blob(
                self._password,
                json.dumps(payload).encode("utf-8"),
                associated_data=self._associated_data(),
            )
        else:
            document["keys"] = payload

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, indent=1), "utf-8")
        with contextlib.suppress(OSError):  # some filesystems have no permission bits
            temporary.chmod(0o600)
        os.replace(temporary, self.path)
        if self._password:
            self._envelope = document["crypto"]
        self._addresses = [
            AddressRecord(str(item["address"]), str(item["label"]), int(item["created"]))
            for item in document["addresses"]
        ]

    # ----------------------------------------------------------------------- keys

    def new_key(self, label: str = "") -> Address:
        """Generate a new key and return its address."""
        self._require_keys()
        record = KeyRecord(PrivateKey.generate(), label, int(time.time()))
        self._keys.append(record)
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
        if any(record.private_key == key for record in self._keys):
            raise WalletError("that key is already in this wallet")
        record = KeyRecord(key, label, int(time.time()))
        self._keys.append(record)
        return record.address(self.params)

    def export_wif(self, address: str) -> str:
        """Return the private key of ``address`` in wallet-import format.

        Raises:
            WalletError: if the address is not in this wallet.
            WalletLocked: if the wallet is locked.
        """
        self._require_keys()
        for record in self._keys:
            if str(record.address(self.params)) == address:
                return record.private_key.to_wif(self.params.wif_version)
        raise WalletError(f"{address} is not in this wallet")

    @property
    def keys(self) -> list[KeyRecord]:
        """The wallet's keys.

        Raises:
            WalletLocked: if the wallet is locked.
        """
        self._require_keys()
        return list(self._keys)

    def keys_by_hash(self) -> dict[bytes, PrivateKey]:
        """Return every private key indexed by its public-key hash."""
        return {record.pubkey_hash(): record.private_key for record in self.keys}

    def addresses(self) -> list[AddressRecord]:
        """Return the wallet's addresses; available even when locked."""
        if self.locked:
            return list(self._addresses)
        return [
            AddressRecord(str(record.address(self.params)), record.label, record.created)
            for record in self._keys
        ]

    def address_strings(self) -> list[str]:
        """Return the wallet's addresses as strings."""
        return [record.address for record in self.addresses()]

    def default_address(self) -> str:
        """Return the address new payments should go to.

        Raises:
            WalletError: if the wallet has no keys.
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
        for index, record in enumerate(self._keys):
            if str(record.address(self.params)) == address:
                self._keys[index] = KeyRecord(record.private_key, label, record.created)
                return
        raise WalletError(f"{address} is not in this wallet")
