"""The ScarletCoin node.

A node keeps the blockchain, relays blocks and transactions, and answers RPC
calls.  It runs a handful of daemon threads:

* one listener accepting inbound connections;
* one reader per connected peer;
* a connector that keeps the outbound slot count topped up from the address book;
* a maintenance thread for pings, timeouts, orphan expiry and periodic saves.

All chain mutation goes through :meth:`Node.submit_block` and
:meth:`Node.submit_transaction`, so the peer-to-peer code, the RPC interface and
the tests share exactly the same acceptance path.
"""

from __future__ import annotations

import contextlib
import logging
import random
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from scarletcoin import __version__
from scarletcoin.core.block import Block
from scarletcoin.core.chain import AddBlockResult, Blockchain, BlockStatus
from scarletcoin.core.mempool import Mempool, MempoolEntry
from scarletcoin.core.params import ChainParams, get_params
from scarletcoin.core.storage import Storage
from scarletcoin.core.transaction import Transaction
from scarletcoin.core.validation import MissingInputError, ValidationError
from scarletcoin.net import protocol
from scarletcoin.net.addrbook import AddressBook, parse_address
from scarletcoin.net.peer import Peer, PeerDisconnected, connect_to
from scarletcoin.net.protocol import InvItem, InvType, ProtocolError

__all__ = ["Node", "NodeConfig"]

logger = logging.getLogger(__name__)

#: Score at which a peer is disconnected and banned.
BAN_THRESHOLD = 100
#: Blocks requested from a single peer at a time.
BLOCKS_IN_FLIGHT = 64
#: Orphan blocks kept while waiting for their parents.
MAX_ORPHANS = 200
#: Drop a peer that has not sent anything for this long.
PEER_TIMEOUT = 180.0
#: Ping a quiet peer after this long.
PING_INTERVAL = 60.0
#: How often the background threads wake up to look for work.
_TICK = 0.25
_CONNECT_INTERVAL = 1.0
_MAINTENANCE_INTERVAL = 5.0
#: Never re-ask every peer for blocks more often than this.
_POLL_INTERVAL = 60.0


@dataclass
class NodeConfig:
    """Everything needed to start a :class:`Node`."""

    network: str = "mainnet"
    datadir: Path = field(default_factory=lambda: Path.home() / ".scarletcoin")
    listen: bool = True
    p2p_host: str = "0.0.0.0"
    p2p_port: int | None = None
    rpc: bool = True
    rpc_host: str = "127.0.0.1"
    rpc_port: int | None = None
    rpc_token: str | None = None
    max_outbound: int = 8
    max_inbound: int = 64
    connect: tuple[str, ...] = ()
    """Peers to connect to on start, in addition to the address book."""
    seeds: tuple[str, ...] = ()
    """Extra seed hosts, resolved through DNS, on top of the network's own."""
    use_seeds: bool = True
    user_agent: str = f"/scarletcoin:{__version__}/"

    @property
    def params(self) -> ChainParams:
        """Chain parameters for the configured network."""
        return get_params(self.network)

    @property
    def network_dir(self) -> Path:
        """Directory holding this network's state."""
        return Path(self.datadir) / self.network

    @property
    def chain_path(self) -> Path:
        """Path of the chain database."""
        return self.network_dir / "chain.sqlite3"

    @property
    def peers_path(self) -> Path:
        """Path of the address book."""
        return self.network_dir / "peers.json"

    def resolved_p2p_port(self) -> int:
        """The port to listen on (0 means "pick any free port")."""
        return self.params.default_p2p_port if self.p2p_port is None else self.p2p_port

    def resolved_rpc_port(self) -> int:
        """The RPC port (0 means "pick any free port")."""
        return self.params.default_rpc_port if self.rpc_port is None else self.rpc_port


