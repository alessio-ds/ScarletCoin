#!/usr/bin/env python3
"""Probe a Stratum merged-mining bridge end to end.

Speaks Stratum V1 to a ScarletCoin pool exactly as stock Bitcoin ASIC firmware
does — subscribe, authorize, take a job, assemble the coinbase, fold the Merkle
branch, grind a nonce, submit it — and reports whether the pool accepted it and
whether the chain actually advanced.

This is deliberately a second, independent implementation of the client side:
if it agrees with the pool, the pool's byte handling is right.

Usage::

    python tools/stratum_probe.py --host 127.0.0.1 --port 3333 \\
        --payout-address SYoFdo1FJBaDxnBhvURLcVqV64n9eAUxkt

Exit status is 0 when a block was accepted, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

#: Bitcoin's difficulty-1 target, matching the pool's share-difficulty units.
DIFF1_TARGET = 0x00000000FFFF0000_000000000000000000000000000000000000000000000000

#: Fallback assumption when the node cannot be asked for the real target: the
#: pool derives a share target this many times easier than a block.
DEFAULT_SHARE_EASE = 8


def hash256(data: bytes) -> bytes:
    """Bitcoin's double SHA-256, as the miner computes it."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def hash_to_int(block_hash: bytes) -> int:
    """Interpret a block hash as an integer, little-endian, like the node."""
    return int.from_bytes(block_hash, "little")


# ── stratum client ───────────────────────────────────────────────────────


class StratumClient:
    """A minimal newline-delimited JSON Stratum V1 client."""

    def __init__(self, host: str, port: int, timeout: float = 30.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._file = self._sock.makefile("rwb")
        self._next_id = 0

    def close(self) -> None:
        self._file.close()
        self._sock.close()

    def _send(self, method: str, params: list | None = None) -> int:
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or []}
        self._file.write((json.dumps(message) + "\n").encode())
        self._file.flush()
        return self._next_id

    def _read(self) -> dict:
        line = self._file.readline()
        if not line:
            raise ConnectionError("the pool closed the connection")
        return json.loads(line)

    def call(self, method: str, params: list | None = None) -> dict:
        """Send a request and return the response with the matching id."""
        wanted = self._send(method, params)
        for _ in range(50):
            message = self._read()
            if message.get("id") == wanted:
                return message
        raise RuntimeError(f"no reply to {method}")

    def wait_for(self, method: str) -> dict:
        """Read until a notification for *method* arrives."""
        for _ in range(50):
            message = self._read()
            if message.get("method") == method:
                return message
        raise RuntimeError(f"never saw a {method} notification")


@dataclass
class Job:
    job_id: str
    prev_hash: str
    coinbase1: str
    coinbase2: str
    branches: list[str]
    version: str
    nbits: str
    ntime: str


def _job_from(params: list) -> Job:
    """Build a :class:`Job` from a ``mining.notify`` params list."""
    return Job(
        job_id=params[0],
        prev_hash=params[1],
        coinbase1=params[2],
        coinbase2=params[3],
        branches=params[4],
        version=params[5],
        nbits=params[6],
        ntime=params[7],
    )


def coinbase_for(job: Job, extranonce1: str, extranonce2: str) -> bytes:
    """Assemble the coinbase the way an ASIC does."""
    return bytes.fromhex(job.coinbase1 + extranonce1 + extranonce2 + job.coinbase2)


def merkle_root_for(coinbase: bytes, branches: list[str]) -> bytes:
    root = hash256(coinbase)
    for sibling in branches:
        root = hash256(root + bytes.fromhex(sibling))
    return root


def header_for(job: Job, merkle_root: bytes, ntime: int, nonce: int) -> bytes:
    return (
        int(job.version, 16).to_bytes(4, "little")
        + bytes.fromhex(job.prev_hash)
        + merkle_root
        + ntime.to_bytes(4, "little")
        + int(job.nbits, 16).to_bytes(4, "little")
        + nonce.to_bytes(4, "little")
    )


def _grind(args: tuple[bytes, int, int, int]) -> tuple[int, bytes] | None:
    """Search a strided nonce range for a header that beats *target*.

    A module-level function so it can be handed to a process pool.
    """
    prefix, target, start, stride = args
    nonce = start
    end = 0x1_0000_0000
    while nonce < end:
        header = prefix + nonce.to_bytes(4, "little")
        digest = hash256(header)
        if hash_to_int(digest) <= target:
            return nonce, header
        nonce += stride
    return None


def grind(job: Job, merkle_root: bytes, target: int, ntime: int, workers: int) -> tuple[int, bytes]:
    """Find a nonce whose header beats *target*, in parallel."""
    prefix = header_for(job, merkle_root, ntime, 0)[:76]
    if workers <= 1:
        result = _grind((prefix, target, 0, 1))
        if result is None:  # pragma: no cover - the space is astronomically large
            raise RuntimeError("no nonce found")
        return result

    with multiprocessing.Pool(workers) as pool:
        pending = [
            pool.apply_async(_grind, ((prefix, target, i, workers),)) for i in range(workers)
        ]
        while True:
            for task in pending:
                if task.ready():
                    result = task.get()
                    if result is not None:
                        pool.terminate()
                        return result
            time.sleep(0.05)


