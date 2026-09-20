"""Stratum V1 TCP server for merged-mining Bitcoin ASICs.

Accepts connections from standard SHA-256 ASIC miners (Antminer, Whatsminer,
Avalon, …), hands out jobs whose parent coinbase carries the ScarletCoin
AuxPoW commitment, and submits an AuxPoW proof whenever a share also meets the
ScarletCoin target.

The wire protocol follows the de-facto Stratum V1 convention so stock firmware
works unchanged:

* ``mining.subscribe`` returns ``extranonce1`` and ``extranonce2_size``;
* ``mining.notify`` sends ``prevhash`` and every Merkle branch entry in
  **internal** byte order, which is how the miner writes them into the header;
* the miner builds ``coinbase = coinbase1 || extranonce1 || extranonce2 ||
  coinbase2`` and the pool rebuilds exactly that.

Usage::

    python -m pool.scarlet_pool.server --payout-address S...
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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

__all__ = ["SimulatedParentChain", "StratumServer", "StratumSession", "create_server"]

logger = logging.getLogger(__name__)

#: Default Stratum share difficulty.  ``None`` means "track the chain": the
#: share target is derived from each job's block target, so the submission rate
#: follows the chain as its difficulty moves.  An explicit value pins it in
#: Bitcoin difficulty-1 units.  A fixed value is a trap here, because
#: ScarletCoin's difficulty is orders of magnitude below Bitcoin's difficulty 1
#: and moves fast while the network hashrate is changing.
DEFAULT_SHARE_DIFFICULTY: float | None = None


# ── simulated parent chain (SCT-only mining, testing) ────────────────────


class SimulatedParentChain:
    """A stand-in parent chain for mining ScarletCoin on its own.

    ScarletCoin never inspects the parent chain's state: an AuxPoW proof is
    validated against the parent header's proof of work and the commitment in
    its coinbase, not against a real Bitcoin block.  So a pool that only wants
    to mine SCT can synthesise parent headers, solve them against the
    ScarletCoin target, and discard the parent side entirely.

    Swap in a ``BitcoinCoreClient`` to earn real BTC from the same hashing.
    """

    #: A generous target so the pool's *share* check is not what rejects work;
    #: the ScarletCoin target is the one that matters.
    _EASY_TARGET = 0x7FFFFF0000000000000000000000000000000000000000000000000000000000

    def __init__(self, *, height: int = 800_000) -> None:
        self.height = height
        self.nbits = 0x207FFFFF  # an easy, always-valid compact target
        # Internal byte order: this is what goes into the header and onto the
        # wire, so the pool and the miner agree without any translation.
        self.prev_hash = os.urandom(32).hex()

    def get_template(self) -> ParentTemplate:
        return ParentTemplate(
            version=1,
            prev_hash=self.prev_hash,
            nbits=self.nbits,
            height=self.height,
            coinbase_value=50 * 100_000_000,
            transactions=[],
            target=self._EASY_TARGET,
        )

    def submit_block(self, raw_hex: str) -> str | None:
        """Accept a solved parent block and advance the simulated chain."""
        from scarletcoin.crypto.hashing import hash256

        block_hash = hash256(bytes.fromhex(raw_hex))[::-1].hex()
        self.prev_hash = block_hash[::-1].hex()
        self.height += 1
        return block_hash


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

    # ── lifecycle ──────────────────────────────────────────────────────

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
            pass  # connection closed or timed out
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self._on_disconnect(self)
            with contextlib.suppress(Exception):
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)

    async def _dispatch(self, req: StratumRequest) -> None:
        method = req.method
        if method == "mining.subscribe":
            await self._handle_subscribe(req)
        elif method == "mining.authorize":
            await self._handle_authorize(req)
        elif method == "mining.submit":
            await self._handle_submit(req)
        elif method in {
            "mining.suggest_target",
            "mining.suggest_difficulty",
            "mining.extranonce.subscribe",
        }:
            await self._send_result(req.id, True)
        elif method == "mining.configure":
            # Advertise no optional extensions; miners fall back to V1 basics.
            await self._send_result(req.id, {"version-rolling": False})
        elif method in {"mining.multi_version", "client.get_version"}:
            await self._send_result(req.id, True)
        else:
            await self._send_error(req.id, -32601, f"unknown method {method!r}")

    async def send_job(self, clean: bool = False) -> None:
        """Push the current job to this miner."""
        job = self._manager.current
        if job is None:
            return
        msg = encode_message(
            {
                "id": None,
                "method": "mining.notify",
                "params": [
                    job.job_id,
                    job.parent.prev_hash,  # internal order
                    job.coinbase.coinbase1,
                    job.coinbase.coinbase2,
                    job.merkle_branches,  # internal order
                    f"{job.parent.version:08x}",
                    f"{job.parent.nbits:08x}",
                    f"{job.ntime:08x}",
                    clean,
                ],
            }
        )
        await self._write(msg)

    async def _write(self, payload: str) -> None:
        try:
            self._writer.write(payload.encode())
            await asyncio.wait_for(self._writer.drain(), timeout=5.0)
        except Exception as exc:
            raise StratumError("write failed") from exc

    # ── handlers ──────────────────────────────────────────────────────

    async def _handle_subscribe(self, req: StratumRequest) -> None:
        if len(req.params) >= 1:
            logger.info("miner %s subscribed (agent=%s)", self.address, str(req.params[0])[:80])

        self.subscription_id = f"scarlet-{os.urandom(4).hex()}"
        # One extranonce1 per connection, four bytes, returned to the miner.
        # The pool never puts it into coinbase1: the miner inserts it itself.
        self.extranonce1 = os.urandom(4).hex()
        self.extranonce2_size = 4
        self.subscribed = True

        await self._send_result(
            req.id,
            [
                [
                    ["mining.set_difficulty", self.subscription_id],
                    ["mining.notify", self.subscription_id],
                ],
                self.extranonce1,
                self.extranonce2_size,
            ],
        )
        await self._set_difficulty(self._manager.share_difficulty)

    async def _set_difficulty(self, difficulty: float) -> None:
        await self._write(
            encode_message(
                {
                    "id": None,
                    "method": "mining.set_difficulty",
                    "params": [difficulty],
                }
            )
        )

    async def _handle_authorize(self, req: StratumRequest) -> None:
        if len(req.params) >= 1:
            self.worker_name = str(req.params[0])
        self.authorized = True
        logger.info("worker %s authorized", self.worker_name)
        await self._send_result(req.id, True)
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
            ntime = int(str(req.params[3]), 16)
            nonce = int(str(req.params[4]), 16)
        except (ValueError, TypeError) as exc:
            await self._send_error(req.id, -32602, f"bad params: {exc}")
            return

        share = self._manager.process_share(job_id, self.extranonce1, extranonce2, ntime, nonce)
        if not share.accepted:
            logger.debug("share rejected from %s: %s", worker or self.address, share.reason)
            await self._send_result(req.id, False)
            return

        if share.meets_sct_target:
            logger.info(
                "SCT block candidate from %s! parent=%s",
                worker or self.address,
                share.hash_hex,
            )
            result = self._manager.submit_sct_block(
                job_id, self.extranonce1, extranonce2, ntime, nonce
            )
            if result and result.get("status") == "connected":
                logger.info("SCT block accepted: %s", result.get("hash"))
            else:
                logger.warning("SCT block rejected: %s", result)

        await self._send_result(req.id, True)

    # ── helpers ────────────────────────────────────────────────────────

    async def _send_result(self, req_id: int | None, result: object) -> None:
        await self._write(StratumResponse(result=result, id=req_id).encode() + "\n")

    async def _send_error(self, req_id: int | None, code: int, message: str) -> None:
        await self._write(StratumResponse(error=(code, message, None), id=req_id).encode() + "\n")


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
        self._refresh_task: asyncio.Task | None = None

    # ── public API ─────────────────────────────────────────────────────

    @property
    def sessions(self) -> int:
        return len(self._sessions)

    @property
    def port(self) -> int:
        """The port actually bound (useful when 0 was requested)."""
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        """Start the Stratum server and the job refresh loop."""
        self._server = await asyncio.start_server(self._handle_connection, self._host, self._port)
        addr = self._server.sockets[0].getsockname()
        logger.info("Stratum server listening on %s:%s", addr[0], addr[1])
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
                    "new job %s (sct height=%s, share target=%064x)",
                    job.job_id,
                    job.scarlet.height,
                    self._manager.share_target,
                )
                for session in list(self._sessions):
                    if session.authorized:
                        with contextlib.suppress(Exception):
                            await session.send_job(clean=True)
            except Exception as exc:
                logger.error("job refresh failed: %s", exc)
            for _ in range(int(self._job_interval)):
                if self._stop.is_set():
                    return
                await asyncio.sleep(1.0)


# ── entry point ─────────────────────────────────────────────────────────


def create_server(
    *,
    scarlet_url: str = "http://127.0.0.1:20332",
    scarlet_token: str | None = None,
    scarlet_address: str = "",
    host: str = "0.0.0.0",
    port: int = 3333,
    job_interval: float = 30.0,
    share_difficulty: float | None = DEFAULT_SHARE_DIFFICULTY,
    chain_id: int = 0,
    parent: ParentChainClient | None = None,
) -> StratumServer:
    """Build a Stratum server wired to a ScarletCoin node.

    The parent chain defaults to :class:`SimulatedParentChain`, which mines
    ScarletCoin on its own; pass a real ``BitcoinCoreClient`` for BTC merged
    mining.  Set *chain_id* to refuse to start against the wrong network
    (1 = mainnet, 2 = testnet, 3 = regtest); 0 disables the check.
    """
    scarlet = RpcClient(scarlet_url, token=scarlet_token, timeout=30.0)
    manager = JobManager(
        bitcoin=parent if parent is not None else SimulatedParentChain(),
        scarlet=scarlet,
        payout_address=scarlet_address,
        chain_id=chain_id,
        share_difficulty=share_difficulty,
        coinbase_builder=CoinbaseBuilder(),
    )
    return StratumServer(host=host, port=port, manager=manager, job_interval=job_interval)


def _main() -> None:
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
        help="ScarletCoin RPC bearer token (omit when the node runs --rpc-public-mining)",
    )
    parser.add_argument("--payout-address", default="", help="SCT address for block rewards")
    parser.add_argument("--host", default="0.0.0.0", help="Stratum listen address")
    parser.add_argument("--port", type=int, default=3333, help="Stratum listen port")
    parser.add_argument("--job-interval", type=float, default=30.0, help="Seconds between new jobs")
    parser.add_argument(
        "--share-difficulty",
        type=float,
        default=DEFAULT_SHARE_DIFFICULTY,
        help="Pin the Stratum share difficulty in Bitcoin difficulty-1 units."
        " By default it is derived from the chain target so the share rate"
        " tracks the chain.",
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=1,
        help="Expected AuxPoW chain id: 1=mainnet, 2=testnet, 3=regtest (0 disables the check)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.payout_address:
        parser.error("--payout-address is required: it receives every SCT block reward")

    server = create_server(
        scarlet_url=args.scarlet_url,
        scarlet_token=args.scarlet_token,
        scarlet_address=args.payout_address,
        host=args.host,
        port=args.port,
        job_interval=args.job_interval,
        share_difficulty=args.share_difficulty,
        chain_id=args.chain_id,
    )
    logger.info("ScarletCoin node: %s", args.scarlet_url)
    logger.info("Payout address: %s", args.payout_address)
    logger.info(
        "Share difficulty: %s",
        "auto (derived from the chain target)"
        if args.share_difficulty is None
        else args.share_difficulty,
    )
    logger.info("Expected chain id: %s", args.chain_id)
    logger.info("Miners connect to stratum+tcp://%s:%s", args.host, args.port)
    asyncio.run(server.serve_forever())


if __name__ == "__main__":
    _main()
