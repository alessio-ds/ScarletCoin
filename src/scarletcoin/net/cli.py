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
from scarletcoin.net.client import RpcClientError
from scarletcoin.net.node import Node, NodeConfig
from scarletcoin.net.rpc import RpcServer

logger = logging.getLogger("scarletcoin.node")

_LOOPBACK = ("127.0.0.1", "::1", "localhost")


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

    call = subparsers.add_parser("rpc", help="call a method on a running node")
    add_network_arguments(call)
    add_connection_arguments(call)
    call.add_argument("method", help="method name, for example getinfo")
    call.add_argument("params", nargs="*", help="positional parameters (JSON or plain text)")

    info = subparsers.add_parser("info", help="show a running node's status")
    add_network_arguments(info)
    add_connection_arguments(info)

    return parser


def _run(args: argparse.Namespace) -> int:
    setup_logging(args.log_level)
    config = NodeConfig(
        network=args.network,
        datadir=args.datadir,
        listen=not args.no_listen,
        p2p_host=args.p2p_host,
        p2p_port=args.p2p_port,
        rpc=not args.no_rpc,
        rpc_host=args.rpc_host,
        rpc_port=args.rpc_port,
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
        rpc = RpcServer(node, host=args.rpc_host, port=config.resolved_rpc_port(), token=token)
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
        if rpc is not None:
            rpc.start()
            print(f"explorer: {rpc.url}")
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


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``scarlet-node``."""
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or (arguments[0].startswith("-") and arguments[0] != "--version"):
        arguments.insert(0, "run")
    args = parser.parse_args(arguments)
    if args.command == "rpc":
        return _rpc(args)
    if args.command == "info":
        return _info(args)
    return _run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
