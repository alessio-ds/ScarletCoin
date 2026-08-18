"""The wallet: balances, history and spending, on top of a node's RPC interface.

The wallet never validates the chain itself; it trusts the node it is configured
to talk to.  It does hold the private keys, and signing always happens locally —
keys are never sent anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from scarletcoin.core.transaction import OutPoint, Transaction
from scarletcoin.core.utxo import Coin
from scarletcoin.crypto.keys import Address, InvalidKeyError
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.wallet.builder import (
    InsufficientFundsError,
    build_sweep_transaction,
    build_transaction,
)
from scarletcoin.wallet.keystore import Keystore, WalletError

__all__ = ["Balance", "SendResult", "Wallet"]


@dataclass(frozen=True, slots=True)
class Balance:
    """What a wallet holds."""

    confirmed: int
    spendable: int
    immature: int
    utxo_count: int

    @property
    def total(self) -> int:
        """Everything the wallet owns, mature or not."""
        return self.confirmed


@dataclass(frozen=True, slots=True)
class SendResult:
    """The outcome of a successful payment."""

    txid: str
    fee: int
    change: int
    transaction: Transaction

    @property
    def size(self) -> int:
        """Size of the broadcast transaction in bytes."""
        return self.transaction.size()


class Wallet:
    """A key store plus a node connection."""

    def __init__(self, keystore: Keystore, client: RpcClient) -> None:
        self.keystore = keystore
        self.client = client
        self.params = keystore.params

    # ------------------------------------------------------------------- queries

    def height(self) -> int:
        """Return the height the node is at."""
        return int(self.client.getblockcount())

    def coins(self, *, spendable_only: bool = True) -> list[tuple[OutPoint, Coin]]:
        """Return the wallet's unspent outputs.

        Raises:
            RpcClientError: if the node cannot be reached.
        """
        result: list[tuple[OutPoint, Coin]] = []
        for address in self.keystore.address_strings():
            pubkey_hash = Address.decode(address).hash
            for item in self.client.getutxos(address)["utxos"]:
                if spendable_only and not item["spendable"]:
                    continue
                result.append(
                    (
                        OutPoint(bytes.fromhex(item["txid"])[::-1], int(item["index"])),
                        Coin(
                            value=int(item["value"]),
                            output_type=0,
                            payload=pubkey_hash,
                            height=int(item["height"]),
                            is_coinbase=bool(item["coinbase"]),
                        ),
                    )
                )
        return result

    def balance(self) -> Balance:
        """Return the wallet's total balance across every address."""
        confirmed = spendable = immature = count = 0
        for data in self.client.getbalances(self.keystore.address_strings()).values():
            confirmed += int(data["balance"])
            spendable += int(data["spendable"])
            immature += int(data["immature"])
            count += int(data["utxo_count"])
        return Balance(confirmed, spendable, immature, count)

    def balances_by_address(self) -> list[tuple[str, str, int]]:
        """Return ``(address, label, balance)`` for every address in the wallet."""
        rows = []
        data = self.client.getbalances(self.keystore.address_strings())
        for record in self.keystore.addresses():
            balance = int(data.get(record.address, {}).get("balance", 0))
            rows.append((record.address, record.label, balance))
        return rows

    def history(self, limit: int = 50) -> list[dict]:
        """Return the wallet's transaction history, newest first."""
        entries: dict[str, dict] = {}
        for address in self.keystore.address_strings():
            for item in self.client.getaddresshistory(address, limit)["transactions"]:
                existing = entries.get(item["txid"])
                if existing is None:
                    entries[item["txid"]] = dict(item, address=address)
                else:
                    existing["received"] += item["received"]
                    existing["sent"] += item["sent"]
                    existing["net"] = existing["received"] - existing["sent"]
        ordered = sorted(entries.values(), key=lambda item: (item["height"], item["txid"]))
        return list(reversed(ordered))[:limit]

    # -------------------------------------------------------------------- keys

    def new_address(self, label: str = "") -> str:
        """Create a new address and save the wallet."""
        address = self.keystore.new_key(label)
        self.keystore.save()
        return str(address)

    # ------------------------------------------------------------------ spending

    def default_fee_rate(self) -> int:
        """Return the fee rate used when the caller does not choose one."""
        return self.params.min_relay_fee_per_kb

    def _parse_destination(self, destination: str) -> Address:
        try:
            return Address.decode(destination, expected_version=self.params.address_version)
        except InvalidKeyError as exc:
            raise WalletError(str(exc)) from exc

    def send(
        self,
        destination: str,
        amount: int,
        *,
        fee_per_kb: int | None = None,
        broadcast: bool = True,
    ) -> SendResult:
        """Pay ``amount`` scar to ``destination``.

        Raises:
            WalletError: if the address is invalid or the wallet is locked.
            InsufficientFundsError: if the wallet cannot cover the payment.
            RpcClientError: if the node rejects or cannot receive the transaction.
        """
        target = self._parse_destination(destination)
        keys = self.keystore.keys_by_hash()
        built = build_transaction(
            spendable_coins=self.coins(),
            keys=keys,
            outputs=[(target, amount)],
            change_hash=Address.decode(self.keystore.default_address()).hash,
            fee_per_kb=fee_per_kb or self.default_fee_rate(),
            params=self.params,
        )
        txid = built.transaction.txid_hex()
        if broadcast:
            txid = self.client.sendrawtransaction(built.transaction.serialize().hex())
        return SendResult(txid, built.fee, built.change, built.transaction)

    def send_everything(
        self, destination: str, *, fee_per_kb: int | None = None, broadcast: bool = True
    ) -> SendResult:
        """Send the wallet's entire spendable balance to ``destination``.

        Raises:
            InsufficientFundsError: if there is nothing to send, or the balance
                would not even cover the fee.
        """
        target = self._parse_destination(destination)
        coins = self.coins()
        if not coins:
            raise InsufficientFundsError("this wallet has no spendable coins")
        built = build_sweep_transaction(
            spendable_coins=coins,
            keys=self.keystore.keys_by_hash(),
            destination=target,
            fee_per_kb=fee_per_kb or self.default_fee_rate(),
            params=self.params,
        )
        txid = built.transaction.txid_hex()
        if broadcast:
            txid = self.client.sendrawtransaction(built.transaction.serialize().hex())
        return SendResult(txid, built.fee, built.change, built.transaction)

    # -------------------------------------------------------------------- status

    def node_info(self) -> dict:
        """Return the node's status, or an explanation of why it is unavailable."""
        try:
            return self.client.getinfo()
        except RpcClientError as exc:
            return {"error": str(exc)}
