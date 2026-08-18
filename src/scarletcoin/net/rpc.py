"""The JSON-RPC interface and the built-in block explorer.

One HTTP server serves both:

* ``POST /`` or ``POST /rpc`` — JSON-RPC 2.0, used by the wallet, the miner and
  the command line tools.  If a token is configured, requests must carry an
  ``Authorization: Bearer <token>`` header.
* ``GET /...`` — a small read-only HTML explorer, plus ``GET /api/info`` for
  monitoring.
"""

from __future__ import annotations

import hmac
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from scarletcoin import __version__
from scarletcoin.core.block import Block
from scarletcoin.core.chain import BlockStatus
from scarletcoin.core.serialize import SerializationError
from scarletcoin.core.template import create_block_template
from scarletcoin.core.transaction import Transaction
from scarletcoin.core.validation import ValidationError
from scarletcoin.crypto.keys import Address, InvalidKeyError
from scarletcoin.net import explorer
from scarletcoin.net.node import Node
from scarletcoin.units import format_bytes

__all__ = ["MINING_METHODS", "PUBLIC_METHODS", "RpcError", "RpcServer", "build_methods"]

logger = logging.getLogger(__name__)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
APPLICATION_ERROR = -32000
UNAUTHORISED = -32001

#: Methods a node may safely answer for anybody, when started with ``--rpc-public``.
#:
#: These are the calls a wallet and a block explorer need: they read the chain, or
#: hand it a transaction that is validated like any other before being relayed.
#: Everything outside this set — mining, peer management, shutdown — stays behind
#: the bearer token.
PUBLIC_METHODS = frozenset(
    {
        "getinfo",
        "getblockcount",
        "getbestblockhash",
        "getdifficulty",
        "getsupply",
        "getchainsize",
        "getnetworkstats",
        "getpublicnodes",
        "getblockhash",
        "getblock",
        "getblockheader",
        "getrawblock",
        "gettransaction",
        "getrawtransaction",
        "getmempool",
        "estimatefee",
        "validateaddress",
        "getbalance",
        "getutxos",
        "getaddresshistory",
        "getrichlist",
        "sendrawtransaction",
    }
)

#: Mining methods, public only when the operator asks with ``--rpc-public-mining``.
#:
#: Handing out work costs the node a block template per request, and a public
#: node is by definition asked by strangers, so this is a separate decision from
#: serving the read-only set.  A miner needs both of these or none of them, which
#: is why they travel together.
MINING_METHODS = frozenset({"getblocktemplate", "submitblock"})

MAX_BODY = 8 * 1024 * 1024


class RpcError(Exception):
    """An error to report back to the JSON-RPC caller."""

    def __init__(self, message: str, code: int = APPLICATION_ERROR) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _hash_from_hex(text: str, what: str = "hash") -> bytes:
    """Parse a display-order hex hash into internal byte order."""
    try:
        raw = bytes.fromhex(str(text).strip())
    except ValueError:
        raise RpcError(f"{what} is not valid hexadecimal", INVALID_PARAMS) from None
    if len(raw) != 32:
        raise RpcError(f"{what} must be 32 bytes ({len(raw)} given)", INVALID_PARAMS)
    return raw[::-1]


def _pruned_or_missing(entry: object) -> str:
    """Explain why a block the index knows about cannot be handed over."""
    if getattr(entry, "pruned", False):
        return (
            f"block {getattr(entry, 'height', '?')} has been pruned by this node:"
            " only its header is still stored. Ask a node that keeps the whole chain."
        )
    return "no block with that hash"  # pragma: no cover - index and data go together


