"""Stratum V1 TCP server for merged-mining Bitcoin ASICs.

Accepts connections from standard SHA-256 ASIC miners (Antminer, Whatsminer, …),
hands out merged-mining jobs that include the ScarletCoin AuxPoW commitment, and
detects when a submitted share also satisfies the ScarletCoin target.

Usage (regtest / local dev)::

    python -m pool.scarlet_pool.server

Requires a ScarletCoin node and a Bitcoin parent-chain source.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable

from scarletcoin.net.client import RpcClient

from .coinbase import CoinbaseBuilder
from .jobs import JobManager, ParentChainClient, ParentTemplate
from .stratum import (
    StratumError,
    StratumRequest,
    StratumResponse,
    encode_message,
    read_message,
)

__all__ = ["StratumServer", "StratumSession", "create_server"]

logger = logging.getLogger(__name__)


# ── simulated parent chain (regtest / testing) ───────────────────────────


class SimulatedParentChain:
    """A fake Bitcoin chain that generates solveable blocks for testing.

    Replace with :class:`BitcoinCoreClient` for production.
    """

    def __init__(self) -> None:
        self.height = 800_000
        self.prev_hash = os.urandom(32)[::-1].hex()
        self.nbits = 0x207FFFFF  # easy target

    def get_template(self) -> ParentTemplate:
        return ParentTemplate(
            version=1,
            prev_hash=self.prev_hash,
            nbits=self.nbits,
            height=self.height,
            coinbase_value=50 * 100_000_000,
            transactions=[],  # no extra txns for simplicity
            target=0x7FFFFF0000000000000000000000000000000000000000000000000000000000,
        )

    def submit_block(self, raw_hex: str) -> str | None:
        from scarletcoin.crypto.hashing import hash256

        blk_hash = hash256(bytes.fromhex(raw_hex))[::-1].hex()
        self.prev_hash = blk_hash
        self.height += 1
        return blk_hash


# ── Stratum session (one per connected miner) ────────────────────────────


class StratumSession:
    """One connected miner."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        manager: JobManager,
        on_disconnect: Callable[[StratumSession], None],
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._manager = manager
        self._on_disconnect = on_disconnect

        self.worker_name: str = "unknown"
        self.address: str = writer.get_extra_info("peername", ("?", 0))[0]
        self.subscribed: bool = False
        self.authorized: bool = False
        self.extranonce1: str = ""
        self.extranonce2_size: int = 4
        self.subscription_id: str = ""

    async def run(self) -> None:
        """Read-submit loop for one miner."""
        try:
            while True:
                line = await read_message(self._reader)
                try:
                    req = StratumRequest.parse(line)
                except StratumError as exc:
                    await self._send_error(None, -32700, str(exc))
                    continue
                await self._dispatch(req)
        except StratumError:
            pass  # connection closed
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self._on_disconnect(self)
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            except Exception:
                pass

    async def _dispatch(self, req: StratumRequest) -> None:
        method = req.method
        if method == "mining.subscribe":
            await self._handle_subscribe(req)
        elif method == "mining.authorize":
            await self._handle_authorize(req)
        elif method == "mining.submit":
            await self._handle_submit(req)
        elif method == "mining.suggest_target" or method == "mining.suggest_difficulty":
            await self._send_result(req.id, True)
        else:
            await self._send_error(req.id, -32601, f"unknown method {method!r}")

    async def send_job(self, clean: bool = False) -> None:
        """Push a new mining job to this miner."""
        job = self._manager.current
        if job is None:
            return
        msg = encode_message(
            {
                "id": None,
                "method": "mining.notify",
                "params": [
                    job.job_id,
                    job.parent.prev_hash,
                    job.coinbase.coinbase1,
                    job.coinbase.coinbase2,
                    job.merkle_branches,
                    f"{job.parent.version:08x}",
                    f"{job.parent.nbits:08x}",
                    f"{int(time.time()):08x}",
                    clean,
                ],
            }
        )
        try:
            self._writer.write(msg.encode())
            await asyncio.wait_for(self._writer.drain(), timeout=5.0)
        except Exception as exc:
            raise StratumError("write failed") from exc

    # ── handlers ──────────────────────────────────────────────────────

    async def _handle_subscribe(self, req: StratumRequest) -> None:
        if len(req.params) >= 1:
            user_agent = str(req.params[0])
            logger.info("miner %s subscribed (agent=%s)", self.address, user_agent[:80])

        self.subscription_id = f"scarlet-{os.urandom(4).hex()}"
        self.extranonce1 = os.urandom(4).hex()
        self.extranonce2_size = 4
        self.subscribed = True

        # Stratum subscribe response
        result = [
            [
                ["mining.set_difficulty", self.subscription_id],
                ["mining.notify", self.subscription_id],
            ],
            self.extranonce1,
            self.extranonce2_size,
        ]
        await self._send_result(req.id, result)
        # Set initial difficulty
        diff_msg = encode_message(
            {
                "id": None,
                "method": "mining.set_difficulty",
                "params": [256.0],
            }
        )
        self._writer.write(diff_msg.encode())

    async def _handle_authorize(self, req: StratumRequest) -> None:
        if len(req.params) >= 1:
            self.worker_name = str(req.params[0])
        self.authorized = True
        logger.info("worker %s authorized", self.worker_name)
        await self._send_result(req.id, True)
        # Send the current job
        if self._manager.current is not None:
            await self.send_job(clean=True)

    async def _handle_submit(self, req: StratumRequest) -> None:
        if not self.authorized:
            await self._send_error(req.id, -32003, "not authorized")
            return
        if len(req.params) < 5:
            await self._send_error(req.id, -32602, "missing params")
            return
        try:
            worker = str(req.params[0])
            job_id = str(req.params[1])
            extranonce2 = str(req.params[2])
            ntime = int(req.params[3], 16) if isinstance(req.params[3], str) else int(req.params[3])
            nonce = int(req.params[4], 16) if isinstance(req.params[4], str) else int(req.params[4])
        except (ValueError, TypeError) as exc:
            await self._send_error(req.id, -32602, f"bad params: {exc}")
            return

        share = self._manager.process_share(job_id, extranonce2, ntime, nonce)
        if not share.accepted:
            await self._send_result(req.id, False)
            return

        # Check if this share meets the ScarletCoin target
        if share.meets_sct_target:
            logger.info(
                "SCT block candidate from %s! hash=%s",
                worker or self.address,
                share.hash_hex,
            )
            result = self._manager.submit_sct_block(job_id, extranonce2, ntime, nonce)
            if result and result.get("status") == "connected":
                logger.info("SCT block accepted: %s", result.get("hash"))
            else:
                logger.warning("SCT block rejected: %s", result)

        await self._send_result(req.id, True)

    # ── helpers ────────────────────────────────────────────────────────

    async def _send_result(self, req_id: int | None, result: object) -> None:
        resp = StratumResponse(result=result, id=req_id)
        self._writer.write(resp.encode().encode() + b"\n")

    async def _send_error(self, req_id: int | None, code: int, message: str) -> None:
        resp = StratumResponse(error=(code, message, None), id=req_id)
        self._writer.write(resp.encode().encode() + b"\n")


