"""Shared plumbing for the command line tools."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

from scarletcoin.core.params import get_params, network_names
from scarletcoin.net.client import RpcClient
from scarletcoin.version_check import check_version

__all__ = [
    "DEFAULT_DATADIR",
    "NodeConnection",
    "add_connection_arguments",
    "add_network_arguments",
    "add_node_choice_arguments",
    "die",
    "forget_connection",
    "load_connection",
    "local_url",
    "make_client",
    "maybe_check_version",
    "read_rpc_token",
    "save_connection",
    "setup_logging",
    "write_rpc_token",
]

#: Where node and wallet data live unless told otherwise.
DEFAULT_DATADIR = Path(os.environ.get("SCARLETCOIN_DATADIR") or Path.home() / ".scarletcoin")

TOKEN_FILENAME = "rpc.token"

#: Remembers which node the user chose, so nobody is asked twice.  Shared by the
#: command line tools and the desktop applications.
CONNECTION_FILENAME = "node.json"

#: What the desktop applications used to write before the file above existed.
_LEGACY_CONNECTION_FILENAME = "gui.json"


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


def add_node_choice_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the options that decide *which* node a tool talks to.

    Only the tools that have to reach a node offer these: the answer is
    remembered, so somebody who picks a public node once is never asked again.
    """
    parser.add_argument(
        "--node",
        default=os.environ.get("SCARLETCOIN_NODE"),
        metavar="local|public|ask|URL",
        help="which node to use: 'local' for one on this machine, 'public' to pick"
        " the best public node, 'ask' to be offered the choice, or a node URL."
        " Remembered afterwards; the default asks once and then reuses the answer",
    )
    parser.add_argument(
        "--start-node",
        action="store_true",
        help="start a local node if none is running, without asking",
    )
    parser.add_argument(
        "--no-start-node",
        action="store_true",
        help="never start a local node",
    )
    parser.add_argument(
        "--forget-node",
        action="store_true",
        help="ignore the remembered node and choose again",
    )


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


def local_url(network: str) -> str:
    """The RPC URL of a node running on this machine."""
    return f"http://127.0.0.1:{get_params(network).default_rpc_port}"


@dataclass
class NodeConnection:
    """Where a tool should look for a node, and with what token."""

    url: str
    token: str = ""

    def client(self, timeout: float = 30.0) -> RpcClient:
        """Build a client from this connection."""
        return RpcClient(self.url, token=self.token or None, timeout=timeout)

    def is_local(self, network: str) -> bool:
        """Whether this points at the default node on this machine."""
        return self.url.rstrip("/") == local_url(network)


def connection_path(datadir: str | Path, network: str) -> Path:
    """Where the chosen node is remembered."""
    return Path(datadir) / network / CONNECTION_FILENAME


def load_connection(datadir: str | Path, network: str) -> NodeConnection | None:
    """Read the remembered node, or ``None`` if nothing has been chosen yet.

    Falls back to ``gui.json``, which earlier releases wrote, so upgrading does
    not throw away a URL somebody typed once.
    """
    for name in (CONNECTION_FILENAME, _LEGACY_CONNECTION_FILENAME):
        try:
            data = json.loads((Path(datadir) / network / name).read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        url = str(data.get("rpc_url") or "").strip()
        if url:
            return NodeConnection(url, str(data.get("rpc_token") or ""))
    return None


def save_connection(datadir: str | Path, network: str, connection: NodeConnection) -> None:
    """Remember a node so the next run does not have to ask."""
    target = connection_path(datadir, network)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"rpc_url": connection.url, "rpc_token": connection.token}, indent=1),
            "utf-8",
        )
    except OSError as exc:  # pragma: no cover - disk errors
        logging.getLogger(__name__).warning("could not remember the node: %s", exc)
        return
    with contextlib.suppress(OSError):  # the token in it is a secret
        target.chmod(0o600)


def forget_connection(datadir: str | Path, network: str) -> None:
    """Drop the remembered node."""
    for name in (CONNECTION_FILENAME, _LEGACY_CONNECTION_FILENAME):
        with contextlib.suppress(OSError):
            (Path(datadir) / network / name).unlink()


def make_client(args: argparse.Namespace) -> RpcClient:
    """Build an :class:`RpcClient` from parsed command line arguments."""
    url = args.rpc_url or local_url(args.network)
    token = args.rpc_token or read_rpc_token(args.datadir, args.network)
    return RpcClient(url, token=token, timeout=args.timeout)


def maybe_check_version(datadir: str | Path) -> None:
    """Log a warning when a newer ScarletCoin release is on PyPI.
    
    Called once at start-up by every CLI tool.  The check is cached for a day
    and never blocks — a slow or unreachable PyPI is silently ignored.
    """
    import logging
    
    logger = logging.getLogger(__name__)
    try:
        latest = check_version(datadir)
    except Exception:
        return
    if latest is not None:
        from scarletcoin import __version__ as current
        
        logger.warning(
            "ScarletCoin %s is available (you are running %s)."
            " Upgrade with: pip install --upgrade scarletcoin",
            latest,
            current,
        )
