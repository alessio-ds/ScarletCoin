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
from scarletcoin.net.proxy import SocksHTTPConnection, SocksHTTPSConnection

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


def default_url(network: str = "mainnet", host: str = "127.0.0.1") -> str:
    """Return the usual RPC URL for a network."""
    return f"http://{host}:{get_params(network).default_rpc_port}"


class RpcClient:
    """Calls JSON-RPC methods on a node over HTTP."""

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        proxy_host: str | None = None,
        proxy_port: int = 9050,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port

        context = (
            ssl.create_default_context(cafile=certifi.where())
            if self.url.startswith("https://")
            else None
        )

        if proxy_host:
            handlers: list[urllib.request.BaseHandler] = []
            from urllib.request import HTTPHandler, HTTPSHandler

            class _ProxyHTTP(HTTPHandler):
                def http_open(self, req):
                    def _conn(h, p, **kw):
                        return SocksHTTPConnection(
                            proxy_host, proxy_port, h, p, timeout=timeout, **kw
                        )
                    return self.do_open(_conn, req)

            handlers.append(_ProxyHTTP())
            if context:
                class _ProxyHTTPS(HTTPSHandler):
                    def https_open(self, req):
                        def _conn(h, p, **kw):
                            return SocksHTTPSConnection(
                                proxy_host, proxy_port, h, p,
                                timeout=timeout, context=context, **kw,
                            )
                        return self.do_open(_conn, req)
                handlers.append(_ProxyHTTPS())

            self._opener = urllib.request.build_opener(*handlers)
        else:
            self._opener = urllib.request.build_opener()
            if context:
                https_handler = urllib.request.HTTPSHandler(context=context)
                self._opener = urllib.request.build_opener(https_handler)

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
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        except (ValueError, http.client.InvalidURL) as exc:
            raise RpcClientError(f"cannot reach the node at {self.url}: {exc}") from exc
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for attempt in range(MAX_ATTEMPTS):
            try:
                with self._opener.open(
                    request, timeout=self.timeout
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

    def getblock(self, height: int, verbose: bool = True) -> dict:
        """Return a block, either its transaction list or just its txids."""
        return self.call("getblock", height, verbose)

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

    def getoutputs(self) -> list:
        """Return all outputs for decoy selection (one_time_key, value, height)."""
        return self.call("getoutputs")

    def getkeyimages(self) -> list:
        """Return all spent key images."""
        return self.call("getkeyimages")
