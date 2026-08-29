"""``scarlet-miner``: mine ScarletCoins with a node's help."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from scarletcoin import __version__
from scarletcoin.cli_common import (
    add_connection_arguments,
    add_network_arguments,
    add_node_choice_arguments,
    die,
    maybe_check_version,
    setup_logging,
)
from scarletcoin.miner.miner import Miner, MiningError
from scarletcoin.net.chooser import NodeChoiceError, resolve_client
from scarletcoin.net.client import RpcClientError

__all__ = ["main"]

logger = logging.getLogger("scarletcoin.miner")


def _format_rate(rate: float) -> str:
    for unit in ("H/s", "kH/s", "MH/s", "GH/s"):
        if rate < 1000:
            return f"{rate:6.2f} {unit}"
        rate /= 1000
    return f"{rate:6.2f} TH/s"  # pragma: no cover - wishful thinking


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``scarlet-miner``."""
    parser = argparse.ArgumentParser(
        prog="scarlet-miner",
        description="Mine ScarletCoins: get work from a node, search for a nonce, submit blocks.",
    )
    parser.add_argument("--version", action="version", version=f"scarletcoin {__version__}")
    parser.add_argument("address", help="address the block rewards are paid to")
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 2) - 1),
        help="how many CPU processes to mine with (default: %(default)s)",
    )
    parser.add_argument(
        "--refresh",
        type=float,
        default=15.0,
        help="seconds between requests for fresh work (default: %(default)s)",
    )
    parser.add_argument(
        "--max-rate",
        type=float,
        metavar="HASHES_PER_SEC",
        help="cap the hash rate so the machine is not saturated (e.g. 1000)",
    )
    parser.add_argument("--blocks", type=int, help="stop after mining this many blocks")
    parser.add_argument("--quiet", action="store_true", help="only report mined blocks")
    add_network_arguments(parser)
    add_connection_arguments(parser)
    add_node_choice_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``scarlet-miner``."""
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    maybe_check_version(args.datadir / args.network)
    # The command line reports mined blocks itself, so keep the library quiet --
    # but only when a person is watching. Under a service supervisor the log file
    # is the only record, so leave the logger audible there.
    if args.log_level == "info" and sys.stdout.isatty():
        logging.getLogger("scarletcoin.miner").setLevel(logging.WARNING)
    try:
        client = resolve_client(args, for_mining=True)
    except NodeChoiceError as exc:
        die(str(exc))

    try:
        info = client.getinfo()
    except RpcClientError as exc:
        die(str(exc))
    print(
        f"mining on {info['network']} at height {info['height']},"
        f" difficulty {info['difficulty']:.6g}, paying {args.address}",
        flush=True,
    )
    try:
        client.getblocktemplate()
    except RpcClientError as exc:
        if exc.code in (401, -32001):
            die(
                f"the node at {client.url} will not hand out mining work without its"
                " token.\nUse a node of your own (--node local), or ask that operator"
                " for the token\nand pass it with --rpc-token."
            )
        die(str(exc))

    last_report = 0.0

    def on_event(kind: str, payload: dict) -> None:
        nonlocal last_report
        if kind == "progress":
            now = time.time()
            if args.quiet or now - last_report < 2.0:
                return
            last_report = now
            print(
                f"\r{_format_rate(miner.stats.last_rate)}"
                f"  avg {_format_rate(miner.stats.average_rate)}"
                f"  blocks {miner.stats.blocks_accepted}"
                f"  height {miner.stats.height}",
                end="",
                flush=True,
            )
        elif kind == "accepted":
            print(f"\rmined block {payload['hash']} at height {payload['height']}", flush=True)
        elif kind == "rejected":
            print(f"\rblock {payload['hash']} was rejected: {payload['reason']}", flush=True)
        elif kind == "error":
            print(f"\r{payload['message']}", flush=True)

    miner = Miner(
        client,
        args.address,
        workers=args.workers,
        refresh_seconds=args.refresh,
        max_rate=args.max_rate,
        on_event=on_event,
    )

    def shutdown(*_signal: object) -> None:
        miner.stop()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), shutdown)

    try:
        stats = miner.run(max_blocks=args.blocks)
    except MiningError as exc:
        die(str(exc))
    print(
        f"\nstopped after {stats.elapsed:.0f}s:"
        f" {stats.hashes} hashes, {stats.blocks_accepted} blocks accepted"
        f", {stats.blocks_rejected} rejected",
        flush=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
