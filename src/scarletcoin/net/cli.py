"""``scarlet-node``: run a ScarletCoin node, or talk to a running one."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys

from scarletcoin import __version__
from scarletcoin.cli_common import (
    add_connection_arguments,
    add_network_arguments,
    die,
    generate_rpc_token,
    make_client,
    setup_logging,
    write_rpc_token,
)
from scarletcoin.core.chain import MIN_PRUNE_KEEP, prune_database
from scarletcoin.core.storage import inspect_database
from scarletcoin.net.client import RpcClientError
from scarletcoin.net.node import Node, NodeConfig
from scarletcoin.net.rpc import MINING_METHODS, PUBLIC_METHODS, RpcServer
from scarletcoin.units import format_bytes

logger = logging.getLogger("scarletcoin.node")

_LOOPBACK = ("127.0.0.1", "::1", "localhost")

#: Options that belong to ``scarlet-node`` itself rather than to ``run``.
_OWN_OPTIONS = ("--version", "-h", "--help")


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``scarlet-node``."""
    parser = argparse.ArgumentParser(
        prog="scarlet-node",
        description="Run a ScarletCoin node: validate blocks, relay them and serve RPC.",
    )
    parser.add_argument("--version", action="version", version=f"scarletcoin {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="run the node (default)")
    add_network_arguments(run)
    run.add_argument("--p2p-port", type=int, help="port to listen on for peers")
    run.add_argument(
        "--p2p-host", default="0.0.0.0", help="address to listen on (default: %(default)s)"
    )
    run.add_argument("--no-listen", action="store_true", help="do not accept inbound peers")
    run.add_argument(
        "--addnode",
        "--connect",
        dest="connect",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="also try this peer, on top of the ones already known (may be repeated)",
    )
    run.add_argument(
        "--seed",
        dest="seeds",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="bootstrap from this seed host name, resolving all of its addresses",
    )
    run.add_argument(
        "--no-seeds", action="store_true", help="ignore the seed peers built into this build"
    )
    run.add_argument(
        "--max-outbound", type=int, default=8, help="outbound peer slots (default: %(default)s)"
    )
    run.add_argument(
        "--max-inbound", type=int, default=64, help="inbound peer slots (default: %(default)s)"
    )
    run.add_argument("--no-rpc", action="store_true", help="do not start the RPC server")
    run.add_argument(
        "--rpc-host", default="127.0.0.1", help="RPC bind address (default: %(default)s)"
    )
    run.add_argument("--rpc-port", type=int, help="RPC port")
    run.add_argument(
        "--rpc-token",
        help="require this bearer token on RPC calls (a random one is generated otherwise)",
    )
    run.add_argument(
        "--no-rpc-token",
        action="store_true",
        help="serve RPC without authentication (loopback only)",
    )
    run.add_argument(
        "--rpc-public",
        action="store_true",
        help="let anyone call the read-only and broadcast methods without the token,"
        " so remote wallets and explorers can use this node",
    )
    run.add_argument(
        "--rpc-public-mining",
        action="store_true",
        help="also hand out and accept mining work without the token, so remote"
        " miners can use this node (implies --rpc-public)",
    )
    run.add_argument(
        "--rpc-advertise",
        metavar="URL",
        help="the address other people should use to reach this node, for example"
        " https://scarletcoin.example.net; reported by getpublicnodes",
    )
    run.add_argument(
        "--public-peer",
        dest="public_peers",
        action="append",
        default=[],
        metavar="URL",
        help="another public node to tell clients about (may be repeated)",
    )
    run.add_argument(
        "--prune",
        type=int,
        default=0,
        metavar="BLOCKS",
        help="keep only this many recent blocks whole, dropping older bodies to save"
        f" disk (minimum {MIN_PRUNE_KEEP}; 0 keeps the entire chain)",
    )

    call = subparsers.add_parser("rpc", help="call a method on a running node")
    add_network_arguments(call)
    add_connection_arguments(call)
    call.add_argument("method", help="method name, for example getinfo")
    call.add_argument("params", nargs="*", help="positional parameters (JSON or plain text)")

    info = subparsers.add_parser("info", help="show a running node's status")
    add_network_arguments(info)
    add_connection_arguments(info)

    size = subparsers.add_parser("size", help="show how much disk this network's chain uses")
    add_network_arguments(size)

    prune = subparsers.add_parser(
        "prune", help="drop the bodies of old blocks to reclaim disk space"
    )
    add_network_arguments(prune)
    add_connection_arguments(prune)
    prune.add_argument(
        "--keep",
        type=int,
        default=MIN_PRUNE_KEEP,
        metavar="BLOCKS",
        help="how many recent blocks to keep whole (default: %(default)s)",
    )
    prune.add_argument(
        "--no-vacuum",
        action="store_true",
        help="do not rebuild the database afterwards (faster, but the file does not shrink)",
    )
    prune.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    return parser


def _run(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    if args.prune and args.prune < MIN_PRUNE_KEEP:
        logger.warning(
            "--prune %d is below the %d block minimum; keeping %d",
            args.prune,
            MIN_PRUNE_KEEP,
            MIN_PRUNE_KEEP,
        )
    config = NodeConfig(
        network=args.network,
        datadir=args.datadir,
        listen=not args.no_listen,
        p2p_host=args.p2p_host,
        p2p_port=args.p2p_port,
        rpc=not args.no_rpc,
        rpc_host=args.rpc_host,
        rpc_port=args.rpc_port,
        rpc_public=args.rpc_public or args.rpc_public_mining,
        rpc_public_mining=args.rpc_public_mining,
        rpc_advertise=args.rpc_advertise,
        public_peers=tuple(args.public_peers),
        prune=max(0, args.prune),
        max_outbound=args.max_outbound,
        max_inbound=args.max_inbound,
        connect=tuple(args.connect),
        seeds=tuple(args.seeds),
        use_seeds=not args.no_seeds,
    )

    token: str | None = None
    if config.rpc:
        if args.rpc_token:
            token = args.rpc_token
        elif args.no_rpc_token:
            if args.rpc_host not in _LOOPBACK:
                die("--no-rpc-token is only allowed when the RPC server binds to loopback")
            token = None
        else:
            token = generate_rpc_token()

    node = Node(config)
    rpc: RpcServer | None = None
    if config.rpc:
        rpc = RpcServer(
            node,
            host=args.rpc_host,
            port=config.resolved_rpc_port(),
            token=token,
            public=config.rpc_public,
            public_mining=config.rpc_public_mining,
        )
        if token:
            path = write_rpc_token(args.datadir, args.network, token)
            logger.info("RPC token written to %s", path)

    def shutdown(*_signal: object) -> None:
        node.stop()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), shutdown)

    try:
        node.start()
        stats = node.chain.stats()
        print(
            f"chain: height {stats['height']}, {stats['chain_size']} of blocks"
            f" ({stats['disk_size']} on disk)"
        )
        if config.prune:
            logger.info(
                "pruning is on: only the last %d blocks are kept whole, so this node"
                " cannot serve old blocks to a peer syncing from scratch",
                max(config.prune, node.chain.min_prune_keep),
            )
        if rpc is not None:
            rpc.start()
            print(f"explorer: {rpc.url}")
            if rpc.public:
                logger.info(
                    "public RPC is on: anyone may call %d read-only and broadcast"
                    " methods; everything else still needs the token",
                    len(PUBLIC_METHODS),
                )
            if rpc.public_mining:
                logger.info(
                    "public mining is on: anyone may call %s without the token",
                    ", ".join(sorted(MINING_METHODS)),
                )
            if rpc.public and not config.rpc_advertise:
                logger.info(
                    "no --rpc-advertise given, so this node will not tell clients"
                    " its own address; other public nodes cannot pass it on"
                )
        if not node.seed_hosts and not config.connect:
            logger.warning(
                "no seeds and no --addnode peers: this node will stay alone unless"
                " another node connects to it"
            )
        node.wait()
    except RuntimeError as exc:
        die(str(exc))
    finally:
        if rpc is not None:
            rpc.stop()
        node.stop()
    return 0