# ── server ──────────────────────────────────────────────────────────────


class StratumServer:
    """A Stratum V1 server for merged-mining ASIC miners."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 3333,
        manager: JobManager,
        job_interval: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._manager = manager
        self._job_interval = max(5.0, float(job_interval))
        self._sessions: set[StratumSession] = set()
        self._server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()

    # ── public API ─────────────────────────────────────────────────────

    @property
    def sessions(self) -> int:
        return len(self._sessions)

    async def start(self) -> None:
        """Start the Stratum server and job refresh loop."""
        self._server = await asyncio.start_server(self._handle_connection, self._host, self._port)
        addr = self._server.sockets[0].getsockname()
        logger.info("Stratum server listening on %s:%s", addr[0], addr[1])

        # Run job refresh loop in background
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Stop the server and disconnect all miners."""
        self._stop.set()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for session in list(self._sessions):
            with contextlib.suppress(Exception):
                session._writer.close()
        self._sessions.clear()

    async def serve_forever(self) -> None:
        """Start and run until stopped."""
        await self.start()
        await self._stop.wait()

    # ── internals ──────────────────────────────────────────────────────

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session = StratumSession(reader, writer, self._manager, self._on_disconnect)
        self._sessions.add(session)
        logger.info("miner connected from %s (total: %s)", session.address, len(self._sessions))
        try:
            await session.run()
        except Exception as exc:
            logger.debug("session error from %s: %s", session.address, exc)

    def _on_disconnect(self, session: StratumSession) -> None:
        self._sessions.discard(session)
        logger.info("miner disconnected from %s (total: %s)", session.address, len(self._sessions))

    async def _refresh_loop(self) -> None:
        """Periodically refresh templates and push new jobs."""
        while not self._stop.is_set():
            try:
                job = self._manager.refresh()
                logger.debug(
                    "new job %s (height=%s, target=%s)",
                    job.job_id,
                    job.parent.height,
                    f"{job.scarlet.target:064x}"[:16],
                )
                # Push to all sessions
                for session in list(self._sessions):
                    if session.authorized:
                        with contextlib.suppress(Exception):
                            await session.send_job(clean=True)
            except Exception as exc:
                logger.error("job refresh failed: %s", exc)
            # Sleep, checking stop periodically
            for _ in range(int(self._job_interval)):
                if self._stop.is_set():
                    return
                await asyncio.sleep(1.0)


