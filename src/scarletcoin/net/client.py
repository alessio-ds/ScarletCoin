"""A small JSON-RPC client for talking to a ScarletCoin node."""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request
from typing import Any

from scarletcoin.core.params import get_params

__all__ = ["RpcClient", "RpcClientError", "default_url"]

_ids = itertools.count(1)


class RpcClientError(Exception):
    """Raised when a node refuses a request or cannot be reached."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def default_url(network: str = "mainnet", host: str = "127.0.0.1") -> str:
    """Return the usual RPC URL for a network."""
    return f"http://{host}:{get_params(network).default_rpc_port}"


class RpcClient:
    """Calls JSON-RPC methods on a node over HTTP."""

    def __init__(self, url: str, *, token: str | None = None, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

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
        request = urllib.request.Request(
            f"{self.url}/rpc",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise RpcClientError(
                f"node returned HTTP {exc.code} for {method}: {detail}", exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise RpcClientError(f"cannot reach the node at {self.url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RpcClientError(f"the node did not answer within {self.timeout}s") from exc

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

    def getutxos(self, address: str) -> dict:
        """Return the unspent outputs of an address."""
        return self.call("getutxos", address)

    def getaddresshistory(self, address: str, limit: int = 100) -> dict:
        """Return the transactions that touched an address."""
        return self.call("getaddresshistory", address, limit)

    def gettransaction(self, txid: str) -> dict:
        """Return a decoded transaction."""
        return self.call("gettransaction", txid)

    def sendrawtransaction(self, raw_hex: str) -> str:
        """Broadcast a serialised transaction and return its id."""
        return self.call("sendrawtransaction", raw_hex)

    def getblocktemplate(self) -> dict:
        """Fetch a template for the next block."""
        return self.call("getblocktemplate")

    def submitblock(self, raw_hex: str) -> dict:
        """Submit a solved block."""
        return self.call("submitblock", raw_hex)