def _parse_param(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _rpc(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        result = client.call(args.method, *[_parse_param(item) for item in args.params])
    except RpcClientError as exc:
        die(str(exc))
    print(json.dumps(result, indent=2))
    return 0


def _info(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        info = client.getinfo()
    except RpcClientError as exc:
        die(str(exc))
    width = max(len(key) for key in info)
    for key, value in info.items():
        print(f"{key.replace('_', ' '):<{width}}  {value}")
    return 0


def _size(args: argparse.Namespace) -> int:
    """Report the size of the chain on this machine, running node or not."""
    path = NodeConfig(network=args.network, datadir=args.datadir).chain_path
    summary = inspect_database(path)
    print(f"database   {summary['path']}")
    if not summary["exists"]:
        print("            no chain here yet")
        return 0
    if summary["error"]:
        print(f"            unreadable: {summary['error']}")
        print(f"on disk    {format_bytes(summary['disk_bytes'])}")
        return 0
    blocks = summary["blocks"] or 0
    print(f"height     {summary['height']}")
    print(f"blocks     {blocks} stored, {summary['pruned_blocks']} pruned")
    print(f"blockchain {format_bytes(summary['chain_bytes'])} of active-chain blocks")
    print(f"on disk    {format_bytes(summary['disk_bytes'])} including indexes and the UTXO set")
    if blocks:
        print(f"average    {format_bytes(round(summary['chain_bytes'] / blocks))} per block")
    if summary["prune_height"]:
        print(f"pruned     everything up to height {summary['prune_height']}")
    return 0


def _prune(args: argparse.Namespace) -> int:
    """Prune through a running node when there is one, otherwise in place."""
    keep = max(0, args.keep)
    if keep < MIN_PRUNE_KEEP:
        die(f"--keep must be at least {MIN_PRUNE_KEEP} blocks")
    config = NodeConfig(network=args.network, datadir=args.datadir)
    if not config.chain_path.exists():
        die(f"no {args.network} chain at {config.chain_path}")
    if not args.yes:
        print(
            f"Pruning keeps the last {keep} blocks whole and drops the bodies of"
            " everything older.\nBalances stay correct, but this node will no longer be"
            " able to show those blocks,\nserve them to a peer syncing from scratch, or"
            " reorganise past them. This cannot be undone."
        )
        if input(f"prune {args.network} to the last {keep} blocks? [y/N] ").strip().lower() not in (
            "y",
            "yes",
        ):
            print("cancelled")
            return 1

    try:
        result = make_client(args).call("prune", keep, not args.no_vacuum)
    except RpcClientError as exc:
        if exc.code is not None:
            die(str(exc))
        result = _prune_offline(config, keep, vacuum=not args.no_vacuum)
    print(
        f"pruned {result['blocks']} block(s) up to height {result['prune_height']},"
        f" freeing {result['freed_size']}"
    )
    print(f"database is now {result['disk_size']}")
    return 0


def _prune_offline(config: NodeConfig, keep: int, *, vacuum: bool) -> dict:
    """Prune a chain nothing is currently serving."""
    outcome, disk = prune_database(config.chain_path, config.params, keep, vacuum=vacuum)
    return {
        "blocks": outcome.blocks,
        "prune_height": outcome.prune_height,
        "freed_bytes": outcome.freed_bytes,
        "freed_size": format_bytes(outcome.freed_bytes),
        "disk_bytes": disk,
        "disk_size": format_bytes(disk),
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``scarlet-node``."""
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    # ``scarlet-node --rpc-public`` means ``scarlet-node run --rpc-public``: the
    # daemon is what people came for. The parser's own options are left alone, or
    # ``--help`` would only ever describe ``run``.
    if not arguments or (arguments[0].startswith("-") and arguments[0] not in _OWN_OPTIONS):
        arguments.insert(0, "run")
    args = parser.parse_args(arguments)
    if args.command == "rpc":
        return _rpc(args)
    if args.command == "info":
        return _info(args)
    if args.command == "size":
        return _size(args)
    if args.command == "prune":
        return _prune(args)
    return _run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
