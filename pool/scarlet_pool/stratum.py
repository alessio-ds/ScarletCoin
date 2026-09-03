"""Stratum V1 protocol messages — wire encoding and parsing.

Newline-delimited JSON over TCP, as specified by the Stratum mining protocol.
Each message is a single JSON line terminated by ``\\n``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = ["StratumError", "StratumRequest", "StratumResponse", "encode_message", "read_message"]


class StratumError(Exception):
    """Raised when a Stratum message is malformed."""


@dataclass
class StratumRequest:
    """A parsed JSON-RPC request from a miner."""

    method: str
    params: list[Any] = field(default_factory=list)
    id: int | None = None

    @classmethod
    def parse(cls, line: str) -> StratumRequest:
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StratumError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise StratumError("request must be a JSON object")
        method = data.get("method")
        if not isinstance(method, str):
            raise StratumError("missing or invalid method name")
        params = data.get("params", [])
        if not isinstance(params, list):
            raise StratumError("params must be an array")
        req_id = data.get("id")
        if req_id is not None and not isinstance(req_id, (int, float, str)):
            raise StratumError("invalid request id")
        return cls(method=method, params=params, id=req_id)


@dataclass
class StratumResponse:
    """A JSON-RPC response to send to a miner."""

    result: Any = None
    error: tuple[int, str, Any] | None = None
    id: int | None = None

    def encode(self) -> str:
        payload: dict[str, Any] = {"id": self.id, "jsonrpc": "2.0"}
        if self.error is not None:
            payload["error"] = {
                "code": self.error[0],
                "message": self.error[1],
                "data": self.error[2],
            }
        else:
            payload["result"] = self.result
        return json.dumps(payload, separators=(",", ":"))


def encode_message(obj: dict[str, Any]) -> str:
    """Encode a JSON-RPC notification or response as a Stratum line."""
    return json.dumps(obj, separators=(",", ":")) + "\n"


async def read_message(reader: asyncio.StreamReader) -> str:
    """Read one newline-delimited line from *reader*."""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=120.0)
    except asyncio.TimeoutError:
        raise StratumError("read timeout") from None
    if not line:
        raise StratumError("connection closed") from None
    return line.decode("utf-8").rstrip("\n").rstrip("\r")
