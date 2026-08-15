"""Shared plumbing for the command line tools."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import secrets
import sys
from pathlib import Path

from scarletcoin.core.params import get_params, network_names
from scarletcoin.net.client import RpcClient

__all__ = [
    "DEFAULT_DATADIR",
    "add_connection_arguments",
    "add_network_arguments",
    "die",
    "make_client",
    "read_rpc_token",
    "setup_logging",
    "write_rpc_token",
]

#: Where node and wallet data live unless told otherwise.
DEFAULT_DATADIR = Path(os.environ.get("SCARLETCOIN_DATADIR") or Path.home() / ".scarletcoin")

TOKEN_FILENAME = "rpc.token"


def setup_logging(level: str = "info") -> None:
    """Configure logging for a command line tool."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
    )


def die(message: str, code: int = 1) -> None:
    """Print an error to stderr and exit."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def add_network_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``--network`` and ``--datadir``."""
    parser.add_argument(
        "--network",
        default=os.environ.get("SCARLETCOIN_NETWORK", "mainnet"),
        choices=network_names(),
        help="which network to use (default: %(default)s)",
    )
    parser.add_argument(
        "--datadir",
        type=Path,
        default=DEFAULT_DATADIR,
        help="directory holding node and wallet data (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="how much to log (default: %(default)s)",
    )


def add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the options needed to reach a node's RPC interface."""
    parser.add_argument(
        "--rpc-url",
        default=os.environ.get("SCARLETCOIN_RPC_URL"),
        help="node RPC URL (default: http://127.0.0.1:<network port>)",
    )
    parser.add_argument(
        "--rpc-token",
        default=os.environ.get("SCARLETCOIN_RPC_TOKEN"),
        help="RPC token; read from the node's rpc.token file when omitted",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="RPC timeout in seconds")


def token_path(datadir: Path, network: str) -> Path:
    """Path of the file holding the node's generated RPC token."""
    return Path(datadir) / network / TOKEN_FILENAME


def write_rpc_token(datadir: Path, network: str, token: str) -> Path:
    """Store an RPC token so local tools can authenticate without configuration."""
    path = token_path(datadir, network)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, "utf-8")
    with contextlib.suppress(OSError):  # some filesystems have no permission bits
        path.chmod(0o600)
    return path


def read_rpc_token(datadir: Path, network: str) -> str | None:
    """Read the node's RPC token, if this machine has one."""
    path = token_path(datadir, network)
    try:
        return path.read_text("utf-8").strip() or None
    except OSError:
        return None


def generate_rpc_token() -> str:
    """Return a fresh random RPC token."""
    return secrets.token_urlsafe(32)


def make_client(args: argparse.Namespace) -> RpcClient:
    """Build an :class:`RpcClient` from parsed command line arguments."""
    url = args.rpc_url or f"http://127.0.0.1:{get_params(args.network).default_rpc_port}"
    token = args.rpc_token or read_rpc_token(args.datadir, args.network)
    return RpcClient(url, token=token, timeout=args.timeout)