# ── entry point ─────────────────────────────────────────────────────────


def create_server(
    *,
    scarlet_url: str = "http://127.0.0.1:40332",
    scarlet_token: str | None = None,
    scarlet_address: str = "",
    host: str = "0.0.0.0",
    port: int = 3333,
    chain_id: int = 3,
    job_interval: float = 30.0,
) -> StratumServer:
    """Factory: build a Stratum server wired to a ScarletCoin node.

    Uses :class:`SimulatedParentChain` for the parent Bitcoin side;
    swap with a real ``bitcoind`` RPC client for production.
    """
    scarlet = RpcClient(scarlet_url, token=scarlet_token, timeout=30.0)
    parent: ParentChainClient = SimulatedParentChain()

    manager = JobManager(
        bitcoin=parent,
        scarlet=scarlet,
        payout_address=scarlet_address,
        chain_id=chain_id,
        coinbase_builder=CoinbaseBuilder(),
    )

    return StratumServer(host=host, port=port, manager=manager, job_interval=job_interval)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ScarletCoin merged-mining Stratum bridge")
    parser.add_argument(
        "--scarlet-url",
        default="http://127.0.0.1:20332",
        help="ScarletCoin node RPC URL (default: http://127.0.0.1:20332)",
    )
    parser.add_argument(
        "--scarlet-token",
        default=None,
        help="ScarletCoin RPC bearer token (omit if node runs without one)",
    )
    parser.add_argument(
        "--payout-address",
        default="",
        help="SCT address that receives block rewards",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Stratum listen address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3333,
        help="Stratum listen port (default: 3333)",
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=1,
        help="AuxPoW chain ID (1=mainnet, 2=testnet, 3=regtest; default: 1)",
    )
    parser.add_argument(
        "--job-interval",
        type=float,
        default=30.0,
        help="Seconds between template refresh / new job broadcast (default: 30)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = create_server(
        scarlet_url=args.scarlet_url,
        scarlet_token=args.scarlet_token,
        scarlet_address=args.payout_address,
        host=args.host,
        port=args.port,
        chain_id=args.chain_id,
        job_interval=args.job_interval,
    )
    print(f"Stratum server starting on {args.host}:{args.port}")
    print(f"ScarletCoin node: {args.scarlet_url}")
    print(f"Payout address: {args.payout_address or '(none - rewards burned)'}")
    print(f"Chain ID: {args.chain_id}")
    print(f"Connect your ASIC miners to stratum+tcp://<this-host>:{args.port}")
    asyncio.run(server.serve_forever())