class Node:
    """A full node: chain, mempool and peer-to-peer networking."""

    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.params = config.params
        self.storage = Storage(config.chain_path)
        self.chain = Blockchain(self.storage, self.params)
        self.mempool = Mempool(self.chain, self.params)
        self.chain.add_listener(self.mempool)
        self.addrbook = AddressBook(config.peers_path)

        self._lock = threading.RLock()
        self._peers: dict[int, Peer] = {}
        self._orphans: OrderedDict[bytes, tuple[Block, float]] = OrderedDict()
        self._nonce = random.getrandbits(64)
        self._local_addresses: set[tuple[str, int]] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._listen_socket: socket.socket | None = None
        self.started_at = time.time()
        self._last_block_at = time.time()
        self._last_poll_at = 0.0

        for peer_address in config.connect:
            self._add_address(peer_address, source="config")

    @property
    def seed_hosts(self) -> tuple[str, ...]:
        """Every seed this node may bootstrap from."""
        if not self.config.use_seeds:
            return tuple(self.config.seeds)
        return tuple(self.params.seeds) + tuple(self.config.seeds)

    def _add_address(self, text: str, *, source: str) -> None:
        try:
            host, port = parse_address(text, self.params.default_p2p_port)
        except ValueError as exc:
            logger.warning("ignoring peer address %r: %s", text, exc)
            return
        if (host, port) in self._local_addresses:
            return
        self.addrbook.add(host, port, source=source)

    def _note_local_address(self, host: str, port: int) -> None:
        """Remember that an address is this very node, and stop dialling it.

        Without this, the node that *is* the network's seed would dial its own
        published name forever: the connection is accepted, recognised as itself
        by the handshake nonce, dropped, and immediately retried.
        """
        if (host, port) in self._local_addresses:
            return
        self._local_addresses.add((host, port))
        self.addrbook.forget(host, port)
        logger.info("%s:%d is this node; it will not be dialled again", host, port)

    def _bootstrap_seeds(self) -> None:
        """Add the seed hosts and every address their names resolve to.

        A seed is published as a host name, so one name can point at several
        machines: each of its A and AAAA records becomes a candidate peer. The
        name itself is kept too, which keeps working when the records change.
        """
        for seed in self.seed_hosts:
            try:
                host, port = parse_address(seed, self.params.default_p2p_port)
            except ValueError as exc:
                logger.warning("ignoring seed %r: %s", seed, exc)
                continue
            if (host, port) in self._local_addresses:
                continue  # this node is that seed
            self.addrbook.add(host, port, source="seed")
            try:
                resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                logger.warning("cannot resolve seed %s: %s", host, exc)
                continue
            addresses = {info[4][0] for info in resolved}
            for address in addresses:
                if (address, port) not in self._local_addresses:
                    self.addrbook.add(address, port, source="dns")
            logger.info("seed %s resolved to %d address(es)", host, len(addresses))

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the listener and the background threads."""
        logger.info(
            "starting %s node at height %d (%s)",
            self.params.name,
            self.chain.height,
            self.config.network_dir,
        )
        if self.config.listen:
            self._start_listener()
        if self.seed_hosts:
            # DNS can be slow, so never block start-up on it.
            self._spawn("seeds", self._bootstrap_seeds)
        self._spawn("connector", self._connect_loop)
        self._spawn("maintenance", self._maintenance_loop)

    def stop(self) -> None:
        """Stop every thread, disconnect peers and close the database."""
        if self._stop.is_set():
            return
        logger.info("shutting down")
        self._stop.set()
        if self._listen_socket is not None:
            with contextlib.suppress(OSError):
                self._listen_socket.close()
        for peer in self.peers:
            peer.close()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self.addrbook.save()
        self.storage.close()

    def __enter__(self) -> Node:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def wait(self) -> None:
        """Block until :meth:`stop` is called."""
        try:
            while not self._stop.wait(1.0):
                pass
        except KeyboardInterrupt:  # pragma: no cover - interactive use
            self.stop()

    def _spawn(self, name: str, target) -> threading.Thread:
        thread = threading.Thread(target=target, name=f"scarlet-{name}", daemon=True)
        thread.start()
        self._threads.append(thread)
        return thread

    @property
    def stopping(self) -> bool:
        """``True`` once shutdown has begun."""
        return self._stop.is_set()

    # -------------------------------------------------------------------- peers

    @property
    def peers(self) -> list[Peer]:
        """Currently connected peers."""
        with self._lock:
            return list(self._peers.values())

    @property
    def p2p_port(self) -> int:
        """The port the node actually listens on (0 means "not listening")."""
        if self._listen_socket is None:
            return 0
        return self._listen_socket.getsockname()[1]

    def _start_listener(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.config.p2p_host, self.config.resolved_p2p_port()))
        except OSError as exc:
            sock.close()
            raise RuntimeError(
                f"cannot listen on {self.config.p2p_host}:{self.config.resolved_p2p_port()}: {exc}"
            ) from exc
        sock.listen(32)
        # A timeout lets the accept loop notice shutdown: closing a socket does
        # not reliably interrupt a thread already blocked in accept().
        sock.settimeout(_TICK)
        self._listen_socket = sock
        logger.info("listening for peers on %s", sock.getsockname()[1])
        self._spawn("listener", self._accept_loop)

    def _accept_loop(self) -> None:
        assert self._listen_socket is not None
        while not self.stopping:
            try:
                client, address = self._listen_socket.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self.stopping:  # pragma: no cover - transient accept errors
                    logger.debug("accept failed", exc_info=True)
                return
            if self.addrbook.is_banned(address[0]):
                client.close()
                continue
            inbound = sum(1 for peer in self.peers if peer.inbound)
            if inbound >= self.config.max_inbound:
                client.close()
                continue
            client.settimeout(None)
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = Peer(client, address, magic=self.params.magic, inbound=True)
            self._register(peer)

    def _connect_loop(self) -> None:
        """Keep the outbound peer slots filled from the address book."""
        while not self.stopping:
            outbound = [peer for peer in self.peers if not peer.inbound]
            if len(outbound) >= self.config.max_outbound:
                if self._stop.wait(_CONNECT_INTERVAL):
                    return
                continue
            busy = {(peer.host, peer.port) for peer in self.peers}
            busy.update(peer.advertised_address for peer in self.peers if peer.advertised_address)
            busy.update(self._local_addresses)
            candidates = self.addrbook.candidates(busy)
            if candidates:
                entry = candidates[0]
                self.connect_peer(entry.host, entry.port)
            if self._stop.wait(_CONNECT_INTERVAL):
                return

    def connect_peer(self, host: str, port: int) -> bool:
        """Open an outbound connection and start its reader thread."""
        if self.addrbook.is_banned(host):
            return False
        if self._already_connected(host, port):
            return False
        try:
            peer = connect_to(host, port, magic=self.params.magic)
        except PeerDisconnected as exc:
            logger.debug("%s", exc)
            self.addrbook.mark_failure(host, port)
            return False
        self.addrbook.mark_success(host, port)
        self._register(peer)
        return True

    def _already_connected(self, host: str, port: int) -> bool:
        """Whether a peer to ``host:port`` is already connected.

        Host names are resolved so that ``localhost`` and ``127.0.0.1`` count as
        the same address: a node reachable under two names must not take two
        outbound slots.  The handshake also drops true duplicates by nonce, but
        refusing the redundant dial here avoids racing a second connection that
        a slow node might otherwise never get told to close.
        """
        return any(self._addresses_match(peer.host, peer.port, host, port) for peer in self.peers)

    @staticmethod
    def _addresses_match(a_host: str, a_port: int, b_host: str, b_port: int) -> bool:
        """Whether two addresses name the same socket, resolving names when needed."""
        if a_port != b_port:
            return False
        if a_host == b_host:
            return True
        try:
            return socket.gethostbyname(a_host) == socket.gethostbyname(b_host)
        except OSError:
            return False

    def _register(self, peer: Peer) -> None:
        with self._lock:
            self._peers[peer.id] = peer
        logger.info("%s connected", peer)
        self._spawn(f"peer{peer.id}", lambda: self._peer_loop(peer))

    def _unregister(self, peer: Peer) -> None:
        with self._lock:
            self._peers.pop(peer.id, None)
        peer.close()
        logger.info("%s disconnected", peer)

    def _peer_loop(self, peer: Peer) -> None:
        try:
            if not peer.inbound:
                self._send_version(peer)
            while not self.stopping and not peer.closed:
                message = peer.receive()
                if message is None:
                    continue
                self._handle_message(peer, message)
        except PeerDisconnected as exc:
            logger.debug("%s", exc)
        except ProtocolError as exc:
            logger.info("%s misbehaved: %s", peer, exc)
            self._misbehave(peer, BAN_THRESHOLD)
        except Exception:  # pragma: no cover - defensive
            logger.exception("%s: unexpected error", peer)
        finally:
            self._unregister(peer)

    def _send_version(self, peer: Peer) -> None:
        peer.send(
            protocol.Version(
                version=protocol.PROTOCOL_VERSION,
                user_agent=self.config.user_agent,
                start_height=self.chain.height,
                nonce=self._nonce,
                listen_port=self.p2p_port,
                timestamp=int(time.time()),
            )
        )

    def _misbehave(self, peer: Peer, points: int, reason: str = "") -> None:
        peer.misbehaviour += points
        if reason:
            logger.debug("%s misbehaviour +%d: %s", peer, points, reason)
        if peer.misbehaviour >= BAN_THRESHOLD:
            logger.info("banning %s", peer.host)
            self.addrbook.ban(peer.host)
            peer.close()

    # ---------------------------------------------------------- message handling

    def _handle_message(self, peer: Peer, message: protocol.Message) -> None:
        if isinstance(message, protocol.Version):
            self._on_version(peer, message)
            return
        if isinstance(message, protocol.VerAck):
            if peer.version is None:
                raise ProtocolError("verack before version")
            self._complete_handshake(peer)
            return
        if peer.version is None:
            raise ProtocolError(f"{message.command} before the handshake")

        if isinstance(message, protocol.Ping):
            peer.send(protocol.Pong(message.nonce))
        elif isinstance(message, protocol.Pong):
            if peer.last_ping_nonce == message.nonce and peer.last_ping_sent is not None:
                peer.latency = time.time() - peer.last_ping_sent
                peer.last_ping_nonce = None
        elif isinstance(message, protocol.GetAddr):
            self._on_getaddr(peer)
        elif isinstance(message, protocol.Addr):
            self._on_addr(message)
        elif isinstance(message, protocol.Inv):
            self._on_inv(peer, message)
        elif isinstance(message, protocol.GetData):
            self._on_getdata(peer, message)
        elif isinstance(message, protocol.NotFound):
            for item in message.items:
                peer.requested_blocks.discard(item.hash)
        elif isinstance(message, protocol.GetBlocks):
            self._on_getblocks(peer, message)
        elif isinstance(message, protocol.BlockMessage):
            self._on_block(peer, message.block)
        elif isinstance(message, protocol.TxMessage):
            self._on_tx(peer, message.transaction)
        elif isinstance(message, protocol.Mempool):
            self._announce_mempool(peer)

    def _on_version(self, peer: Peer, message: protocol.Version) -> None:
        if peer.version is not None:
            raise ProtocolError("duplicate version message")
        if message.nonce == self._nonce:
            # Both ends of a self-connection land here: remember the address so
            # the connector never tries it again.
            if not peer.inbound:
                self._note_local_address(peer.host, peer.port)
            if message.listen_port:
                self._note_local_address(peer.host, message.listen_port)
            logger.debug("%s is ourselves, dropping", peer)
            peer.close()
            return
        twin = next(
            (other for other in self.peers if other is not peer and other.nonce == message.nonce),
            None,
        )
        if twin is not None:
            # The same node reachable under two names or addresses: one link is enough.
            logger.debug("%s is already connected as %s, dropping", peer, twin)
            peer.close()
            return
        peer.nonce = message.nonce
        peer.version = message.version
        peer.user_agent = message.user_agent
        peer.start_height = message.start_height
        peer.listen_port = message.listen_port or None
        if peer.inbound:
            self._send_version(peer)
        peer.send(protocol.VerAck())
        if peer.advertised_address is not None:
            host, port = peer.advertised_address
            self.addrbook.add(host, port, source="handshake")

    def _complete_handshake(self, peer: Peer) -> None:
        if peer.handshake_done.is_set():
            return
        peer.handshake_done.set()
        logger.info(
            "%s handshake complete: %s at height %d",
            peer,
            peer.user_agent or "unknown client",
            peer.start_height,
        )
        peer.send(protocol.GetAddr())
        self._maybe_sync(peer)
        if self.chain.height >= peer.start_height and len(self.mempool):
            self._announce_mempool(peer)

    def _on_getaddr(self, peer: Peer) -> None:
        addresses = [
            protocol.NetworkAddress(entry.host, entry.port, entry.last_seen)
            for entry in self.addrbook.sample(protocol.MAX_ADDR_ITEMS // 4)
        ]
        if addresses:
            peer.send(protocol.Addr(tuple(addresses)))

    def _on_addr(self, message: protocol.Addr) -> None:
        for address in message.addresses:
            self.addrbook.add(address.host, address.port, last_seen=address.last_seen)

    def _on_inv(self, peer: Peer, message: protocol.Inv) -> None:
        wanted: list[InvItem] = []
        for item in message.items:
            peer.note_inventory(item.hash)
            if item.is_block:
                if self.chain.has_block(item.hash) or item.hash in self._orphans:
                    continue
                if item.hash in peer.requested_blocks or item.hash in peer.pending_blocks:
                    continue
                peer.pending_blocks.append(item.hash)
            else:
                if item.hash in self.mempool or self.chain.get_transaction(item.hash):
                    continue
                wanted.append(item)
        if wanted:
            peer.send(protocol.GetData(tuple(wanted)))
        self._request_blocks(peer)

    def _request_blocks(self, peer: Peer) -> None:
        """Ask ``peer`` for the next batch of announced blocks."""
        batch: list[InvItem] = []
        while peer.pending_blocks and len(peer.requested_blocks) + len(batch) < BLOCKS_IN_FLIGHT:
            block_hash = peer.pending_blocks.popleft()
            if self.chain.has_block(block_hash):
                continue
            batch.append(InvItem(InvType.BLOCK, block_hash))
        if not batch:
            return
        peer.requested_blocks.update(item.hash for item in batch)
        peer.send(protocol.GetData(tuple(batch)))

    def _on_getdata(self, peer: Peer, message: protocol.GetData) -> None:
        missing: list[InvItem] = []
        for item in message.items:
            if item.is_block:
                block = self.chain.get_block(item.hash)
                if block is None:
                    missing.append(item)
                else:
                    peer.send(protocol.BlockMessage(block))
            else:
                transaction = self.mempool.get(item.hash)
                if transaction is None:
                    found = self.chain.get_transaction(item.hash)
                    transaction = None if found is None else found[0]
                if transaction is None:
                    missing.append(item)
                else:
                    peer.send(protocol.TxMessage(transaction))
        if missing:
            peer.send(protocol.NotFound(tuple(missing)))

    def _on_getblocks(self, peer: Peer, message: protocol.GetBlocks) -> None:
        fork_height = self.chain.find_fork_height(message.locator)
        hashes = self.chain.active_hashes_after(fork_height, protocol.MAX_BLOCKS_PER_INV)
        if message.stop_hash != b"\x00" * 32 and message.stop_hash in hashes:
            hashes = hashes[: hashes.index(message.stop_hash) + 1]
        if hashes:
            peer.send(protocol.Inv(tuple(InvItem(InvType.BLOCK, h) for h in hashes)))

    def _on_block(self, peer: Peer, block: Block) -> None:
        block_hash = block.hash()
        peer.requested_blocks.discard(block_hash)
        peer.note_inventory(block_hash)
        result = self.submit_block(block, source=peer)
        if result.status is BlockStatus.INVALID:
            self._misbehave(peer, 50, result.reason)
        elif result.status is BlockStatus.ORPHAN:
            self._maybe_sync(peer, force=True)
        if not peer.requested_blocks and not peer.pending_blocks:
            self._maybe_sync(peer, force=True)
        else:
            self._request_blocks(peer)

    def _on_tx(self, peer: Peer, transaction: Transaction) -> None:
        peer.note_inventory(transaction.txid())
        try:
            self.submit_transaction(transaction, source=peer)
        except MissingInputError:
            pass  # the parent transaction may still arrive
        except ValidationError as exc:
            self._misbehave(peer, 10, str(exc))

    def _announce_mempool(self, peer: Peer) -> None:
        txids = self.mempool.txids()
        if txids:
            peer.send(protocol.Inv(tuple(InvItem(InvType.TX, txid) for txid in txids)))

    @property
    def stale_tip_seconds(self) -> float:
        """How long a quiet tip is normal before we go looking for blocks."""
        return max(300.0, 10.0 * self.params.target_spacing)

    def _poll_for_blocks(self) -> None:
        """Ask every peer what follows our tip.

        Announcements can be missed — a peer may have been mid-handshake, or two
        nodes may sit on equal-height branches, in which case neither considers
        the other ahead. Re-asking on a slow timer makes the network converge
        without anyone having to restart a node.
        """
        for peer in self.peers:
            if peer.handshake_done.is_set():
                self._maybe_sync(peer, force=True)

    def _maybe_sync(self, peer: Peer, *, force: bool = False) -> None:
        """Ask ``peer`` for the blocks we are missing, if it looks ahead of us."""
        if peer.closed:
            return
        if not force and peer.start_height <= self.chain.height:
            return
        if peer.requested_blocks or peer.pending_blocks:
            return
        peer.send(protocol.GetBlocks(tuple(self.chain.locator())))

    # ------------------------------------------------------------- chain updates

    def submit_block(self, block: Block, *, source: Peer | None = None) -> AddBlockResult:
        """Validate a block, store it and relay it if it was accepted."""
        result = self.chain.add_block(block)
        if result.status is BlockStatus.ORPHAN:
            self._remember_orphan(block)
            return result
        if result.status is BlockStatus.INVALID:
            logger.info("rejected block %s: %s", block.hash_hex(), result.reason)
            return result
        if result.status is BlockStatus.CONNECTED:
            self._last_block_at = time.time()
            logger.info(
                "block %s accepted at height %d%s",
                block.hash_hex(),
                result.height,
                " (chain reorganised)" if result.reorganised else "",
            )
        self._relay(InvItem(InvType.BLOCK, block.hash()), source=source)
        self._connect_orphans_of(block.hash())
        return result

    def submit_transaction(
        self, transaction: Transaction, *, source: Peer | None = None
    ) -> MempoolEntry:
        """Validate a transaction, add it to the mempool and relay it.

        Raises:
            ValidationError: if the transaction is not acceptable.
        """
        entry = self.mempool.add(transaction)
        logger.info(
            "accepted transaction %s (%d bytes, %d scar fee)",
            entry.txid[::-1].hex(),
            entry.size,
            entry.fee,
        )
        self._relay(InvItem(InvType.TX, entry.txid), source=source)
        return entry

    def _relay(self, item: InvItem, *, source: Peer | None = None) -> None:
        message = protocol.Inv((item,))
        for peer in self.peers:
            if peer is source or not peer.handshake_done.is_set() or peer.knows(item.hash):
                continue
            peer.note_inventory(item.hash)
            try:
                peer.send(message)
            except PeerDisconnected:
                continue

    def _remember_orphan(self, block: Block) -> None:
        with self._lock:
            self._orphans[block.hash()] = (block, time.time())
            while len(self._orphans) > MAX_ORPHANS:
                self._orphans.popitem(last=False)

    def _connect_orphans_of(self, parent_hash: bytes) -> None:
        """Connect stored orphans that were waiting for ``parent_hash``."""
        pending = [parent_hash]
        while pending:
            current = pending.pop()
            with self._lock:
                children = [
                    block
                    for block, _ in self._orphans.values()
                    if block.header.prev_hash == current
                ]
                for block in children:
                    self._orphans.pop(block.hash(), None)
            for block in children:
                result = self.chain.add_block(block)
                if result.accepted:
                    logger.info("connected orphan block %s", block.hash_hex())
                    self._relay(InvItem(InvType.BLOCK, block.hash()))
                    pending.append(block.hash())

    # ----------------------------------------------------------------- housekeeping

    def _maintenance_loop(self) -> None:
        last_save = last_check = time.time()
        while not self._stop.wait(_TICK):
            now = time.time()
            if now - last_check < _MAINTENANCE_INTERVAL:
                continue
            last_check = now
            for peer in self.peers:
                idle = now - peer.last_message_at
                if idle > PEER_TIMEOUT:
                    logger.info("%s timed out", peer)
                    peer.close()
                    continue
                if idle > PING_INTERVAL and peer.last_ping_nonce is None:
                    peer.last_ping_nonce = random.getrandbits(64)
                    peer.last_ping_sent = now
                    try:
                        peer.send(protocol.Ping(peer.last_ping_nonce))
                    except PeerDisconnected:
                        continue
            if (
                self.peers
                and now - self._last_block_at > self.stale_tip_seconds
                and now - self._last_poll_at > _POLL_INTERVAL
            ):
                self._last_poll_at = now
                logger.debug("tip has not moved recently, asking peers for blocks")
                self._poll_for_blocks()

            if not self.peers and not len(self.addrbook) and self.seed_hosts:
                self._bootstrap_seeds()

            with self._lock:
                for block_hash, (_, seen) in list(self._orphans.items()):
                    if now - seen > 600:
                        del self._orphans[block_hash]
            if now - last_save > 300:
                self.addrbook.prune()
                self.addrbook.save()
                last_save = now

    # ----------------------------------------------------------------- reporting

    def info(self) -> dict:
        """Return a summary of the node, used by the ``getinfo`` RPC."""
        peers = self.peers
        data = self.chain.stats()
        data.update(
            {
                "version": __version__,
                "protocol_version": protocol.PROTOCOL_VERSION,
                "user_agent": self.config.user_agent,
                "uptime": round(time.time() - self.started_at, 1),
                "genesis": self.params.genesis_hash[::-1].hex(),
                "magic": self.params.magic.decode("ascii", "replace"),
                "listening": self.config.listen,
                "p2p_port": self.p2p_port,
                "peers": len(peers),
                "inbound_peers": sum(1 for peer in peers if peer.inbound),
                "known_addresses": len(self.addrbook),
                "own_addresses": sorted(f"{h}:{p}" for h, p in self._local_addresses),
                "mempool_size": len(self.mempool),
                "mempool_bytes": self.mempool.total_bytes,
                "orphan_blocks": len(self._orphans),
            }
        )
        return data

    @property
    def local_addresses(self) -> set[tuple[str, int]]:
        """Addresses discovered to be this node itself."""
        return set(self._local_addresses)

    def orphan_hashes(self) -> Iterable[bytes]:
        """Hashes of the blocks currently waiting for a parent."""
        with self._lock:
            return list(self._orphans)
