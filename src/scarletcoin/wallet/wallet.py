"""The wallet: balances, history and spending, on top of a node's RPC interface.

The wallet never validates the chain itself; it trusts the node it is configured
to talk to.  It does hold the private keys, and signing always happens locally —
keys are never sent anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from scarletcoin.core.transaction import Transaction
from scarletcoin.core.utxo import Coin
from scarletcoin.crypto.hash_to_point import hash_to_point
from scarletcoin.crypto.schnorr import point_from_bytes, schnorr_point_to_bytes
from scarletcoin.crypto.stealth import (
    StealthAddress,
    StealthError,
    recognize_output,
    spend_key_for_output,
)
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.wallet.builder import (
    DEFAULT_RING_SIZE,
    InsufficientFundsError,
    build_anonymous_transaction,
    estimate_size_v2,
    fee_for_size,
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
        self._scan_cache: tuple[int, list] | None = None

    # ------------------------------------------------------------------- queries

    def height(self) -> int:
        """Return the height the node is at."""
        return int(self.client.getblockcount())

    def _spent_key_images(self) -> set[bytes]:
        spent: set[bytes] = set()
        for item in self.client.getkeyimages():
            if isinstance(item, str):
                spent.add(bytes.fromhex(item))
            elif isinstance(item, (bytes, bytearray)):
                spent.add(bytes(item))
            elif isinstance(item, dict):
                ki = item.get("key_image") or item.get("keyImage") or item.get("key")
                if ki:
                    spent.add(bytes.fromhex(ki) if isinstance(ki, str) else bytes(ki))
        return spent

    def _pairs(self) -> list:
        return self.keystore.get_keys()

    def _addresses_for_pairs(self) -> list[tuple]:
        return [
            (pair, pair.address(self.params.stealth_version)) for pair in self._pairs()
        ]

    def _scan(self) -> list[tuple[bytes, Coin, int, str]]:
        """Scan the chain for outputs owned by this wallet.

        Returns:
            ``(one_time_key, Coin, spend_key_int, address_str)`` for every
            recognized output, including spent ones.
        """
        current_height = self.height()
        if self._scan_cache is not None:
            cached_height, cached = self._scan_cache
            if cached_height == current_height:
                return cached

        pair_addrs = self._addresses_for_pairs()
        results: list[tuple[bytes, Coin, int, str]] = []

        for h in range(current_height + 1):
            block = self.client.getblock(h, True)
            for tx in block["transactions"]:
                r_hex = tx.get("tx_public_key") or ""
                if not r_hex:
                    continue
                try:
                    r_point = point_from_bytes(bytes.fromhex(r_hex))
                except Exception:
                    continue
                is_cb = bool(tx.get("coinbase", False))
                for out in tx.get("outputs", []):
                    otk_hex = out.get("one_time_key") or out.get("oneTimeKey") or ""
                    if not otk_hex:
                        continue
                    try:
                        p_point = point_from_bytes(bytes.fromhex(otk_hex))
                    except Exception:
                        continue
                    p_bytes = bytes.fromhex(otk_hex)
                    for pair, addr in pair_addrs:
                        if recognize_output(r_point, p_point, pair.view_secret, addr):
                            spend = spend_key_for_output(
                                r_point, pair.view_secret, pair.spend_secret
                            )
                            coin = Coin(
                                value=int(out["value"]),
                                height=h,
                                is_coinbase=is_cb,
                            )
                            results.append((p_bytes, coin, spend, str(addr)))
                            break

        self._scan_cache = (current_height, results)
        return results

    def coins(self, *, spendable_only: bool = True) -> list[tuple[bytes, Coin, int]]:
        """Return the wallet's unspent outputs.

        Each entry is ``(one_time_key, Coin, spend_key_int)``.

        Raises:
            RpcClientError: if the node cannot be reached.
        """
        current_height = self.height()
        next_height = current_height + 1
        spent = self._spent_key_images()
        maturity = self.params.coinbase_maturity
        result: list[tuple[bytes, Coin, int]] = []
        for otk, coin, spend, _addr in self._scan():
            if spendable_only and not coin.is_spendable_at(next_height, maturity):
                continue
            ki = schnorr_point_to_bytes(spend * hash_to_point(otk))
            if ki in spent:
                continue
            result.append((otk, coin, spend))
        return result

    def balance(self) -> Balance:
        """Return the wallet's total balance across every address.

        Raises:
            RpcClientError: if the node cannot be reached.
        """
        current_height = self.height()
        next_height = current_height + 1
        spent = self._spent_key_images()
        maturity = self.params.coinbase_maturity
        confirmed = spendable = immature = count = 0
        for otk, coin, spend, _addr in self._scan():
            ki = schnorr_point_to_bytes(spend * hash_to_point(otk))
            if ki in spent:
                continue
            count += 1
            confirmed += coin.value
            if coin.is_spendable_at(next_height, maturity):
                spendable += coin.value
            else:
                immature += coin.value
        return Balance(confirmed, spendable, immature, count)

    def balances_by_address(self) -> list[tuple[str, str, int]]:
        """Return ``(address, label, balance)`` for every address in the wallet.

        Raises:
            RpcClientError: if the node cannot be reached.
        """
        current_height = self.height()
        next_height = current_height + 1
        spent = self._spent_key_images()
        maturity = self.params.coinbase_maturity

        label_map: dict[str, str] = {}
        for record in self.keystore.addresses():
            label_map[record.address] = record.label

        by_addr: dict[str, int] = {}
        for otk, coin, spend, addr_str in self._scan():
            ki = schnorr_point_to_bytes(spend * hash_to_point(otk))
            if ki in spent:
                continue
            if coin.is_spendable_at(next_height, maturity):
                by_addr[addr_str] = by_addr.get(addr_str, 0) + coin.value

        for record in self.keystore.addresses():
            by_addr.setdefault(record.address, 0)

        return [(addr, label_map.get(addr, ""), balance) for addr, balance in by_addr.items()]

    def history(self, limit: int = 50) -> list[dict]:
        """Return the wallet's transaction history, newest first.

        Raises:
            RpcClientError: if the node cannot be reached.
        """
        current_height = self.height()
        pair_addrs = self._addresses_for_pairs()

        # --- pass 1: collect owned outputs and their key-images
        owned_ki: dict[bytes, int] = {}      # key_image → value
        entries: dict[str, dict] = {}        # txid → {...}

        for h in range(current_height + 1):
            block = self.client.getblock(h, True)
            for tx in block["transactions"]:
                txid = tx.get("txid", "")
                r_hex = tx.get("tx_public_key") or ""
                if not r_hex or not txid:
                    continue
                try:
                    r_point = point_from_bytes(bytes.fromhex(r_hex))
                except Exception:
                    continue
                for out in tx.get("outputs", []):
                    otk_hex = out.get("one_time_key") or out.get("oneTimeKey") or ""
                    if not otk_hex:
                        continue
                    try:
                        p_point = point_from_bytes(bytes.fromhex(otk_hex))
                    except Exception:
                        continue
                    p_bytes = bytes.fromhex(otk_hex)
                    for pair, addr in pair_addrs:
                        if recognize_output(r_point, p_point, pair.view_secret, addr):
                            spend = spend_key_for_output(
                                r_point, pair.view_secret, pair.spend_secret
                            )
                            ki = schnorr_point_to_bytes(spend * hash_to_point(p_bytes))
                            value = int(out["value"])
                            owned_ki[ki] = value
                            entry = entries.setdefault(
                                txid,
                                {
                                    "txid": txid,
                                    "height": h,
                                    "received": 0,
                                    "sent": 0,
                                    "net": 0,
                                    "confirmations": max(1, current_height - h + 1),
                                },
                            )
                            entry["received"] += value
                            entry["net"] += value
                            break

        # --- pass 2: find which transactions spent our outputs
        for h in range(current_height + 1):
            block = self.client.getblock(h, True)
            for tx in block["transactions"]:
                txid = tx.get("txid", "")
                for txin in tx.get("inputs", []):
                    if txin.get("coinbase"):
                        continue
                    ki_hex = txin.get("key_image") or txin.get("keyImage") or ""
                    if not ki_hex:
                        continue
                    try:
                        ki = bytes.fromhex(ki_hex)
                    except Exception:
                        continue
                    value = owned_ki.get(ki)
                    if value is not None:
                        entry = entries.setdefault(
                            txid,
                            {
                                "txid": txid,
                                "height": h,
                                "received": 0,
                                "sent": 0,
                                "net": 0,
                                "confirmations": max(1, current_height - h + 1),
                            },
                        )
                        entry["sent"] += value
                        entry["net"] -= value

        ordered = sorted(entries.values(), key=lambda e: (e["height"], e["txid"]))
        return list(reversed(ordered))[:limit]

    # -------------------------------------------------------------------- keys

    def new_address(self, label: str = "") -> str:
        """Create a new address and save the wallet."""
        address = self.keystore.new_key(label)
        self.keystore.save()
        return address

    # ------------------------------------------------------------------ spending

    def default_fee_rate(self) -> int:
        """Return the fee rate used when the caller does not choose one."""
        return self.params.min_relay_fee_per_kb

    def _parse_destination(self, destination: str) -> StealthAddress:
        try:
            return StealthAddress.decode(
                destination, expected_version=self.params.stealth_version
            )
        except StealthError as exc:
            raise WalletError(str(exc)) from exc

    def _change_address(self) -> StealthAddress:
        return StealthAddress.decode(
            self.keystore.default_address(),
            expected_version=self.params.stealth_version,
        )

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
        built = build_anonymous_transaction(
            self,
            [(target, amount)],
            self._change_address(),
            fee_per_kb or self.default_fee_rate(),
            self.params,
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
        rate = fee_per_kb or self.default_fee_rate()
        total = sum(coin.value for _, coin, _ in coins)

        # Ask for the whole balance; the builder selects every coin and reduces
        # the amount by the fee it converges on. When that over-shoots, back off
        # by the estimated fee and retry until the amount actually fits.
        amount = total
        built = None
        for _ in range(16):
            try:
                built = build_anonymous_transaction(
                    self, [(target, amount)], self._change_address(), rate, self.params
                )
                break
            except InsufficientFundsError:
                amount -= fee_for_size(
                    estimate_size_v2(len(coins), 1, DEFAULT_RING_SIZE), rate
                )
                if amount <= 0:
                    raise InsufficientFundsError(
                        f"the {total} scar available does not cover the fee"
                    ) from None
        assert built is not None
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