def build_methods(node: Node) -> dict[str, Callable[..., object]]:
    """Return the RPC method table for ``node``."""
    chain = node.chain
    params = node.params

    def address_hash(text: str) -> bytes:
        for version in (params.address_version, params.script_address_version):
            try:
                address = Address.decode(str(text), expected_version=version)
            except InvalidKeyError:
                continue
            return address.hash
        raise RpcError(f"{text!r} is not a valid {params.name} address", INVALID_PARAMS)

    def entry_or_error(block_hash: bytes):
        entry = chain.get_entry(block_hash)
        if entry is None:
            raise RpcError("no block with that hash")
        return entry

    def resolve_block(identifier: object) -> bytes:
        """Accept either a block hash or a height."""
        if isinstance(identifier, int):
            entry = chain.get_entry_by_height(identifier)
            if entry is None:
                raise RpcError(f"no block at height {identifier}")
            return entry.hash
        text = str(identifier)
        if text.isdigit() and len(text) < 12:
            return resolve_block(int(text))
        return _hash_from_hex(text, "block hash")

    # ---------------------------------------------------------------- chain info

    def getinfo() -> dict:
        return node.info()

    def getblockcount() -> int:
        return chain.height

    def getbestblockhash() -> str:
        return chain.tip.hash[::-1].hex()

    def getdifficulty() -> float:
        return chain.difficulty()

    def getsupply() -> dict:
        count, total = node.storage.utxo_stats()
        return {"supply": total, "utxo_count": count, "height": chain.height}

    def getchainsize() -> dict:
        """How much room this chain takes up, in bytes and in words."""
        sizes = node.storage.size_stats()
        return {
            "height": chain.height,
            "blocks": sizes["blocks"],
            "chain_blocks": sizes["chain_blocks"],
            "chain_bytes": sizes["chain_bytes"],
            "chain_size": format_bytes(sizes["chain_bytes"]),
            "block_bytes": sizes["block_bytes"],
            "block_size": format_bytes(sizes["block_bytes"]),
            "undo_bytes": sizes["undo_bytes"],
            "disk_bytes": sizes["disk_bytes"],
            "disk_size": format_bytes(sizes["disk_bytes"]),
            "average_block_bytes": sizes["average_block_bytes"],
            "average_block_size": format_bytes(sizes["average_block_bytes"]),
            "pruned_blocks": sizes["pruned_blocks"],
            "prune_height": sizes["prune_height"],
        }

    def getpublicnodes() -> list[str]:
        """Public RPC endpoints this node knows about, so clients can hop on."""
        return node.public_nodes()

    def getnetworkstats(window: int | None = None) -> dict:
        return chain.network_stats(None if window is None else int(window))

    def getblockhash(height: int) -> str:
        entry = chain.get_entry_by_height(int(height))
        if entry is None:
            raise RpcError(f"no block at height {height}")
        return entry.hash[::-1].hex()

    def getblock(block: object, verbose: bool = True) -> dict:
        block_hash = resolve_block(block)
        entry = entry_or_error(block_hash)
        stored = chain.get_block(block_hash)
        if stored is None:
            raise RpcError(_pruned_or_missing(entry))
        data = stored.to_dict(params.address_version, verbose=bool(verbose))
        data.update(
            {
                "height": entry.height,
                "confirmations": chain.confirmations(entry.height) if entry.in_chain else 0,
                "in_active_chain": entry.in_chain,
                "chainwork": entry.chainwork,
            }
        )
        return data

    def getblockheader(block: object) -> dict:
        block_hash = resolve_block(block)
        entry = entry_or_error(block_hash)
        data = entry.header.to_dict()
        data.update({"height": entry.height, "in_active_chain": entry.in_chain})
        return data

    def getrawblock(block: object) -> str:
        block_hash = resolve_block(block)
        stored = chain.get_block(block_hash)
        if stored is None:
            raise RpcError(_pruned_or_missing(chain.get_entry(block_hash)))
        return stored.serialize().hex()

    # ------------------------------------------------------------- transactions

    def gettransaction(txid: str) -> dict:
        raw_txid = _hash_from_hex(txid, "transaction id")
        found = chain.get_transaction(raw_txid)
        if found is not None:
            transaction, location = found
            data = transaction.to_dict(params.address_version)
            data.update(
                {
                    "confirmations": chain.confirmations(location.height),
                    "height": location.height,
                    "block": location.block_hash[::-1].hex(),
                    "in_mempool": False,
                }
            )
            return data
        pooled = node.mempool.get(raw_txid)
        if pooled is None:
            raise RpcError("no transaction with that id")
        data = pooled.to_dict(params.address_version)
        data.update({"confirmations": 0, "height": None, "block": None, "in_mempool": True})
        return data

    def getrawtransaction(txid: str) -> str:
        raw_txid = _hash_from_hex(txid, "transaction id")
        pooled = node.mempool.get(raw_txid)
        if pooled is not None:
            return pooled.serialize().hex()
        found = chain.get_transaction(raw_txid)
        if found is None:
            raise RpcError("no transaction with that id")
        return found[0].serialize().hex()

    def sendrawtransaction(raw: str) -> str:
        try:
            transaction = Transaction.deserialize(bytes.fromhex(str(raw).strip()))
        except (ValueError, SerializationError) as exc:
            raise RpcError(f"cannot decode transaction: {exc}", INVALID_PARAMS) from exc
        try:
            entry = node.submit_transaction(transaction)
        except ValidationError as exc:
            raise RpcError(str(exc)) from exc
        return entry.txid[::-1].hex()

    def getmempool() -> dict:
        return node.mempool.to_dict()

    def estimatefee(blocks: int = 1) -> dict:
        rate = node.mempool.estimate_fee_rate(blocks)
        return {
            "fee_per_kb": rate,
            "blocks": blocks,
            "mempool_size": len(node.mempool),
        }

    # ------------------------------------------------------------------ addresses

    def validateaddress(address: str) -> dict:
        for version in (params.address_version, params.script_address_version):
            try:
                parsed = Address.decode(str(address), expected_version=version)
            except InvalidKeyError:
                continue
            kind = "p2sh" if version == params.script_address_version else "p2pkh"
            return {
                "valid": True,
                "address": str(parsed),
                "pubkey_hash": parsed.hash.hex(),
                "type": kind,
            }
        return {"valid": False, "reason": f"{address!r} is not a {params.name} address"}

    def getbalance(address: str) -> dict:
        pubkey_hash = address_hash(address)
        coins = node.storage.coins_of(pubkey_hash)
        height = chain.height
        confirmed = sum(coin.value for _, coin in coins)
        spendable = sum(
            coin.value
            for outpoint, coin in coins
            if coin.is_spendable_at(height + 1, params.coinbase_maturity)
            and not node.mempool.is_spent(outpoint)
        )
        immature = sum(
            coin.value
            for _, coin in coins
            if not coin.is_spendable_at(height + 1, params.coinbase_maturity)
        )
        mempool_spent_value = confirmed - spendable - immature
        mempool_spent_count = sum(
            1 for outpoint, _coin in coins if node.mempool.is_spent(outpoint)
        )
        return {
            "address": str(address),
            "balance": confirmed,
            "spendable": spendable,
            "immature": immature,
            "utxo_count": len(coins),
            "mempool_spent": mempool_spent_value,
            "mempool_spent_count": mempool_spent_count,
            "height": height,
        }

    def getutxos(address: str) -> dict:
        pubkey_hash = address_hash(address)
        height = chain.height
        coins = node.storage.coins_of(pubkey_hash)
        return {
            "address": str(address),
            "height": height,
            "utxos": [
                {
                    "txid": outpoint.txid[::-1].hex(),
                    "index": outpoint.index,
                    "value": coin.value,
                    "height": coin.height,
                    "confirmations": chain.confirmations(coin.height),
                    "coinbase": coin.is_coinbase,
                    "spendable": coin.is_spendable_at(height + 1, params.coinbase_maturity)
                    and not node.mempool.is_spent(outpoint),
                    "mempool_spent": node.mempool.is_spent(outpoint),
                }
                for outpoint, coin in coins
            ],
        }

    def getaddresshistory(address: str, limit: int = 100) -> dict:
        pubkey_hash = address_hash(address)
        limit = max(1, min(int(limit), 1000))
        history = []
        for txid, height in node.storage.address_history(pubkey_hash, limit):
            found = chain.get_transaction(txid)
            if found is None:  # pragma: no cover - index follows the chain
                continue
            transaction, _ = found
            received = sum(
                output.value for output in transaction.outputs if output.payload == pubkey_hash
            )
            sent = 0
            for txin in transaction.inputs:
                if txin.prevout.is_null:
                    continue
                parent = chain.get_transaction(txin.prevout.txid)
                if parent is None:
                    continue
                output = parent[0].outputs[txin.prevout.index]
                if output.payload == pubkey_hash:
                    sent += output.value
            history.append(
                {
                    "txid": txid[::-1].hex(),
                    "height": height,
                    "confirmations": chain.confirmations(height),
                    "received": received,
                    "sent": sent,
                    "net": received - sent,
                    "coinbase": transaction.is_coinbase,
                }
            )
        return {"address": str(address), "transactions": history}

    def getrichlist(limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        return [
            {"address": str(Address(params.address_version, pubkey_hash)), "balance": total}
            for pubkey_hash, total in node.storage.richest_addresses(limit)
        ]

    # ---------------------------------------------------------------- mining

    def getblocktemplate() -> dict:
        template = create_block_template(chain, node.mempool)
        data = template.to_dict()
        data["network"] = params.name
        return data

    def submitblock(raw: str) -> dict:
        try:
            block = Block.deserialize(bytes.fromhex(str(raw).strip()))
        except (ValueError, SerializationError) as exc:
            raise RpcError(f"cannot decode block: {exc}", INVALID_PARAMS) from exc
        result = node.submit_block(block)
        if result.status is BlockStatus.INVALID:
            raise RpcError(f"block rejected: {result.reason}")
        return {
            "status": result.status.value,
            "hash": block.hash_hex(),
            "height": result.height,
            "reorganised": result.reorganised,
        }

    # ---------------------------------------------------------------- networking

    def getpeers() -> list[dict]:
        return [peer.to_dict() for peer in node.peers]

    def addpeer(host: str, port: int | None = None) -> dict:
        target = int(port) if port else params.default_p2p_port
        connected = node.connect_peer(str(host), target)
        return {"connected": connected, "peer": f"{host}:{target}"}

    def getaddresses() -> list[dict]:
        return [
            {"host": entry.host, "port": entry.port, "last_seen": entry.last_seen}
            for entry in node.addrbook.all()
        ]

    def stop() -> str:
        threading.Thread(target=node.stop, name="scarlet-stop", daemon=True).start()
        return "stopping"

    # ------------------------------------------------------------------ storage

    def prune(keep: int | None = None, vacuum: bool = False) -> dict:
        """Drop the bodies of old blocks, keeping the last ``keep`` of them.

        Irreversible: the node can no longer show those blocks, serve them to a
        syncing peer, or reorganise past them.  Behind the token, because it
        permanently changes what the node can do.
        """
        target = node.config.prune if keep is None else int(keep)
        if target <= 0:
            raise RpcError(
                "give the number of recent blocks to keep, for example prune 5000",
                INVALID_PARAMS,
            )
        result = node.prune_now(target)
        if vacuum:
            result["reclaimed_bytes"] = node.storage.vacuum()
        result["disk_bytes"] = node.storage.size_stats(max_age=0.0)["disk_bytes"]
        result["disk_size"] = format_bytes(result["disk_bytes"])
        result["freed_size"] = format_bytes(result["freed_bytes"])
        return result

    methods: dict[str, Callable[..., object]] = {
        "getinfo": getinfo,
        "getblockcount": getblockcount,
        "getbestblockhash": getbestblockhash,
        "getdifficulty": getdifficulty,
        "getsupply": getsupply,
        "getchainsize": getchainsize,
        "getnetworkstats": getnetworkstats,
        "getpublicnodes": getpublicnodes,
        "getblockhash": getblockhash,
        "getblock": getblock,
        "getblockheader": getblockheader,
        "getrawblock": getrawblock,
        "gettransaction": gettransaction,
        "getrawtransaction": getrawtransaction,
        "sendrawtransaction": sendrawtransaction,
        "getmempool": getmempool,
        "estimatefee": estimatefee,
        "validateaddress": validateaddress,
        "getbalance": getbalance,
        "getutxos": getutxos,
        "getaddresshistory": getaddresshistory,
        "getrichlist": getrichlist,
        "getblocktemplate": getblocktemplate,
        "submitblock": submitblock,
        "getpeers": getpeers,
        "addpeer": addpeer,
        "getaddresses": getaddresses,
        "prune": prune,
        "stop": stop,
    }

    if params.name == "regtest":

        def generate(count: int = 1, address: str | None = None) -> list[str]:
            """Mine blocks immediately; only available on regtest."""
            from scarletcoin.crypto.keys import PrivateKey
            from scarletcoin.miner.solver import solve_block

            pubkey_hash = (
                address_hash(address) if address else PrivateKey.generate().public_key().hash160()
            )
            mined: list[str] = []
            for _ in range(max(1, int(count))):
                template = create_block_template(chain, node.mempool)
                candidate = template.build_block(pubkey_hash=pubkey_hash, extra=b"generate")
                solved = solve_block(candidate)
                if solved is None:  # pragma: no cover - regtest always solves
                    raise RpcError("could not solve a block")
                result = node.submit_block(solved)
                if result.status is BlockStatus.INVALID:
                    raise RpcError(f"generated an invalid block: {result.reason}")
                mined.append(solved.hash_hex())
            return mined

        methods["generate"] = generate

    return methods


class RpcServer:
    """HTTP server exposing the JSON-RPC interface and the explorer."""

    def __init__(
        self,
        node: Node,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        public: bool | None = None,
        public_mining: bool | None = None,
        cors: str | None = None,
    ) -> None:
        self.node = node
        self.token = token
        self.public = node.config.rpc_public if public is None else bool(public)
        """Whether unauthenticated callers may use :data:`PUBLIC_METHODS`."""
        self.public_mining = (
            node.config.rpc_public_mining if public_mining is None else bool(public_mining)
        )
        """Whether unauthenticated callers may also use :data:`MINING_METHODS`."""
        if self.public_mining:
            self.public = True
        # The server decides the policy, so the node's configuration is brought
        # into line with it: ``getinfo`` reports these, and a wallet on the other
        # side of the internet has no other way to find out.
        node.config.rpc_public = self.public
        node.config.rpc_public_mining = self.public_mining
        self.cors = node.config.resolved_rpc_cors if cors is None else cors
        """Value of the ``Access-Control-Allow-Origin`` header, or ``None``."""
        self.methods = build_methods(node)
        self._server = ThreadingHTTPServer((host, port), self._make_handler())
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The port the server is bound to."""
        return self._server.server_address[1]

    @property
    def host(self) -> str:
        """The address the server is bound to."""
        return self._server.server_address[0]

    @property
    def url(self) -> str:
        """The base URL of the server."""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> None:
        """Serve requests in a background thread."""
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="scarlet-rpc", daemon=True
        )
        self._thread.start()
        logger.info("RPC and explorer listening on %s", self.url)

    def stop(self) -> None:
        """Stop serving and release the socket.

        Safe on a server that was never started: ``shutdown`` waits for the
        serving loop to acknowledge it, which never happens if there is no loop,
        so it is only called when there is a thread to stop.
        """
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> RpcServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ internals

    def _authorised(self, header: str | None) -> bool:
        """Whether a request carries the right token (or none is required)."""
        if not self.token:
            return True
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self.token)

    def allows_anonymous(self, method: str) -> bool:
        """Whether ``method`` may be called without the token."""
        if not self.public:
            return False
        if method in PUBLIC_METHODS:
            return True
        return self.public_mining and method in MINING_METHODS

    def _dispatch(self, method: str, params: object) -> object:
        handler = self.methods.get(method)
        if handler is None:
            raise RpcError(f"unknown method {method!r}", METHOD_NOT_FOUND)
        try:
            if params is None:
                return handler()
            if isinstance(params, list):
                return handler(*params)
            if isinstance(params, dict):
                return handler(**params)
        except TypeError as exc:
            signature = inspect.signature(handler)
            raise RpcError(
                f"bad parameters for {method}{signature}: {exc}", INVALID_PARAMS
            ) from exc
        raise RpcError("params must be a list or an object", INVALID_PARAMS)

    def handle_rpc(self, body: bytes, *, authorised: bool = True) -> dict | list:
        """Execute one JSON-RPC request (or batch) and return the response object.

        Args:
            body: The raw request body.
            authorised: Whether the caller presented a valid token. When it did
                not, only :data:`PUBLIC_METHODS` are answered, and only on a node
                started with ``--rpc-public``.
        """
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _error_response(None, PARSE_ERROR, f"invalid JSON: {exc}")
        if isinstance(request, list):
            return [self._handle_single(item, authorised=authorised) for item in request]
        return self._handle_single(request, authorised=authorised)

    def _handle_single(self, request: object, *, authorised: bool = True) -> dict:
        if not isinstance(request, dict):
            return _error_response(None, INVALID_REQUEST, "a request must be an object")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return _error_response(request_id, INVALID_REQUEST, "missing method name")
        if not authorised and not self.allows_anonymous(method):
            return _error_response(
                request_id,
                UNAUTHORISED,
                f"{method} needs the node's RPC token"
                if method in self.methods
                else f"unknown method {method!r}",
            )
        try:
            result = self._dispatch(method, request.get("params"))
        except RpcError as exc:
            return _error_response(request_id, exc.code, exc.message)
        except ValidationError as exc:
            return _error_response(request_id, APPLICATION_ERROR, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("RPC method %s failed", method)
            return _error_response(request_id, APPLICATION_ERROR, f"internal error: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = f"scarletcoin/{__version__}"

            def log_message(self, format: str, *args: object) -> None:
                logger.debug("%s - %s", self.address_string(), format % args)

            def _send_cors_headers(self) -> None:
                """Emit the CORS headers a browser wallet needs, when enabled."""
                if not server.cors:
                    return
                self.send_header("Access-Control-Allow-Origin", server.cors)
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Access-Control-Max-Age", "86400")
                if server.cors != "*":
                    self.send_header("Vary", "Origin")

            def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self._send_cors_headers()
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        # The client went away; nothing to do, and the handler
                        # loop notices the closed socket and disconnects.
                        self.close_connection = True

            def do_OPTIONS(self) -> None:
                """Answer a CORS preflight, no matter which path it targets."""
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self._send_cors_headers()
                self.end_headers()

            def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
                self._respond(
                    status, json.dumps(payload, indent=1).encode("utf-8"), "application/json"
                )

            def _html(self, markup: str, status: HTTPStatus = HTTPStatus.OK) -> None:
                self._respond(status, markup.encode("utf-8"), "text/html; charset=utf-8")

            def do_POST(self) -> None:
                url = urlparse(self.path)
                if url.path not in ("/", "/rpc"):
                    self._json({"error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)
                    return
                authorised = server._authorised(self.headers.get("Authorization"))
                if not authorised and not server.public:
                    self._json({"error": "unauthorised"}, HTTPStatus.UNAUTHORIZED)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length > MAX_BODY:
                    self._json({"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                body = self.rfile.read(length) if length else b""
                self._json(server.handle_rpc(body, authorised=authorised))

            def do_GET(self) -> None:
                url = urlparse(self.path)
                path = unquote(url.path).rstrip("/") or "/"
                query = parse_qs(url.query)
                try:
                    if path == "/api/info":
                        self._json(server.node.info())
                        return
                    if path in ("/icon.svg", "/favicon.ico"):
                        self._respond(
                            HTTPStatus.OK,
                            explorer.FAVICON_SVG.encode("utf-8"),
                            "image/svg+xml",
                        )
                        return
                    if path == "/metrics":
                        self._respond(
                            HTTPStatus.OK,
                            metrics_text(server.node).encode("utf-8"),
                            "text/plain; version=0.0.4; charset=utf-8",
                        )
                        return
                    markup = explorer.render(server, path, query)
                except explorer.NotFound as exc:
                    self._html(explorer.render_error(server, str(exc)), HTTPStatus.NOT_FOUND)
                    return
                self._html(markup)

            def do_HEAD(self) -> None:
                self.do_GET()

        return Handler


def _error_response(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def metrics_text(node: Node) -> str:
    """Render Prometheus-format metrics for ``node``."""
    chain = node.chain
    stats = chain.stats()
    peers = node.peers
    mempool = node.mempool
    lines = [
        "# HELP scarletcoin_height Active chain height.",
        "# TYPE scarletcoin_height gauge",
        f"scarletcoin_height {chain.height}",
        "# HELP scarletcoin_peers Connected peers.",
        "# TYPE scarletcoin_peers gauge",
        f"scarletcoin_peers {len(peers)}",
        "# HELP scarletcoin_inbound_peers Inbound peer connections.",
        "# TYPE scarletcoin_inbound_peers gauge",
        f"scarletcoin_inbound_peers {sum(1 for p in peers if p.inbound)}",
        "# HELP scarletcoin_mempool_transactions Unconfirmed transactions.",
        "# TYPE scarletcoin_mempool_transactions gauge",
        f"scarletcoin_mempool_transactions {len(mempool)}",
        "# HELP scarletcoin_mempool_bytes Unconfirmed transaction bytes.",
        "# TYPE scarletcoin_mempool_bytes gauge",
        f"scarletcoin_mempool_bytes {mempool.total_bytes}",
        "# HELP scarletcoin_utxo_count Unspent outputs.",
        "# TYPE scarletcoin_utxo_count gauge",
        f"scarletcoin_utxo_count {stats['utxo_count']}",
        "# HELP scarletcoin_supply_scar Circulating supply in scar.",
        "# TYPE scarletcoin_supply_scar gauge",
        f"scarletcoin_supply_scar {stats['supply']}",
        "# HELP scarletcoin_difficulty Current difficulty.",
        "# TYPE scarletcoin_difficulty gauge",
        f"scarletcoin_difficulty {stats['difficulty']:.4f}",
        "# HELP scarletcoin_chain_bytes Serialised active-chain size.",
        "# TYPE scarletcoin_chain_bytes gauge",
        f"scarletcoin_chain_bytes {stats['chain_bytes']}",
        "# HELP scarletcoin_disk_bytes On-disk database size.",
        "# TYPE scarletcoin_disk_bytes gauge",
        f"scarletcoin_disk_bytes {stats['disk_bytes']}",
        "# HELP scarletcoin_uptime_seconds Process uptime.",
        "# TYPE scarletcoin_uptime_seconds gauge",
        f"scarletcoin_uptime_seconds {time.time() - node.started_at:.1f}",
    ]
    return "\n".join(lines) + "\n"
