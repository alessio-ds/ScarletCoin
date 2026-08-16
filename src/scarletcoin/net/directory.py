"""Finding a public node to talk to.

A wallet or a miner needs *a* node, not necessarily its own. A node started with
``--rpc-public`` answers the read-only and broadcast methods for anybody, so a
newcomer can be useful in seconds instead of waiting for a chain to download.

The problem is knowing which public nodes exist. This module solves it in three
steps, cheapest first:

1. the list built into the release (:attr:`ChainParams.public_nodes`), plus
   anything the user has added by hand or through ``SCARLETCOIN_PUBLIC_NODES``;
2. every candidate is probed in parallel — reachable, right network, how far
   along, how fast to answer;
3. whichever ones answer are asked for the public nodes *they* know
   (``getpublicnodes``), and the new names are probed too.

So a new public node only has to be known to one existing node to become
findable by everybody, and a release that ships with a stale list still works as
long as one entry survives.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from scarletcoin.core.params import get_params
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.units import format_bytes

__all__ = [
    "NodeStatus",
    "PublicNode",
    "candidates",
    "discover",
    "forget_node",
    "normalise_url",
    "probe",
    "probe_all",
    "remember_node",
    "user_nodes",
]

logger = logging.getLogger(__name__)

#: File under ``<datadir>/<network>/`` holding public nodes the user added.
NODES_FILENAME = "public-nodes.json"

#: How long a single probe may take.
PROBE_TIMEOUT = 6.0

#: How many nodes to probe at once.  Probing is all waiting, so this can be well
#: above the core count.
PROBE_WORKERS = 8

#: Most candidates to probe in one go, so a hostile ``getpublicnodes`` answer
#: cannot turn a wallet into a port scanner.
MAX_CANDIDATES = 24

#: A host name or IPv4 address.  Deliberately strict: whatever comes out of this
#: module is handed to :mod:`urllib`, and a "host" with a space or a control
#: character in it makes it raise deep inside the HTTP client instead of being
#: reported as the typo it is.
_HOSTNAME = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$")


def normalise_url(text: str) -> str:
    """Turn whatever somebody typed into a base URL, or return ``""``.

    ``scarletcoin.example.net`` becomes ``https://scarletcoin.example.net``;
    a bare address with a port becomes plain HTTP, because that is what a node on
    a local network serves. Paths, query strings and fragments are dropped: this
    is a base URL, and the client appends ``/rpc`` itself.
    """
    cleaned = str(text).strip()
    if not cleaned or any(character.isspace() for character in cleaned):
        return ""
    if "://" not in cleaned:
        # A host with an explicit port is almost always somebody's own node on a
        # private network, where there is no certificate to be had.
        host = cleaned.split("/")[0]
        tail = host.rpartition("]")[2] if host.startswith("[") else host
        cleaned = f"{'http' if ':' in tail else 'https'}://{cleaned}"
    try:
        parsed = urlparse(cleaned)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not hostname:
        return ""
    if not hostname.startswith("[") and ":" not in hostname and not _HOSTNAME.match(hostname):
        return ""
    netloc = hostname if port is None else f"{hostname}:{port}"
    if ":" in hostname:  # IPv6, which urlparse hands back without its brackets
        netloc = f"[{hostname}]" if port is None else f"[{hostname}]:{port}"
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


@dataclass(frozen=True)
class PublicNode:
    """A public node we might use, and where we heard about it."""

    url: str
    source: str = "built-in"
    """``built-in``, ``saved``, ``environment``, ``discovered`` or ``typed``."""

    @property
    def host(self) -> str:
        """Host name of the node."""
        return urlparse(self.url).hostname or self.url

    @property
    def label(self) -> str:
        """Host and port, which is what tells two nodes on one machine apart."""
        return urlparse(self.url).netloc or self.url


@dataclass
class NodeStatus:
    """What a probe found out about one node."""

    node: PublicNode
    reachable: bool = False
    network: str | None = None
    height: int | None = None
    peers: int | None = None
    chain_bytes: int | None = None
    latency: float | None = None
    """Seconds the ``getinfo`` call took."""
    needs_token: bool = False
    """The node answered, but demands a token: it is not a public node."""
    serves_mining: bool = False
    """The node hands out mining work without a token."""
    version: str = ""
    error: str = ""

    @property
    def url(self) -> str:
        """The node's base URL."""
        return self.node.url

    def usable(self, network: str, *, for_mining: bool = False) -> bool:
        """Whether this node can actually serve a client on ``network``."""
        if not self.reachable or self.network != network:
            return False
        return self.serves_mining if for_mining else True

    def describe(self) -> str:
        """One line summarising the node, for a list a person reads."""
        if self.needs_token:
            return "private: this node wants a token"
        if not self.reachable:
            return self.error or "no answer"
        parts = [f"height {self.height}"]
        if self.peers is not None:
            parts.append(f"{self.peers} peers")
        if self.chain_bytes is not None:
            parts.append(format_bytes(self.chain_bytes))
        if self.latency is not None:
            parts.append(f"{self.latency * 1000:.0f} ms")
        if self.serves_mining:
            parts.append("hands out work")
        return "  ·  ".join(parts)

    def sort_key(self, network: str) -> tuple:
        """Best first: usable, then furthest along, then quickest to answer."""
        return (
            0 if self.usable(network) else 1,
            -(self.height or 0),
            self.latency if self.latency is not None else 999.0,
        )


# ------------------------------------------------------------------- candidates


def nodes_path(datadir: str | Path, network: str) -> Path:
    """Where this machine keeps the public nodes its owner added."""
    return Path(datadir) / network / NODES_FILENAME


