"""Stress-test a ScarletCoin node by sending transactions between two wallets at a steady TPS rate.

The script works in four stages:

1. ``init`` — create two wallets (A and B) and print their addresses for funding.
2. ``split`` — split wallet A's balance into many equal UTXOs (so each can be spent independently).
3. ``run``  — send transactions from A → B at the target TPS rate, measuring real throughput.
4. ``ping-pong`` — alternate direction when one wallet is drained, for continuous load.

Example::

    uv run python tools/two_wallet_tps.py --network regtest --tps 50

    # Or step by step:
    uv run python tools/two_wallet_tps.py init --network regtest
    # ... fund wallet A ...
    uv run python tools/two_wallet_tps.py split --utxos 5000 --network regtest
    uv run python tools/two_wallet_tps.py run --tps 50 --network regtest
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scarletcoin import __version__
from scarletcoin.cli_common import (
    add_connection_arguments,
    add_network_arguments,
    die,
    read_rpc_token,
    setup_logging,
)
from scarletcoin.core.params import get_params
from scarletcoin.core.transaction import OutPoint
from scarletcoin.core.utxo import Coin
from scarletcoin.crypto.keys import Address
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.units import format_amount
from scarletcoin.wallet.builder import (
    BASE_BYTES,
    PER_INPUT_BYTES,
    PER_OUTPUT_BYTES,
    InsufficientFundsError,
    build_sweep_transaction,
    build_transaction,
    estimate_size,
    fee_for_size,
)
from scarletcoin.wallet.keystore import Keystore, WalletError
from scarletcoin.wallet.wallet import Wallet

__all__ = ["TwoWalletTpsError"]

DEFAULT_TPS = 50
DEFAULT_UTXOS = 5_000
DEFAULT_AMOUNT = 1
DEFAULT_CONFIRM_TIMEOUT = 300.0


class TwoWalletTpsError(Exception):
    """A recoverable test failure, reported on the command line."""


def _wallet_path(datadir: Path, network: str, name: str) -> Path:
    return Path(datadir) / network / name


def _make_client(args: argparse.Namespace) -> RpcClient:
    url = args.rpc_url or f"http://127.0.0.1:{get_params(args.network).default_rpc_port}"
    token = args.rpc_token or read_rpc_token(args.datadir, args.network)
    return RpcClient(url, token=token, timeout=args.timeout)


def _load_keystore(path: Path, network: str) -> Keystore:
    try:
        keystore = Keystore.load(path)
    except WalletError as exc:
        die(str(exc))
    if keystore.locked:
        die(f"{path} is encrypted; create a fresh wallet with 'init'")
    if keystore.params.name != network:
        die(f"{path} belongs to the {keystore.params.name} network, not {network}")
    return keystore


def _make_wallet(path: Path, network: str, client: RpcClient) -> Wallet:
    return Wallet(_load_keystore(path, network), client)


def _amount(scar: int) -> str:
    return f"{format_amount(scar)} SCT"


def _max_split_outputs(params, input_count: int) -> int:
    usable = params.max_block_size // 2 - BASE_BYTES - input_count * PER_INPUT_BYTES
    return max(0, usable // PER_OUTPUT_BYTES)


# --------------------------------------------------------------------------- init


def _cmd_init(args: argparse.Namespace) -> int:
    path_a = _wallet_path(args.datadir, args.network, args.wallet_a)
    path_b = _wallet_path(args.datadir, args.network, args.wallet_b)

    for p in (path_a, path_b):
        if p.exists():
            die(f"{p} already exists; remove it first or pick different names")

    ks_a = Keystore.create(path_a, args.network, password=None)
    ks_b = Keystore.create(path_b, args.network, password=None)

    addr_a = ks_a.default_address()
    addr_b = ks_b.default_address()
    mnemonic_a = ks_a.new_mnemonic

    print(f"wallet A: {path_a}")
    print(f"wallet B: {path_b}")
    print()
    print(f"address A (fund this): {addr_a}")
    print(f"address B (return dst): {addr_b}")
    print()
    if mnemonic_a:
        print(f"recovery phrase A: {mnemonic_a}")
        print(f"recovery phrase B: {ks_b.new_mnemonic}")
        print()
    print("send coins to wallet A, then:")
    print(f"  uv run python tools/two_wallet_tps.py split "
                f"--utxos {DEFAULT_UTXOS} --network {args.network}")
    return 0


# ------------------------------------------------------------------------- split


def _wait_for_confirmation(client: RpcClient, txid: str, *, timeout: float, network: str) -> None:
    deadline = time.monotonic() + timeout
    generate = network == "regtest"
    while True:
        if generate:
            try:
                client.call("generate", 1)
            except RpcClientError:
                generate = False
        try:
            data = client.gettransaction(txid)
        except RpcClientError:
            data = {}
        if data.get("confirmations", 0) >= 1:
            return
        if time.monotonic() >= deadline:
            raise TwoWalletTpsError(
                f"timed out after {timeout:.0f}s waiting for the split to be mined"
            )
        time.sleep(2.0)


def _cmd_split(args: argparse.Namespace) -> int:
    client = _make_client(args)
    path_a = _wallet_path(args.datadir, args.network, args.wallet_a)
    path_b = _wallet_path(args.datadir, args.network, args.wallet_b)

    if not path_a.exists():
        die(f"{path_a} does not exist; run 'init' first")
    if not path_b.exists():
        die(f"{path_b} does not exist; run 'init' first")

    ks_a = _load_keystore(path_a, args.network)
    wallet_a = Wallet(ks_a, client)

    coins = wallet_a.coins(spendable_only=True)
    if not coins:
        die("wallet A has no spendable coins; fund it first")

    total = sum(coin.value for _, coin in coins)
    print(f"wallet A balance: {_amount(total)} from {len(coins)} UTXOs")

    utxos = args.utxos
    params = ks_a.params
    max_out = _max_split_outputs(params, len(coins))
    if utxos > max_out:
        die(f"cannot create {utxos} outputs (max {max_out} for {len(coins)}-input tx)")

    fee_per_kb = args.fee_rate or params.min_relay_fee_per_kb
    fee = fee_for_size(estimate_size(len(coins), utxos + 1), fee_per_kb)
    distribute = total - fee
    if distribute <= 0:
        die("balance too small to cover the split fee")

    per = distribute // utxos
    if per < args.amount + fee_for_size(estimate_size(1, 2), fee_per_kb):
        die(f"balance too small: each UTXO would be {per} scar, cannot fund {args.amount} scar txs")

    remainder = distribute - per * utxos

    default_hash = Address.decode(
        ks_a.default_address(), expected_version=params.address_version
    ).hash
    outputs = [(default_hash, per)] * utxos
    outputs[0] = (default_hash, per + remainder)

    built = build_transaction(
        spendable_coins=coins,
        keys=ks_a.keys_by_hash(),
        outputs=outputs,
        change_hash=default_hash,
        fee_per_kb=fee_per_kb,
        params=params,
    )

    if built.size > params.max_block_size // 2:
        die(f"split tx is {built.size} bytes (relay limit {params.max_block_size // 2})")

    txid = client.sendrawtransaction(built.transaction.serialize().hex())
    print(f"split tx {txid} ({built.size} bytes, fee {built.fee} scar)")
    print(f"created {utxos} UTXOs of ~{_amount(per)} each")

    if not args.no_confirm:
        _wait_for_confirmation(client, txid, timeout=args.confirm_timeout, network=params.name)
        confirmed = wallet_a.coins(spendable_only=True)
        print(f"split confirmed at height {client.getblockcount()}:"
              f" {len(confirmed)} spendable UTXOs")
    else:
        print("skipping confirmation wait (--no-confirm)")

    print(f"next: uv run python tools/two_wallet_tps.py run "
              f"--tps {DEFAULT_TPS} --network {args.network}")
    return 0


# ----------------------------------------------------------------------------- run


def _build_one(
    outpoint: OutPoint,
    coin: Coin,
    keys: dict,
    destination: bytes,
    fee_per_kb: int,
    params,
) -> tuple[str | None, str | None]:
    try:
        built = build_sweep_transaction(
            spendable_coins=[(outpoint, coin)],
            keys=keys,
            destination=destination,
            fee_per_kb=fee_per_kb,
            params=params,
        )
        return built.transaction.serialize().hex(), None
    except (InsufficientFundsError, ValueError) as exc:
        return None, str(exc)


def _prefill_queue(
    coins: list,
    keys: dict,
    destination: bytes,
    fee_per_kb: int,
    params,
    count: int,
) -> deque:
    queue: deque = deque()
    to_build = coins[:count]
    if not to_build:
        return queue
    with ThreadPoolExecutor(max_workers=min(8, len(to_build))) as executor:
        future_map = {
            executor.submit(_build_one, op, c, keys, destination, fee_per_kb, params): (op, c)
            for op, c in to_build
        }
        for fut in as_completed(future_map):
            raw, _err = fut.result()
            if raw:
                queue.append(raw)
    return queue


def _run_continuous(
    *,
    keystore_a: Keystore,
    keystore_b: Keystore,
    client: RpcClient,
    tps: int,
    amount: int,
    duration: float | None,
    fee_per_kb: int | None,
    ping_pong: bool,
) -> dict:
    """Continuously send transactions from A → B at target TPS, optionally ping-ponging.

    Every transaction sweeps one confirmed UTXO to the other wallet, so each
    output is spent exactly once.  Outpoints already sent are tracked locally
    (``getutxos`` still reports them until a block confirms), and broadcasts
    happen in parallel: a sequential HTTP round-trip per transaction would cap
    the rate at ~1/latency tx/s no matter what ``tps`` says.
    """
    params = keystore_a.params
    fee_per_kb = fee_per_kb or params.min_relay_fee_per_kb

    wallet_a = Wallet(keystore_a, client)
    wallet_b = Wallet(keystore_b, client)

    addr_a_hash = Address.decode(
        keystore_a.default_address(), expected_version=params.address_version
    ).hash
    addr_b_hash = Address.decode(
        keystore_b.default_address(), expected_version=params.address_version
    ).hash

    keys_a = keystore_a.keys_by_hash()
    keys_b = keystore_b.keys_by_hash()

    direction = "a_to_b"  # or "b_to_a"
    used: set[OutPoint] = set()
    fresh: list[tuple[OutPoint, Coin]] = []
    pending: deque[str] = deque()

    accepted = 0
    duplicates = 0
    rejected = 0
    build_failures = 0
    start_time = time.monotonic()
    deadline = start_time + duration if duration else None

    interval = 1.0 / tps
    buffer_target = max(tps * 2, 50)
    workers = max(8, min(64, tps // 2 + 8))

    def sender() -> Wallet:
        return wallet_a if direction == "a_to_b" else wallet_b

    def receiver() -> Wallet:
        return wallet_b if direction == "a_to_b" else wallet_a

    def sender_keys() -> dict:
        return keys_a if direction == "a_to_b" else keys_b

    def sender_dst() -> bytes:
        return addr_b_hash if direction == "a_to_b" else addr_a_hash

    def refill_fresh() -> None:
        nonlocal fresh
        fresh = [item for item in sender().coins(spendable_only=True) if item[0] not in used]

    def switch_direction() -> None:
        nonlocal direction, fresh
        old = direction
        direction = "b_to_a" if direction == "a_to_b" else "a_to_b"
        fresh = []
        print(f"\nswitching direction: {old} -> {direction}", flush=True)

    print(f"\ntarget: {tps} tx/s, amount: {_amount(amount)} per tx")
    if ping_pong:
        print("mode: ping-pong (alternates A→B and B→A as blocks confirm)")
    if duration:
        print(f"duration: {duration:.0f}s")
    print()

    # Per-thread RPC clients: parallel broadcasts hide the HTTP round-trip time.
    thread_local = threading.local()

    def init_worker() -> None:
        thread_local.client = RpcClient(client.url, token=client.token, timeout=client.timeout)

    def send_one(raw_hex: str) -> str:
        try:
            thread_local.client.sendrawtransaction(raw_hex)
            return "accepted"
        except RpcClientError as exc:
            message = str(exc)
            if "already in the mempool" in message or "already in a block" in message:
                return "duplicate"
            return "rejected"

    refill_fresh()
    pool = ThreadPoolExecutor(max_workers=workers, initializer=init_worker)
    inflight: list = []
    last_report = start_time
    next_send = time.monotonic()
    waiting_since: float | None = None

    def reap() -> None:
        nonlocal accepted, duplicates, rejected
        still = []
        for future in inflight:
            if future.done():
                status = future.result()
                if status == "accepted":
                    accepted += 1
                elif status == "duplicate":
                    duplicates += 1
                else:
                    rejected += 1
            else:
                still.append(future)
        inflight[:] = still

    try:
        while True:
            now = time.monotonic()
            if deadline and now >= deadline:
                break

            reap()

            if not pending:
                if not fresh:
                    refill_fresh()
                if fresh:
                    take = min(buffer_target, len(fresh))
                    chunk = fresh[:take]
                    del fresh[:take]
                    for outpoint, _coin in chunk:
                        used.add(outpoint)
                    built = _prefill_queue(
                        chunk, sender_keys(), sender_dst(), fee_per_kb, params, take
                    )
                    build_failures += take - len(built)
                    pending.extend(built)
                if not pending:
                    if ping_pong and receiver().coins(spendable_only=True):
                        switch_direction()
                        refill_fresh()
                        continue
                    if not ping_pong:
                        print("\nwallet drained, stopping", flush=True)
                        break
                    if waiting_since is None:
                        waiting_since = time.monotonic()
                        print(
                            "\nwaiting for blocks to confirm the other wallet's coins...",
                            flush=True,
                        )
                    elif time.monotonic() - waiting_since >= 30.0:
                        waiting_since = time.monotonic()
                    time.sleep(2.0)
                    continue
                waiting_since = None

            raw = pending.popleft()
            inflight.append(pool.submit(send_one, raw))

            next_send += interval
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_send = time.monotonic()

            t = time.monotonic()
            if t - last_report >= 2.0:
                last_report = t
                elapsed = t - start_time
                try:
                    mempool = client.call("getmempool")["count"]
                except RpcClientError:
                    mempool = "?"
                print(
                    f"\rsent {accepted} | {accepted / elapsed:.1f} tx/s | "
                    f"queued {len(pending)} | mempool {mempool} | "
                    f"rejected {rejected} | elapsed {elapsed:.0f}s",
                    end="",
                    flush=True,
                )
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    elapsed = time.monotonic() - start_time
    avg_tps = accepted / elapsed if elapsed > 0 else 0
    print(
        f"\n\nfinal: {accepted} accepted, {duplicates} duplicates,"
        f" {rejected} rejected in {elapsed:.1f}s ({avg_tps:.1f} tx/s)"
    )
    if build_failures:
        print(f"note: {build_failures} coins were too small to cover a fee and were skipped")

    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
        "elapsed": elapsed,
        "tps": avg_tps,
    }


def _cmd_run(args: argparse.Namespace) -> int:
    client = _make_client(args)
    path_a = _wallet_path(args.datadir, args.network, args.wallet_a)
    path_b = _wallet_path(args.datadir, args.network, args.wallet_b)

    if not path_a.exists():
        die(f"{path_a} does not exist; run 'init' first")
    if not path_b.exists():
        die(f"{path_b} does not exist; run 'init' first")

    ks_a = _load_keystore(path_a, args.network)
    ks_b = _load_keystore(path_b, args.network)

    wallet_a = Wallet(ks_a, client)
    coins = wallet_a.coins(spendable_only=True)
    if not coins:
        die("wallet A has no spendable coins; fund it and run 'split' first")

    total = sum(coin.value for _, coin in coins)
    fee_per_kb = args.fee_rate or ks_a.params.min_relay_fee_per_kb
    min_needed = args.amount + fee_for_size(estimate_size(1, 2), fee_per_kb)
    if total < min_needed * 10:
        print(f"warning: wallet A only has {_amount(total)} ({len(coins)} UTXOs)")
        print(f"         each tx needs at least {_amount(min_needed)}")

    try:
        _run_continuous(
            keystore_a=ks_a,
            keystore_b=ks_b,
            client=client,
            tps=args.tps,
            amount=args.amount,
            duration=args.duration,
            fee_per_kb=args.fee_rate,
            ping_pong=args.ping_pong,
        )
    except TwoWalletTpsError as exc:
        die(str(exc))
    return 0


# -------------------------------------------------------------------------- status


def _cmd_status(args: argparse.Namespace) -> int:
    client = _make_client(args)
    path_a = _wallet_path(args.datadir, args.network, args.wallet_a)
    path_b = _wallet_path(args.datadir, args.network, args.wallet_b)

    for label, path in [("A", path_a), ("B", path_b)]:
        if not path.exists():
            print(f"wallet {label}: not found ({path})")
            continue
        wallet = _make_wallet(path, args.network, client)
        try:
            balance = wallet.balance()
            info = wallet.client.getinfo()
        except RpcClientError as exc:
            die(str(exc))
        print(f"wallet {label}: {_amount(balance.spendable)} spendable, {balance.utxo_count} UTXOs")
        print(f"          {path}")

    try:
        info = client.getinfo()
        print(
                f"node:      height {info['height']}, {info['peers']} peers,"
                f" {info['mempool_size']} tx in mempool"
            )
    except RpcClientError as exc:
        print(f"node:      unavailable ({exc})")
    return 0


# -------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="two_wallet_tps",
        description="Send transactions between two wallets at a steady TPS rate.",
    )
    parser.add_argument("--version", action="version", version=f"scarletcoin {__version__}")

    def add_common(sub: argparse.ArgumentParser) -> None:
        add_network_arguments(sub)
        add_connection_arguments(sub)
        sub.add_argument(
            "--wallet-a",
            default="tps_wallet_a.json",
            help="filename for wallet A (default: %(default)s)",
        )
        sub.add_argument(
            "--wallet-b",
            default="tps_wallet_b.json",
            help="filename for wallet B (default: %(default)s)",
        )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create two test wallets")
    add_common(init_parser)

    status_parser = subparsers.add_parser("status", help="show balance of both wallets")
    add_common(status_parser)

    split_parser = subparsers.add_parser("split", help="split wallet A balance into many UTXOs")
    add_common(split_parser)
    split_parser.add_argument(
        "--utxos",
        type=int,
        default=DEFAULT_UTXOS,
        help="how many equal UTXOs to create (default: %(default)s)",
    )
    split_parser.add_argument("--fee-rate", type=int, help="fee in scar per kilobyte")
    split_parser.add_argument("--amount", type=int, default=DEFAULT_AMOUNT,
                              help="amount per tx in scar")
    split_parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="do not wait for the split to be mined",
    )
    split_parser.add_argument(
        "--confirm-timeout",
        type=float,
        default=DEFAULT_CONFIRM_TIMEOUT,
        help="how long to wait for confirmation (default: %(default)s)",
    )

    run_parser = subparsers.add_parser("run", help="start sending transactions at target TPS")
    add_common(run_parser)
    run_parser.add_argument(
        "--tps",
        type=int,
        default=DEFAULT_TPS,
        help="target transactions per second (default: %(default)s)",
    )
    run_parser.add_argument(
        "--amount",
        type=int,
        default=DEFAULT_AMOUNT,
        help="amount per transaction in scar (default: %(default)s)",
    )
    run_parser.add_argument("--fee-rate", type=int, help="fee in scar per kilobyte")
    run_parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="stop after this many seconds (default: run until drained)",
    )
    run_parser.add_argument(
        "--ping-pong",
        action="store_true",
        help="switch direction when one wallet is drained (A→B then B→A...)",
    )
    return parser


_COMMANDS = {
    "init": _cmd_init,
    "status": _cmd_status,
    "split": _cmd_split,
    "run": _cmd_run,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("\ncancelled")
        return 130


if __name__ == "__main__":
    sys.exit(main())