# ── node queries ─────────────────────────────────────────────────────────


def rpc(url: str, method: str, params: list, timeout: float = 15.0) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    request = urllib.request.Request(
        f"{url.rstrip('/')}/rpc",
        data=body.encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        payload = json.loads(reply.read())
    if payload.get("error"):
        raise RuntimeError(f"{method}: {payload['error']}")
    return payload["result"]


def _block_target(args: argparse.Namespace, difficulty: float) -> int:
    """The target a parent header must beat to produce a ScarletCoin block.

    Prefers the node's own candidate, which is authoritative.  Falls back to
    deriving it from the share difficulty the pool advertised.
    """
    if args.payout_address:
        try:
            candidate = rpc(args.rpc_url, "createauxblock", [args.payout_address])
            return int(str(candidate["target"]), 16)  # type: ignore[index]
        except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
            print(f"  could not read the target from the node ({exc}); deriving it")
    return max(1, int(DIFF1_TARGET / difficulty) // max(1, args.share_ease))


def chain_height(url: str) -> int | None:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/info", timeout=10) as reply:
            return int(json.loads(reply.read())["height"])
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        return None


# ── main ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="pool host")
    parser.add_argument("--port", type=int, default=3333, help="pool Stratum port")
    parser.add_argument("--worker", default="probe", help="worker name to authorize as")
    parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:20332",
        help="ScarletCoin node RPC URL, used to read the exact block target",
    )
    parser.add_argument(
        "--payout-address",
        default="",
        help="SCT address to ask the node for a candidate (any valid address works)",
    )
    parser.add_argument(
        "--share-ease",
        type=int,
        default=DEFAULT_SHARE_EASE,
        help="fallback: how much easier a share is than a block",
    )
    parser.add_argument("--workers", type=int, default=0, help="grinding processes (0 = auto)")
    parser.add_argument(
        "--attempts",
        type=int,
        default=5,
        help="jobs to try before giving up; the tip moves while grinding",
    )
    args = parser.parse_args(argv)

    workers = args.workers or max(1, (multiprocessing.cpu_count() or 2))
    client = StratumClient(args.host, args.port)
    try:
        subscribe = client.call("mining.subscribe", ["stratum-probe/1.0"])
        if subscribe.get("error"):
            print(f"subscribe failed: {subscribe['error']}")
            return 1
        _, extranonce1, extranonce2_size = subscribe["result"]
        print(f"subscribed: extranonce1={extranonce1} extranonce2_size={extranonce2_size}")

        difficulty_msg = client.wait_for("mining.set_difficulty")
        difficulty = float(difficulty_msg["params"][0])
        print(f"share difficulty: {difficulty:g}")

        authorized = client.call("mining.authorize", [args.worker, "x"])
        if authorized.get("result") is not True:
            print(f"authorize failed: {authorized.get('error')}")
            return 1
        print("authorized")

        # The parent chain is simulated, so the job's prevhash has nothing to
        # do with the ScarletCoin tip; the target has to come from the node's
        # own candidate.  Fetch a job, read the target that goes with the tip
        # the node is on, grind, submit, and retry if the tip moved underneath
        # us - which is routine, because native mining keeps producing blocks.
        extranonce2 = "00" * int(extranonce2_size)
        last_error: str = "no attempt made"
        for attempt in range(1, args.attempts + 1):
            notify = client.wait_for("mining.notify")
            job = _job_from(notify["params"])
            print(
                f"attempt {attempt}: job {job.job_id} prevhash={job.prev_hash}"
                f" nbits={job.nbits} ntime={job.ntime}"
            )

            target = _block_target(args, difficulty)
            print(f"  block target {target:064x}")

            height_before = chain_height(args.rpc_url)
            coinbase = coinbase_for(job, extranonce1, extranonce2)
            root = merkle_root_for(coinbase, job.branches)

            started = time.time()
            nonce, _header = grind(job, root, target, int(job.ntime, 16), workers)
            print(f"  found nonce {nonce:08x} in {time.time() - started:.1f}s")

            reply = client.call(
                "mining.submit",
                [args.worker, job.job_id, extranonce2, job.ntime, f"{nonce:08x}"],
            )
            print(f"  submit reply: {reply}")

            if reply.get("result") is not True:
                last_error = str(reply.get("error") or "the pool refused the share")
                print(f"  refused: {last_error}")
                continue

            time.sleep(2.0)
            height_after = chain_height(args.rpc_url)
            print(f"  chain height {height_before} -> {height_after}")
            if (
                height_before is not None
                and height_after is not None
                and height_after > height_before
            ):
                print("RESULT: AuxPoW block accepted and the chain advanced")
                return 0
            last_error = "the share was accepted but no block landed"
            print(f"  {last_error}")

        print(f"RESULT: no block accepted after {args.attempts} attempts ({last_error})")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
