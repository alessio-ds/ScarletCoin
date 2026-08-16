"""Load-test a ScarletCoin node: how many transactions per second can it take?

The test works in three stages and is non-destructive: every coin the wallet
spends comes back to it, so the balance only shrinks by the fees.

1. ``init`` — create a test wallet and print the address to fund.
2. ``split`` — spend the whole balance in one transaction that creates ``N``
   equal outputs at the wallet's own address, then wait for them to be mined.
3. ``run`` — build and sign one transaction per UTXO, then broadcast them all
   in parallel and report the acceptance rate (TPS) plus latency percentiles.
   With ``--watch`` it also follows how fast blocks clear the mempool.

Because the test spends each UTXO exactly once there is no double spending, and
because every output goes back to the same wallet the test can be repeated.
Coins mined by the wallet are not touched until they mature (the node only
reports spendable outputs).

Example::

    uv run python tools/tps_test.py init --network mainnet
    # ... fund the printed address ...
    uv run python tools/tps_test.py split --utxos 15000 --network mainnet
    uv run python tools/tps_test.py run --workers 8 --network mainnet --watch 300

On ``regtest`` the node can mine instantly, so the script calls ``generate``
itself to confirm the split and (during ``--watch``) to clear the mempool. On
mainnet and testnet the split needs a real miner before ``run`` can find the
UTXOs; start one with ``scarlet-miner`` or let a public network mine it.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
    dust_threshold,
    estimate_size,
    fee_for_size,
)
from scarletcoin.wallet.keystore import Keystore, WalletError
from scarletcoin.wallet.wallet import Wallet

__all__ = ["TpsError", "run_burst", "split_utxos"]

#: How many UTXOs a split produces by default.
DEFAULT_UTXOS = 15_000
#: Seconds a split will wait for its transaction to be mined.
DEFAULT_CONFIRM_TIMEOUT = 300.0


class TpsError(Exception):
    """A recoverable test failure, reported on the command line."""


def _default_wallet_path(datadir: Path, network: str) -> Path:
    return Path(datadir) / network / "wallet.json"


def _make_client(args: argparse.Namespace) -> RpcClient:
    url = args.rpc_url or f"http://127.0.0.1:{get_params(args.network).default_rpc_port}"
    token = args.rpc_token or read_rpc_token(args.datadir, args.network)
    return RpcClient(url, token=token, timeout=args.timeout)


def _load_keystore(args: argparse.Namespace) -> Keystore:
    path = args.wallet or _default_wallet_path(args.datadir, args.network)
    try:
        keystore = Keystore.load(path)
    except WalletError as exc:
        die(str(exc))
    if keystore.locked:
        die(f"{path} is encrypted; tps_test needs the keys. Create one with 'init'")
    if keystore.params.name != args.network:
        die(f"{path} belongs to the {keystore.params.name} network, not {args.network}")
    return keystore


def _make_wallet(args: argparse.Namespace) -> Wallet:
    return Wallet(_load_keystore(args), _make_client(args))


def _amount(scar: int) -> str:
    return f"{format_amount(scar)} SCT"


def _max_split_outputs(params, input_count: int) -> int:
    """Largest number of outputs a single relayed transaction may carry.

    The node refuses to relay transactions larger than half a block, so the
    split (one transaction, many outputs) is limited by ``max_block_size / 2``.
    """
    usable = params.max_block_size // 2 - BASE_BYTES - input_count * PER_INPUT_BYTES
    return max(0, usable // PER_OUTPUT_BYTES)


# --------------------------------------------------------------------------- init


def _cmd_init(args: argparse.Namespace) -> int:
    path = args.wallet or _default_wallet_path(args.datadir, args.network)
    if path.exists():
        die(f"{path} already exists; use 'status' instead")
    keystore = Keystore.create(path, args.network, password=None)
    for _ in range(max(0, args.addresses - 1)):
        keystore.new_key("load-test")
    keystore.save()
    print(f"created {path} (private keys stored unencrypted)")
    print(f"funding address: {keystore.default_address()}")
    print()
    print("send some coins to that address, then:")
    command = (
        f"  uv run python tools/tps_test.py split --utxos {DEFAULT_UTXOS} --network {args.network}"
    )
    print(command)
    return 0


# -------------------------------------------------------------------------- status


def _cmd_status(args: argparse.Namespace) -> int:
    keystore = _load_keystore(args)
    wallet = _make_wallet(args)
    print(f"wallet     {keystore.path}")
    print(f"network    {keystore.params.name}")
    print(f"addresses  {len(keystore.addresses())}")
    try:
        balance = wallet.balance()
        info = wallet.client.getinfo()
    except RpcClientError as exc:
        die(str(exc))
    print(f"confirmed  {_amount(balance.confirmed)}")
    print(f"spendable  {_amount(balance.spendable)}")
    print(f"immature   {_amount(balance.immature)}")
    print(f"utxos      {balance.utxo_count}")
    print(
        f"node       height {info['height']}, {info['peers']} peers,"
        f" {info['mempool_size']} tx in mempool"
    )
    return 0


# --------------------------------------------------------------------------- split


def split_utxos(
    keystore: Keystore,
    client: RpcClient,
    *,
    utxos: int,
    fee_per_kb: int | None = None,
    confirm: bool = True,
    confirm_timeout: float = DEFAULT_CONFIRM_TIMEOUT,
) -> dict:
    """Split the wallet's spendable balance into ``utxos`` equal outputs.

    All outputs pay the wallet's own default address, so afterwards the wallet
    owns ``utxos`` freshly confirmed coins of equal size. One transaction is
    used; the node's relay limit (half a block) caps ``utxos``.

    Returns a summary of what happened.
    """
    params = keystore.params
    wallet = Wallet(keystore, client)
    if utxos <= 0:
        raise TpsError("--utxos must be positive")

    coins = wallet.coins(spendable_only=True)
    if not coins:
        raise TpsError("the wallet has no spendable coins — fund it first")
    total = sum(coin.value for _, coin in coins)
    max_out = _max_split_outputs(params, len(coins))
    if utxos > max_out:
        raise TpsError(
            f"cannot create {utxos} outputs in one transaction: the node relays at most"
            f" {max_out} for a {len(coins)}-input transaction (half a block)"
        )

    fee_per_kb = fee_per_kb or params.min_relay_fee_per_kb
    fee = fee_for_size(estimate_size(len(coins), utxos + 1), fee_per_kb)
    distribute = total - fee
    if distribute <= 0:
        raise TpsError("the balance is too small to cover the split's fee")
    per = distribute // utxos
    if per < 1:
        raise TpsError(f"the balance is too small to create {utxos} outputs of at least one scar")
    remainder = distribute - per * utxos

    default_hash = Address.decode(
        keystore.default_address(), expected_version=params.address_version
    ).hash
    outputs = [(default_hash, per)] * utxos
    outputs[0] = (default_hash, per + remainder)
    built = build_transaction(
        spendable_coins=coins,
        keys=keystore.keys_by_hash(),
        outputs=outputs,
        change_hash=default_hash,
        fee_per_kb=fee_per_kb,
        params=params,
    )
    if built.size > params.max_block_size // 2:
        limit = params.max_block_size // 2
        raise TpsError(
            f"the split transaction is {built.size} bytes, larger than the {limit}"
            " byte relay limit; use fewer --utxos"
        )

    txid = client.sendrawtransaction(built.transaction.serialize().hex())
    print(f"split transaction {txid} ({built.size} bytes, fee {built.fee} scar)")
    print(f"creating {utxos} outputs of {_amount(per)} each")
    if per < dust_threshold(fee_per_kb):
        print("warning: each output is below the dust threshold and would cost more to spend")
    if not confirm:
        return {"txid": txid, "utxos": utxos, "per": per, "confirmed": False}

    _wait_for_confirmation(client, txid, timeout=confirm_timeout, network=params.name)
    confirmed = wallet.coins(spendable_only=True)
    print(
        f"split confirmed at height {client.getblockcount()}:"
        f" {len(confirmed)} spendable UTXOs in the wallet"
    )
    return {"txid": txid, "utxos": len(confirmed), "per": per, "confirmed": True}


def _wait_for_confirmation(client: RpcClient, txid: str, *, timeout: float, network: str) -> None:
    """Poll the node until ``txid`` is mined.

    On regtest the node can mine instantly, so the script asks for one block to
    confirm the split instead of waiting for a real miner.
    """
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
            raise TpsError(
                f"timed out after {timeout:.0f}s waiting for the split to be mined;"
                " run a miner or retry"
            )
        time.sleep(2.0)


def _cmd_split(args: argparse.Namespace) -> int:
    keystore = _load_keystore(args)
    client = _make_client(args)
    try:
        split_utxos(
            keystore,
            client,
            utxos=args.utxos,
            fee_per_kb=args.fee_rate,
            confirm=not args.no_confirm,
            confirm_timeout=args.confirm_timeout,
        )
    except (TpsError, InsufficientFundsError, RpcClientError) as exc:
        die(str(exc))
    print("next: uv run python tools/tps_test.py run --workers 8 --watch 300")
    return 0


# ------------------------------------------------------------------------------ run


def _latency_stats(latencies: list[float]) -> dict:
    if not latencies:
        return {}
    ordered = sorted(latencies)

    def percentile(ratio: float) -> float:
        index = min(len(ordered) - 1, int(ratio * len(ordered)))
        return ordered[index]

    return {
        "mean": statistics.fmean(ordered),
        "median": percentile(0.5),
        "p95": percentile(0.95),
        "max": ordered[-1],
    }


def run_burst(
    keystore: Keystore,
    client: RpcClient,
    *,
    txs: int | None = None,
    workers: int = 8,
    fee_per_kb: int | None = None,
    watch: float = 0.0,
) -> dict:
    """Spend the wallet's UTXOs in parallel and measure the acceptance rate.

    One transaction per UTXO is built and signed first (CPU-bound, parallel),
    then all of them are broadcast through the node's JSON-RPC interface while
    the wall-clock time and per-call latency are recorded. Every transaction
    sends its whole coin back to the wallet's default address.

    With ``watch`` seconds set, the mempool is polled afterwards; on regtest the
    node is asked to mine until it drains, everywhere else a real miner has to.
    """
    params = keystore.params
    wallet = Wallet(keystore, client)
    coins = wallet.coins(spendable_only=True)
    if not coins:
        raise TpsError("the wallet has no spendable coins — fund it and run 'split' first")
    if txs is not None:
        if txs <= 0:
            raise TpsError("--txs must be positive")
        coins = coins[:txs]
    if len(coins) < 100:
        print("warning: only a few UTXOs — run 'split --utxos N' first for a meaningful test")
    print(f"spending {len(coins)} UTXOs with {workers} workers")

    keys = keystore.keys_by_hash()
    default_hash = Address.decode(
        keystore.default_address(), expected_version=params.address_version
    ).hash
    fee_per_kb = fee_per_kb or params.min_relay_fee_per_kb

    # ---- phase 1: build and sign everything (CPU-bound, parallel)
    def build_one(item) -> tuple[str | None, str | None]:
        try:
            built = build_sweep_transaction(
                spendable_coins=[item],
                keys=keys,
                destination=default_hash,
                fee_per_kb=fee_per_kb,
                params=params,
            )
            return built.transaction.serialize().hex(), None
        except (InsufficientFundsError, ValueError) as exc:
            return None, str(exc)

    build_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        built = list(executor.map(build_one, coins))
    build_time = time.monotonic() - build_start
    build_errors = [message for _, message in built if message is not None]
    pending = [raw for raw, _ in built if raw is not None]
    print(
        f"built and signed {len(pending)} transactions in {build_time:.2f}s"
        f" ({len(pending) / build_time:.0f} tx/s)"
    )
    if build_errors:
        for message in build_errors[:5]:
            print(f"build error: {message}")
        if len(build_errors) > 5:
            print(f"...and {len(build_errors) - 5} more build errors")

    # ---- phase 2: broadcast everything in parallel (I/O-bound)
    thread_local = threading.local()
    url = client.url
    token = client.token
    timeout = client.timeout

    def _init_worker() -> None:
        thread_local.client = RpcClient(url, token=token, timeout=timeout)

    def send_one(raw_hex: str) -> tuple[str, str | None, float]:
        start = time.monotonic()
        try:
            thread_local.client.sendrawtransaction(raw_hex)
            return "accepted", None, time.monotonic() - start
        except RpcClientError as exc:
            message = str(exc)
            if "already in the mempool" in message or "already in a block" in message:
                return "accepted", None, time.monotonic() - start
            return "rejected", message, time.monotonic() - start

    send_start = time.monotonic()
    accepted = 0
    latencies: list[float] = []
    rejected: Counter[str] = Counter()
    with ThreadPoolExecutor(max_workers=workers, initializer=_init_worker) as executor:
        for status, message, elapsed in executor.map(send_one, pending):
            if status == "accepted":
                accepted += 1
                latencies.append(elapsed)
            else:
                rejected[message] += 1
    elapsed = time.monotonic() - send_start

    stats = _latency_stats(latencies)
    print()
    print(
        f"broadcast {accepted}/{len(pending)} transactions in {elapsed:.2f}s"
        f" ({accepted / elapsed:.1f} tx/s accepted)"
    )
    if latencies:
        print(
            f"latency   mean {stats['mean'] * 1000:.1f} ms,"
            f" median {stats['median'] * 1000:.1f} ms,"
            f" p95 {stats['p95'] * 1000:.1f} ms,"
            f" max {stats['max'] * 1000:.1f} ms"
        )
    for message, count in rejected.most_common():
        print(f"rejected  {count}x {message}")

    result = {
        "attempted": len(pending),
        "accepted": accepted,
        "rejected": dict(rejected),
        "build_errors": build_errors,
        "elapsed": elapsed,
        "tps": accepted / elapsed if elapsed else 0.0,
        "latencies": stats,
        "watch": None,
    }
    if watch > 0:
        result["watch"] = _watch_mempool(client, seconds=watch, network=params.name)
    return result


def _watch_mempool(client: RpcClient, *, seconds: float, network: str) -> dict:
    """Poll the mempool until it drains or the deadline passes.

    On regtest the node is asked to mine blocks to clear the pool; elsewhere the
    script only watches and prints, leaving the mining to the network.
    """
    print("\nwatching the mempool drain...")
    start_height = client.getblockcount()
    deadline = time.monotonic() + seconds
    last_print = 0.0
    generate = network == "regtest"
    while time.monotonic() < deadline:
        time.sleep(2.0)
        try:
            if generate and client.call("getmempool")["count"] > 0:
                try:
                    client.call("generate", 1)
                except RpcClientError:
                    generate = False
            mempool = client.call("getmempool")["count"]
            height = client.getblockcount()
        except RpcClientError:
            continue
        if time.monotonic() - last_print >= 5.0 or mempool == 0:
            print(f"height {height:>7}  mempool {mempool:>6} tx")
            last_print = time.monotonic()
        if mempool == 0:
            break
    end_height = client.getblockcount()
    mempool_left = client.call("getmempool")["count"]
    drained = mempool_left == 0
    print(
        f"{end_height - start_height} blocks mined, mempool {mempool_left} tx"
        f" {'(drained)' if drained else '(still waiting for miners)'}"
    )
    return {"start_height": start_height, "end_height": end_height, "mempool_left": mempool_left}


def _cmd_run(args: argparse.Namespace) -> int:
    keystore = _load_keystore(args)
    client = _make_client(args)
    try:
        result = run_burst(
            keystore,
            client,
            txs=args.txs,
            workers=args.workers,
            fee_per_kb=args.fee_rate,
            watch=args.watch,
        )
    except (TpsError, RpcClientError) as exc:
        die(str(exc))
    print(
        f"\nresult: {result['accepted']}/{result['attempted']} accepted, {result['tps']:.1f} tx/s"
    )
    return 0 if result["accepted"] == result["attempted"] else 1


# -------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tps_test",
        description="Measure how many transactions per second a ScarletCoin node can take.",
    )
    parser.add_argument("--version", action="version", version=f"scarletcoin {__version__}")

    def add_common(sub: argparse.ArgumentParser) -> None:
        add_network_arguments(sub)
        add_connection_arguments(sub)
        sub.add_argument(
            "--wallet",
            type=Path,
            help="wallet file (default: <datadir>/<network>/wallet.json)",
        )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="create a test wallet")
    add_common(init_parser)
    init_parser.add_argument(
        "--addresses",
        type=int,
        default=1,
        help="how many receiving addresses to create (default: %(default)s)",
    )

    status_parser = subparsers.add_parser("status", help="show balance and UTXO count")
    add_common(status_parser)

    split_parser = subparsers.add_parser("split", help="split the balance into N UTXOs")
    add_common(split_parser)
    split_parser.add_argument(
        "--utxos",
        type=int,
        default=DEFAULT_UTXOS,
        help="how many equal outputs to create (default: %(default)s)",
    )
    split_parser.add_argument("--fee-rate", type=int, help="fee in scar per kilobyte")
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

    run_parser = subparsers.add_parser("run", help="burst-spend the UTXOs and measure TPS")
    add_common(run_parser)
    run_parser.add_argument(
        "--txs",
        type=int,
        help="how many transactions to send (default: one per UTXO)",
    )
    run_parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="parallel worker threads (default: %(default)s)",
    )
    run_parser.add_argument("--fee-rate", type=int, help="fee in scar per kilobyte")
    run_parser.add_argument(
        "--watch",
        type=float,
        default=0.0,
        help="seconds to watch the mempool drain after the burst (default: off)",
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
    except KeyboardInterrupt:  # pragma: no cover - interactive use
        print("\ncancelled")
        return 130


if __name__ == "__main__":
    sys.exit(main())
