"""A small JSON-RPC client for talking to a ScarletCoin node."""

from __future__ import annotations

import http.client
import itertools
import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

import certifi

from scarletcoin.core.params import get_params

__all__ = ["RpcClient", "RpcClientError", "default_url"]

_ids = itertools.count(1)

#: How many attempts for a request whose connection drops before answering.
MAX_ATTEMPTS = 3


def _is_transient(exc: Exception) -> bool:
    """Whether ``exc`` means the node dropped the connection instead of answering.

    A freshly started or overloaded node sometimes tears the TCP connection down
    mid-response, which Python 3.13 surfaces as an ``IncompleteRead``. Those are
    worth retrying once or twice. Everything else — refused tokens, bad
    requests, connection refusals, timeouts — is returned to the caller.
    """
    dropped = (
        http.client.IncompleteRead,
        http.client.RemoteDisconnected,
        ConnectionResetError,
        BrokenPipeError,
        ConnectionAbortedError,
    )
    if isinstance(exc, dropped):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, dropped)


class RpcClientError(Exception):
    """Raised when a node refuses a request or cannot be reached."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _is_unknown_method(exc: RpcClientError) -> bool:
    """Whether the node answered that it does not know the method at all."""
    return exc.code == -32601 or "unknown method" in str(exc)


def default_url(network: str = "mainnet", host: str = "127.0.0.1") -> str:
    """Return the usual RPC URL for a network."""
    return f"http://{host}:{get_params(network).default_rpc_port}"


class RpcClient:
    """Calls JSON-RPC methods on a node over HTTP."""

    def __init__(self, url: str, *, token: str | None = None, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._context = (
            ssl.create_default_context(cafile=certifi.where())
            if self.url.startswith("https://")
            else None
        )

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke ``method`` and return its result.

        Positional and keyword arguments are mutually exclusive, as in JSON-RPC.

        Raises:
            RpcClientError: if the node is unreachable or returns an error.
        """
        if args and kwargs:
            raise ValueError("pass either positional or keyword parameters, not both")
        payload = {
            "jsonrpc": "2.0",
            "id": next(_ids),
            "method": method,
            "params": kwargs if kwargs else list(args),
        }
        try:
            request = urllib.request.Request(
                f"{self.url}/rpc",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Connection": "close",
                },
                method="POST",
            )
        except (ValueError, http.client.InvalidURL) as exc:
            raise RpcClientError(f"cannot reach the node at {self.url}: {exc}") from exc
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for attempt in range(MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout, context=self._context
                ) as response:
                    body = response.read()
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:200]
                raise RpcClientError(
                    f"node returned HTTP {exc.code} for {method}: {detail}", exc.code
                ) from exc
            except urllib.error.URLError as exc:
                if not _is_transient(exc):
                    raise RpcClientError(
                        f"cannot reach the node at {self.url}: {exc.reason}"
                    ) from exc
            except (ValueError, http.client.InvalidURL) as exc:
                raise RpcClientError(f"cannot reach the node at {self.url}: {exc}") from exc
            except TimeoutError as exc:
                raise RpcClientError(f"the node did not answer within {self.timeout}s") from exc
            except (http.client.IncompleteRead, http.client.RemoteDisconnected, OSError) as exc:
                if not _is_transient(exc):
                    raise RpcClientError(f"the node dropped the connection: {exc}") from exc
            if attempt == MAX_ATTEMPTS - 1:
                raise RpcClientError(f"the node closed the connection while answering {method}")
            time.sleep(0.1 * (attempt + 1))

        try:
            message = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpcClientError(f"malformed answer from the node: {exc}") from exc
        if not isinstance(message, dict):
            raise RpcClientError("malformed answer from the node: expected an object")
        error = message.get("error")
        if error:
            raise RpcClientError(str(error.get("message", error)), error.get("code"))
        return message.get("result")

    # A few shortcuts for the calls the tools make most often.

    def getinfo(self) -> dict:
        """Return the node's status."""
        return self.call("getinfo")

    def getblockcount(self) -> int:
        """Return the height of the node's best chain."""
        return self.call("getblockcount")

    def getbalance(self, address: str) -> dict:
        """Return the balance of an address."""
        return self.call("getbalance", address)

    def getbalances(self, addresses: list[str]) -> dict:
        """Return the balances of several addresses in one request."""
        return self.call("getbalances", addresses)

    def getutxos(self, address: str) -> dict:
        """Return the unspent outputs of an address."""
        return self.call("getutxos", address)

    def getutxosmulti(self, addresses: list[str]) -> dict:
        """Return the unspent outputs of several addresses in one request.

        A node that does not know the method — one that has not been upgraded
        yet — is asked one address at a time instead, so a new wallet keeps
        working against an older node.
        """
        try:
            return self.call("getutxosmulti", addresses)
        except RpcClientError as exc:
            if not _is_unknown_method(exc):
                raise
        return {address: self.getutxos(address) for address in addresses}

    def getaddresshistory(self, address: str, limit: int = 100) -> dict:
        """Return the transactions that touched an address."""
        return self.call("getaddresshistory", address, limit)

    def gettransaction(self, txid: str) -> dict:
        """Return a decoded transaction."""
        return self.call("gettransaction", txid)

    def sendrawtransaction(self, raw_hex: str) -> str:
        """Broadcast a serialised transaction and return its id."""
        return self.call("sendrawtransaction", raw_hex)

    def broadcast(self, raw_hex: str, txid: str) -> str:
        """Broadcast a transaction, tolerating a node that was too slow to answer.

        A public node behind a reverse proxy can return a 502, or the client
        timeout can fire, while the node is still verifying a large
        transaction.  The transaction may well have been accepted even though
        the caller saw a failure, so look for it before reporting the broadcast
        lost.  A real rejection (a 4xx, or an error the node actually returned)
        is raised immediately and never retried.

        Args:
            raw_hex: The serialised transaction.
            txid: Its id in display order, so the node can be asked about it.

        Returns:
            The transaction id.

        Raises:
            RpcClientError: if the node rejected it, or never accepted it.
        """
        try:
            return self.sendrawtransaction(raw_hex)
        except RpcClientError as exc:
            if exc.code is not None and exc.code < 500:
                raise
            # The verification may still be running: give it time to land in
            # the mempool (or a block) and then ask the node whether it knows it.
            for delay in (1.0, 2.0, 4.0):
                time.sleep(delay)
                try:
                    if self.gettransaction(txid):
                        return txid
                except RpcClientError:
                    continue
            raise

    def getblocktemplate(self) -> dict:
        """Fetch a template for the next block."""
        return self.call("getblocktemplate")

    def submitblock(self, raw_hex: str) -> dict:
        """Submit a solved block."""
        return self.call("submitblock", raw_hex)
