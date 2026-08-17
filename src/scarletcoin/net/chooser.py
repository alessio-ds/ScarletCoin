"""Choosing a node from the command line.

Every tool that is not a node needs one to talk to, and there are two honest
answers: run one here, or use somebody else's. Neither should require reading the
documentation first, so the tools ask once, in plain words, and remember.

The same decisions are offered by the desktop applications
(:mod:`scarletcoin.gui.common`); this module is the terminal half, and it is
deliberately Qt-free so it can be used from a script.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from scarletcoin.cli_common import (
    NodeConnection,
    die,
    forget_connection,
    load_connection,
    local_url,
    parse_proxy,
    read_rpc_token,
    save_connection,
)
from scarletcoin.core.chain import MIN_PRUNE_KEEP
from scarletcoin.core.storage import inspect_database
from scarletcoin.net import directory
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.net.launcher import LocalNode, LocalNodeError, node_command
from scarletcoin.net.node import NodeConfig
from scarletcoin.units import format_bytes

__all__ = [
    "NodeChoiceError",
    "describe_local_chain",
    "resolve_client",
    "resolve_connection",
]

logger = logging.getLogger(__name__)

_KEYWORDS = ("ask", "local", "public", "auto")


class NodeChoiceError(RuntimeError):
    """Raised when no usable node could be found or the user gave up."""


# ------------------------------------------------------------------- inspection


def describe_local_chain(network: str, datadir: str | Path, *, short: bool = False) -> str:
    """Say how big the chain already on this machine is.

    Args:
        network: Which network to look at.
        datadir: Where its data directory is.
        short: Return a single clause that fits on the end of a menu line,
            instead of a couple of full sentences.
    """
    summary = inspect_database(NodeConfig(network=network, datadir=Path(datadir)).chain_path)
    if not summary["exists"]:
        if short:
            return "nothing stored here yet, so the whole chain has to be downloaded"
        return (
            f"There is no {network} chain here yet, so a new node starts from the\n"
            "genesis block and downloads everything. Expect that to take a while."
        )
    if short:
        pruned = " (pruned)" if summary["prune_height"] else ""
        return (
            f"already at height {summary['height']},"
            f" {format_bytes(summary['disk_bytes'])} on disk{pruned}"
        )
    lines = [
        f"The {network} chain here is at height {summary['height']}:"
        f" {format_bytes(summary['chain_bytes'] or 0)} of blocks,"
        f" {format_bytes(summary['disk_bytes'])} on disk."
    ]
    if summary["prune_height"]:
        lines.append(f"It is pruned: block bodies up to height {summary['prune_height']} are gone.")
    return "\n".join(lines)


def _answers(connection: NodeConnection, *, timeout: float = 5.0) -> bool:
    """Whether a node answers at ``connection``."""
    try:
        connection.client(timeout=timeout).getinfo()
    except RpcClientError:
        return False
    return True


def _local_connection(network: str, datadir: str | Path) -> NodeConnection:
    """The node on this machine, with whatever token it wrote."""
    return NodeConnection(local_url(network), read_rpc_token(datadir, network) or "")


# ------------------------------------------------------------------- interaction


def _prompt(question: str, default: str = "") -> str:
    """Ask a question, returning ``default`` when there is nobody to ask."""
    try:
        answer = input(question).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer or default


def _confirm(question: str, *, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = _prompt(f"{question} {suffix} ", "y" if default else "n").lower()
    return answer in ("y", "yes")


def _interactive() -> bool:
    """Whether there is a person on the other end of this terminal."""
    return sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()


# ---------------------------------------------------------------- starting a node


def _prune_and_public_options(network: str, datadir: Path) -> list[str]:
    """Ask the questions that change what a new node will be, and be honest.

    Both answers matter enough to be worth a prompt: pruning cannot be undone,
    and making a node public exposes it to the internet.
    """
    extra: list[str] = []
    print()
    print(describe_local_chain(network, datadir))
    print()
    if _confirm(
        f"Save disk space by keeping only the last {MIN_PRUNE_KEEP} blocks whole?\n"
        "  (balances stay exact; old blocks can no longer be shown or served)",
        default=False,
    ):
        keep = _prompt(f"  how many blocks to keep [{MIN_PRUNE_KEEP}]: ", str(MIN_PRUNE_KEEP))
        try:
            blocks = max(MIN_PRUNE_KEEP, int(keep))
        except ValueError:
            blocks = MIN_PRUNE_KEEP
        extra += ["--prune", str(blocks)]
    if _confirm(
        "Let other people's wallets and miners use this node?\n"
        "  (--rpc-public: read-only calls need no token; mining still does)",
        default=False,
    ):
        extra.append("--rpc-public")
        if _confirm("  hand out mining work to strangers as well?", default=False):
            extra.append("--rpc-public-mining")
        advertise = _prompt("  address other people should use for it (blank to skip): ")
        if advertise:
            extra += ["--rpc-advertise", advertise]
    return extra


def _start_local(
    network: str,
    datadir: Path,
    *,
    ask: bool,
) -> NodeConnection:
    """Start a node here and leave it running after this command finishes."""
    extra = _prune_and_public_options(network, datadir) if ask else []
    print(f"\nstarting a {network} node...", flush=True)
    try:
        node = LocalNode.launch(network=network, datadir=datadir, extra=extra)
    except LocalNodeError as exc:
        raise NodeChoiceError(str(exc)) from exc
    if not node.wait_until_ready():
        node.stop()
        raise NodeChoiceError(f"the node did not answer in time:\n\n{node.tail_log()}")
    print(f"node running at {node.url}; it keeps running after this command ends")
    print(f"log: {node.log_path}")
    print("stop it with:  scarlet-node rpc stop")
    return NodeConnection(node.url, node.token)


def _local_instructions(network: str, datadir: Path) -> str:
    command = node_command(
        network=network,
        datadir=datadir,
        rpc_port=NodeConfig(network=network, datadir=datadir).resolved_rpc_port(),
        rpc_token="<token>",
    )
    readable = " ".join(part for part in command if part != "<token>" and part != "--rpc-token")
    return f"no node is running on this machine. Start one with:\n\n    {readable}\n"


def _use_local(
    args: argparse.Namespace,
    *,
    ask: bool,
) -> NodeConnection:
    """Use, and if necessary start, a node on this machine."""
    network, datadir = args.network, Path(args.datadir)
    connection = _local_connection(network, datadir)
    if _answers(connection):
        return connection
    allowed = getattr(args, "start_node", False) or (
        ask and not getattr(args, "no_start_node", False) and _interactive()
    )
    if not allowed:
        raise NodeChoiceError(_local_instructions(network, datadir))
    if ask and not getattr(args, "start_node", False):
        print(f"\nNo {network} node is running on this machine.")
        if not _confirm("Start one now?", default=True):
            raise NodeChoiceError(_local_instructions(network, datadir))
    return _start_local(network, datadir, ask=ask and not getattr(args, "start_node", False))


# ------------------------------------------------------------------ public nodes


def _print_public_nodes(statuses: list[directory.NodeStatus], network: str) -> None:
    width = max((len(status.node.label) for status in statuses), default=10)
    for number, status in enumerate(statuses, start=1):
        mark = " " if status.usable(network) else "!"
        print(f"  {number:>2}){mark} {status.node.label:<{width}}  {status.describe()}")


def _use_public(
    args: argparse.Namespace,
    *,
    ask: bool,
    for_mining: bool = False,
) -> NodeConnection:
    """Find a public node, offering the list when somebody is watching."""
    network, datadir = args.network, Path(args.datadir)
    print(f"looking for public {network} nodes...", flush=True)
    statuses = directory.discover(network, datadir)
    usable = [status for status in statuses if status.usable(network)]
    if not usable:
        raise NodeChoiceError(
            f"no public {network} node answered. Add one with --node <URL>,"
            " or run a node here with --node local."
        )
    if for_mining and not any(status.serves_mining for status in usable):
        print(
            "\nnote: none of these public nodes hands out mining work without a token,"
            "\n      so mining needs a node of your own (--node local)."
        )
    if not ask or not _interactive():
        best = usable[0]
        print(f"using {best.url} ({best.describe()})")
        return NodeConnection(best.url)

    print(f"\nPublic {network} nodes:")
    _print_public_nodes(statuses, network)
    print("   0)  enter a different address")
    while True:
        answer = _prompt("\nWhich one? [1] ", "1")
        if answer == "0":
            typed = directory.normalise_url(_prompt("Node URL: "))
            if not typed:
                continue
            directory.remember_node(datadir, network, typed)
            return NodeConnection(typed)
        try:
            chosen = statuses[int(answer) - 1]
        except (ValueError, IndexError):
            print("pick one of the numbers above")
            continue
        if not chosen.usable(network):
            print(f"that one is not usable: {chosen.describe()}")
            continue
        directory.remember_node(datadir, network, chosen.url)
        return NodeConnection(chosen.url)


# ------------------------------------------------------------------- the question


def _ask_which_node(args: argparse.Namespace, *, for_mining: bool) -> NodeConnection:
    """Offer the choice between a node here and a public one."""
    network, datadir = args.network, Path(args.datadir)
    print(f"\nScarletCoin needs a {network} node to talk to.\n")
    print("  1) Run a node on this machine")
    print("     validates everything itself, needs disk space and time to catch up")
    print(f"     {describe_local_chain(network, datadir, short=True)}")
    print("  2) Connect to a public node")
    print("     ready at once, and you trust somebody else's copy of the chain")
    if for_mining:
        print("     mining this way only works if that node hands out work")
    print("  3) Enter a node address yourself")
    while True:
        answer = _prompt("\nChoice [1/2/3]: ", "1")
        if answer == "1":
            return _use_local(args, ask=True)
        if answer == "2":
            return _use_public(args, ask=True, for_mining=for_mining)
        if answer == "3":
            typed = directory.normalise_url(_prompt("Node URL: "))
            if not typed:
                continue
            token = _prompt("RPC token (blank for a public node): ")
            if directory.normalise_url(typed) != local_url(network):
                directory.remember_node(datadir, network, typed)
            return NodeConnection(typed, token)
        print("answer 1, 2 or 3")


# ---------------------------------------------------------------------- resolving


def resolve_connection(
    args: argparse.Namespace,
    *,
    for_mining: bool = False,
) -> NodeConnection:
    """Work out which node to use, asking only when there is no answer already.

    Order of precedence: ``--rpc-url``, then ``--node``, then the node chosen last
    time, then a node already running on this machine, then — if somebody is
    watching — the question. Without a terminal the fallback is silent: local
    first, then the best public node, so a service or a cron job keeps working.

    Args:
        args: Parsed arguments carrying ``network``, ``datadir`` and the options
            added by :func:`scarletcoin.cli_common.add_node_choice_arguments`.
        for_mining: Warn when the chosen node will not hand out mining work.

    Raises:
        NodeChoiceError: if there is no usable node and none could be started.
    """
    network, datadir = args.network, Path(args.datadir)
    choice = (getattr(args, "node", None) or "").strip()

    if getattr(args, "forget_node", False):
        forget_connection(datadir, network)

    if getattr(args, "rpc_url", None):
        token = getattr(args, "rpc_token", None) or ""
        connection = NodeConnection(str(args.rpc_url).rstrip("/"), token)
        if not token and connection.is_local(network):
            connection.token = read_rpc_token(datadir, network) or ""
        return connection

    if choice and choice.lower() not in _KEYWORDS:
        url = directory.normalise_url(choice)
        if not url:
            raise NodeChoiceError(f"{choice!r} is not a node address or one of {_KEYWORDS}")
        connection = NodeConnection(url, getattr(args, "rpc_token", None) or "")
        if connection.is_local(network) and not connection.token:
            connection.token = read_rpc_token(datadir, network) or ""
        _remember(args, connection)
        return connection

    keyword = choice.lower()
    if keyword == "local":
        return _remember(args, _use_local(args, ask=_interactive()))
    if keyword == "public":
        return _remember(args, _use_public(args, ask=_interactive(), for_mining=for_mining))
    if keyword == "ask":
        return _remember(args, _ask_which_node(args, for_mining=for_mining))

    saved = load_connection(datadir, network)
    if saved is not None:
        if saved.is_local(network):
            saved.token = read_rpc_token(datadir, network) or saved.token
        if _answers(saved):
            return saved

    running = _local_connection(network, datadir)
    if _answers(running, timeout=3.0):
        return _remember(args, running)

    if keyword == "auto" or not _interactive():
        try:
            return _remember(args, _use_local(args, ask=False))
        except NodeChoiceError:
            return _remember(args, _use_public(args, ask=False, for_mining=for_mining))
    return _remember(args, _ask_which_node(args, for_mining=for_mining))


def _remember(args: argparse.Namespace, connection: NodeConnection) -> NodeConnection:
    save_connection(Path(args.datadir), args.network, connection)
    return connection


def resolve_client(args: argparse.Namespace, *, for_mining: bool = False) -> RpcClient:
    """Return a client for whichever node :func:`resolve_connection` settles on."""
    connection = resolve_connection(args, for_mining=for_mining)
    try:
        parsed = parse_proxy(getattr(args, "proxy", None))
    except ValueError as exc:
        die(str(exc))
    if parsed is not None:
        connection.proxy_host, connection.proxy_port = parsed
    return connection.client(timeout=getattr(args, "timeout", 30.0))
