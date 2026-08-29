"""``scarlet-wallet``: create wallets, check balances and send ScarletCoins."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from scarletcoin import __version__
from scarletcoin.cli_common import (
    add_connection_arguments,
    add_network_arguments,
    add_node_choice_arguments,
    die,
    maybe_check_version,
    setup_logging,
)
from scarletcoin.net.chooser import NodeChoiceError, resolve_client
from scarletcoin.net.client import RpcClientError
from scarletcoin.units import format_amount, parse_amount
from scarletcoin.wallet.builder import InsufficientFundsError
from scarletcoin.wallet.keystore import Keystore, WalletError, WalletLocked
from scarletcoin.wallet.wallet import Wallet

__all__ = ["main"]


def default_wallet_path(datadir: Path, network: str) -> Path:
    """Return the default wallet file for a network."""
    return Path(datadir) / network / "wallet.json"


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``scarlet-wallet``."""
    parser = argparse.ArgumentParser(
        prog="scarlet-wallet",
        description="A command line ScarletCoin wallet. Keys stay on this machine.",
    )
    parser.add_argument("--version", action="version", version=f"scarletcoin {__version__}")
    parser.add_argument(
        "--wallet", type=Path, help="wallet file (default: <datadir>/<network>/wallet.json)"
    )
    add_network_arguments(parser)
    add_connection_arguments(parser)
    add_node_choice_arguments(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a new wallet")
    create.add_argument("--no-password", action="store_true", help="store the keys unencrypted")

    restore = subparsers.add_parser("restore", help="rebuild a wallet from its seed phrase")
    restore.add_argument(
        "mnemonic", nargs="?", help="the seed phrase; read from a prompt if omitted"
    )
    restore.add_argument("--no-password", action="store_true", help="store the keys unencrypted")

    subparsers.add_parser("info", help="show balances and node status")
    subparsers.add_parser("balance", help="show the total balance")
    subparsers.add_parser("addresses", help="list the wallet's addresses")

    new = subparsers.add_parser("new", help="create a new address")
    new.add_argument("label", nargs="?", default="", help="optional label")

    send = subparsers.add_parser("send", help="send coins")
    send.add_argument("address", help="destination address")
    send.add_argument("amount", help="amount in SCT, or 'all'")
    send.add_argument("--fee-rate", type=int, help="fee in scar per kilobyte")
    send.add_argument(
        "--dry-run", action="store_true", help="build and show the transaction without sending"
    )
    send.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    history = subparsers.add_parser("history", help="show past transactions")
    history.add_argument("--limit", type=int, default=25, help="how many to show")

    subparsers.add_parser("unspent", help="list unspent outputs")

    export = subparsers.add_parser("export", help="print an address's private key")
    export.add_argument("address", nargs="?", help="which address (default: the first one)")

    imp = subparsers.add_parser("import", help="import a private key")
    imp.add_argument("wif", nargs="?", help="the private key; read from a prompt if omitted")
    imp.add_argument("--label", default="imported", help="label for the imported key")

    label = subparsers.add_parser("label", help="rename an address")
    label.add_argument("address")
    label.add_argument("label")

    passwd = subparsers.add_parser("password", help="set, change or remove the password")
    passwd.add_argument(
        "--remove", action="store_true", help="store the keys unencrypted from now on"
    )

    return parser


# --------------------------------------------------------------------------- utils


def _wallet_path(args: argparse.Namespace) -> Path:
    return args.wallet or default_wallet_path(args.datadir, args.network)


def _prompt_password(confirm: bool = False) -> str:
    password = getpass.getpass("Password: ")
    if not password:
        die("an empty password is not allowed; use --no-password instead")
    if confirm and getpass.getpass("Repeat password: ") != password:
        die("the passwords do not match")
    return password


def _load_keystore(args: argparse.Namespace, *, need_keys: bool) -> Keystore:
    path = _wallet_path(args)
    try:
        keystore = Keystore.load(path)
    except WalletError as exc:
        die(str(exc))
    if keystore.locked and need_keys:
        for attempt in range(3):
            try:
                keystore.unlock(getpass.getpass("Password: "))
                break
            except WalletError as exc:
                print(f"error: {exc}", file=sys.stderr)
                if attempt == 2:
                    die("too many failed attempts")
    if keystore.params.name != args.network:
        die(
            f"{path} belongs to the {keystore.params.name} network"
            f" but --network says {args.network}"
        )
    return keystore


def _make_wallet(args: argparse.Namespace, *, need_keys: bool = False) -> Wallet:
    keystore = _load_keystore(args, need_keys=need_keys)
    try:
        client = resolve_client(args)
    except NodeChoiceError as exc:
        die(str(exc))
    return Wallet(keystore, client)


def _amount(scar: int) -> str:
    return f"{format_amount(scar)} SCT"


# ------------------------------------------------------------------------ commands


def _cmd_create(args: argparse.Namespace) -> int:
    path = _wallet_path(args)
    password = None if args.no_password else _prompt_password(confirm=True)
    try:
        keystore = Keystore.create(path, args.network, password=password)
    except WalletError as exc:
        die(str(exc))
    mnemonic = keystore.new_mnemonic
    print(f"created {path} for the {args.network} network")
    print(f"first address: {keystore.default_address()}")
    if mnemonic:
        print("\nThis is the wallet's recovery phrase. Write it down and keep it secret:")
        print(f"  {mnemonic}\n")
        print("Anyone who has these words can spend the coins. They are shown only now.")
    if password is None:
        print("warning: the wallet is stored unencrypted")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    path = _wallet_path(args)
    mnemonic = args.mnemonic or getpass.getpass("Recovery phrase: ")
    password = None if args.no_password else _prompt_password(confirm=True)
    try:
        keystore = Keystore.restore(path, args.network, mnemonic, password=password)
    except WalletError as exc:
        die(str(exc))
    print(f"restored {path} for the {args.network} network")
    print(f"first address: {keystore.default_address()}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    wallet = _make_wallet(args)
    keystore = wallet.keystore
    print(f"wallet     {keystore.path}")
    print(f"network    {keystore.params.name}")
    print(f"encrypted  {'yes' if keystore.encrypted else 'no'}")
    print(f"addresses  {len(keystore.addresses())}")
    try:
        balance = wallet.balance()
        info = wallet.client.getinfo()
    except RpcClientError as exc:
        die(str(exc))
    print(f"balance    {_amount(balance.confirmed)}")
    print(f"spendable  {_amount(balance.spendable)}")
    if balance.immature:
        print(f"immature   {_amount(balance.immature)} (mined coins that are still maturing)")
    print(
        f"node       height {info['height']}, {info['peers']} peers,"
        f" {info['mempool_size']} tx in mempool"
    )
    return 0


def _cmd_balance(args: argparse.Namespace) -> int:
    wallet = _make_wallet(args)
    try:
        balance = wallet.balance()
    except RpcClientError as exc:
        die(str(exc))
    print(_amount(balance.spendable))
    return 0


def _cmd_addresses(args: argparse.Namespace) -> int:
    wallet = _make_wallet(args)
    try:
        rows = wallet.balances_by_address()
    except RpcClientError as exc:
        die(str(exc))
    width = max((len(address) for address, _, _ in rows), default=0)
    for address, label, balance in rows:
        print(f"{address:<{width}}  {_amount(balance):>18}  {label}")
    return 0


def _cmd_new(args: argparse.Namespace) -> int:
    wallet = _make_wallet(args, need_keys=True)
    print(wallet.new_address(args.label))
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    wallet = _make_wallet(args, need_keys=True)
    send_all = str(args.amount).strip().lower() == "all"
    try:
        amount = 0 if send_all else parse_amount(args.amount)
    except ValueError as exc:
        die(str(exc))
    if not send_all and amount <= 0:
        die("the amount must be greater than zero")

    try:
        if send_all:
            results = wallet.send_everything(
                args.address, fee_per_kb=args.fee_rate, broadcast=False
            )
        else:
            results = [wallet.send(args.address, amount, fee_per_kb=args.fee_rate, broadcast=False)]
    except (WalletError, InsufficientFundsError) as exc:
        die(str(exc))
    except RpcClientError as exc:
        die(str(exc))

    results = list(results)
    paid = sum(
        sum(output.value for output in result.transaction.outputs) - result.change
        for result in results
    )
    total_fee = sum(result.fee for result in results)
    print(f"to      {args.address}")
    print(f"amount  {_amount(paid)}")
    print(f"fee     {_amount(total_fee)} ({sum(r.size for r in results)} bytes)")
    if len(results) == 1:
        if results[0].change:
            print(f"change  {_amount(results[0].change)}")
        print(f"txid    {results[0].transaction.txid_hex()}")
    else:
        print(f"split into {len(results)} transactions:")
        for result in results:
            print(f"  txid  {result.transaction.txid_hex()}  fee {_amount(result.fee)}")

    if args.dry_run:
        print("\ndry run: nothing was broadcast")
        for result in results:
            print(result.transaction.serialize().hex())
        return 0
    if not args.yes:
        answer = input("send these transactions? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("cancelled")
            return 1
    try:
        for result in results:
            txid = wallet.client.sendrawtransaction(result.transaction.serialize().hex())
            print(f"broadcast {txid}")
    except RpcClientError as exc:
        die(str(exc))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    wallet = _make_wallet(args)
    try:
        entries = wallet.history(args.limit)
    except RpcClientError as exc:
        die(str(exc))
    if not entries:
        print("no transactions yet")
        return 0
    print(f"{'height':>7}  {'amount':>18}  {'conf':>5}  txid")
    for item in entries:
        sign = "+" if item["net"] >= 0 else "-"
        print(
            f"{item['height']:>7}  {sign}{_amount(abs(item['net'])):>17}"
            f"  {item['confirmations']:>5}  {item['txid']}"
        )
    return 0


def _cmd_unspent(args: argparse.Namespace) -> int:
    wallet = _make_wallet(args)
    try:
        coins = wallet.coins(spendable_only=False)
    except RpcClientError as exc:
        die(str(exc))
    if not coins:
        print("no unspent outputs")
        return 0
    for outpoint, coin in coins:
        kind = "coinbase" if coin.is_coinbase else "payment"
        print(
            f"{outpoint.txid[::-1].hex()}:{outpoint.index}"
            f"  {_amount(coin.value):>18}  height {coin.height}  {kind}"
        )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    keystore = _load_keystore(args, need_keys=True)
    address = args.address or keystore.default_address()
    try:
        wif = keystore.export_wif(address)
    except WalletError as exc:
        die(str(exc))
    print(f"address     {address}")
    print(f"private key {wif}")
    print("\nkeep this secret: anyone who has it can spend the coins on that address")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    keystore = _load_keystore(args, need_keys=True)
    wif = args.wif or getpass.getpass("Private key: ")
    try:
        address = keystore.import_wif(wif, args.label)
        keystore.save()
    except WalletError as exc:
        die(str(exc))
    print(f"imported {address}")
    return 0


def _cmd_label(args: argparse.Namespace) -> int:
    keystore = _load_keystore(args, need_keys=True)
    try:
        keystore.set_label(args.address, args.label)
        keystore.save()
    except WalletError as exc:
        die(str(exc))
    print(f"{args.address} is now labelled {args.label!r}")
    return 0


def _cmd_password(args: argparse.Namespace) -> int:
    keystore = _load_keystore(args, need_keys=True)
    try:
        if args.remove:
            keystore.set_password(None)
            print("the wallet is now stored unencrypted")
        else:
            keystore.set_password(_prompt_password(confirm=True))
            print("the wallet has been encrypted")
    except (WalletError, WalletLocked) as exc:
        die(str(exc))
    return 0


_COMMANDS = {
    "create": _cmd_create,
    "restore": _cmd_restore,
    "info": _cmd_info,
    "balance": _cmd_balance,
    "addresses": _cmd_addresses,
    "new": _cmd_new,
    "send": _cmd_send,
    "history": _cmd_history,
    "unspent": _cmd_unspent,
    "export": _cmd_export,
    "import": _cmd_import,
    "label": _cmd_label,
    "password": _cmd_password,
}


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``scarlet-wallet``."""
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    maybe_check_version(args.datadir / args.network)
    handler = _COMMANDS[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        print("\ncancelled")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