def user_nodes(datadir: str | Path | None, network: str) -> list[PublicNode]:
    """Public nodes the user saved, newest first, plus any from the environment."""
    found: list[PublicNode] = []
    for text in (os.environ.get("SCARLETCOIN_PUBLIC_NODES") or "").split(","):
        url = normalise_url(text)
        if url:
            found.append(PublicNode(url, "environment"))
    if datadir is not None:
        try:
            data = json.loads(nodes_path(datadir, network).read_text("utf-8"))
        except (OSError, ValueError):
            data = []
        if isinstance(data, list):
            for item in data:
                url = normalise_url(item if isinstance(item, str) else "")
                if url:
                    found.append(PublicNode(url, "saved"))
    return found


def remember_node(datadir: str | Path, network: str, url: str) -> None:
    """Save a public node so it is offered first next time."""
    cleaned = normalise_url(url)
    if not cleaned:
        return
    target = nodes_path(datadir, network)
    existing = [node.url for node in user_nodes(datadir, network) if node.source == "saved"]
    ordered = [cleaned] + [item for item in existing if item != cleaned]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(ordered[:20], indent=1), "utf-8")
    except OSError as exc:  # pragma: no cover - disk errors
        logger.warning("could not save the public node list: %s", exc)


def forget_node(datadir: str | Path, network: str, url: str) -> None:
    """Drop a saved public node."""
    cleaned = normalise_url(url)
    remaining = [
        node.url
        for node in user_nodes(datadir, network)
        if node.source == "saved" and node.url != cleaned
    ]
    try:
        nodes_path(datadir, network).write_text(json.dumps(remaining, indent=1), "utf-8")
    except OSError as exc:  # pragma: no cover - disk errors
        logger.warning("could not save the public node list: %s", exc)


def candidates(
    network: str,
    datadir: str | Path | None = None,
    *,
    extra: tuple[str, ...] = (),
) -> list[PublicNode]:
    """Every public node worth trying, the user's own first."""
    ordered: list[PublicNode] = [
        PublicNode(url, "typed") for url in map(normalise_url, extra) if url
    ]
    ordered += user_nodes(datadir, network)
    ordered += [
        PublicNode(url, "built-in")
        for url in map(normalise_url, get_params(network).public_nodes)
        if url
    ]
    return _unique(ordered)


def _unique(nodes: list[PublicNode]) -> list[PublicNode]:
    seen: set[str] = set()
    result: list[PublicNode] = []
    for node in nodes:
        if node.url not in seen:
            seen.add(node.url)
            result.append(node)
    return result


# ----------------------------------------------------------------------- probing


def probe(node: PublicNode, *, timeout: float = PROBE_TIMEOUT) -> NodeStatus:
    """Ask one node who it is.  Never raises."""
    status = NodeStatus(node)
    client = RpcClient(node.url, timeout=timeout)
    started = time.monotonic()
    try:
        info = client.getinfo()
    except RpcClientError as exc:
        status.error = str(exc)
        status.needs_token = exc.code in (401, -32001)
        return status
    except Exception as exc:  # pragma: no cover - defensive: URL parsing, DNS
        status.error = str(exc)
        return status
    status.latency = time.monotonic() - started
    if not isinstance(info, dict):  # pragma: no cover - a node always answers a dict
        status.error = "that is not a ScarletCoin node"
        return status
    status.reachable = True
    status.network = info.get("network")
    status.height = info.get("height")
    status.peers = info.get("peers")
    status.chain_bytes = info.get("chain_bytes")
    status.serves_mining = bool(info.get("public_mining"))
    status.version = str(info.get("version") or "")
    return status


def _ask_for_more(status: NodeStatus, *, timeout: float) -> list[PublicNode]:
    """Ask a node that answered which other public nodes it knows."""
    try:
        answer = RpcClient(status.url, timeout=timeout).call("getpublicnodes")
    except Exception:  # an older node simply does not have the method
        return []
    if not isinstance(answer, list):
        return []
    return [
        PublicNode(url, "discovered")
        for url in (normalise_url(item) for item in answer if isinstance(item, str))
        if url
    ]


def probe_all(
    nodes: list[PublicNode],
    *,
    timeout: float = PROBE_TIMEOUT,
    workers: int = PROBE_WORKERS,
) -> list[NodeStatus]:
    """Probe every node at once and return the results in the order given."""
    if not nodes:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(nodes))) as pool:
        return list(pool.map(lambda node: probe(node, timeout=timeout), nodes))


def discover(
    network: str,
    datadir: str | Path | None = None,
    *,
    extra: tuple[str, ...] = (),
    timeout: float = PROBE_TIMEOUT,
    follow: bool = True,
) -> list[NodeStatus]:
    """Return the public nodes for ``network``, best first.

    Args:
        network: Which network the client is on; nodes on another one are
            reported but sorted to the bottom.
        datadir: Where to look for nodes the user saved.
        extra: URLs to try before anything else.
        timeout: Per-probe timeout.
        follow: Ask the nodes that answered for the ones they know. Turn this off
            for a quick refresh of a list already on screen.
    """
    first = candidates(network, datadir, extra=extra)[:MAX_CANDIDATES]
    results = probe_all(first, timeout=timeout)
    if follow:
        known = {status.url for status in results}
        more: list[PublicNode] = []
        for status in results:
            if not status.usable(network):
                continue
            for found in _ask_for_more(status, timeout=timeout):
                if found.url not in known:
                    known.add(found.url)
                    more.append(found)
        room = MAX_CANDIDATES - len(results)
        if more and room > 0:
            results += probe_all(more[:room], timeout=timeout)
    results.sort(key=lambda status: status.sort_key(network))
    return results
