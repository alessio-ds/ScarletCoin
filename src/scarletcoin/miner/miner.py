"""The miner: ask a node for work, search for a nonce, submit the block.

Mining runs in worker *processes* (not threads), because CPython's global
interpreter lock would otherwise serialise the hashing loop.  With one worker the
search runs in-process, which keeps tests and the GUI simple.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from scarletcoin.core.block import Block
from scarletcoin.core.params import get_params
from scarletcoin.core.template import BlockTemplate
from scarletcoin.crypto.keys import Address, InvalidKeyError
from scarletcoin.miner.solver import NONCE_LIMIT, compile_native, scan_nonces
from scarletcoin.net.client import RpcClient, RpcClientError

__all__ = ["Miner", "MinerStats", "MiningError"]

logger = logging.getLogger(__name__)

#: Aim for rounds of roughly this length, so the miner reacts quickly to new blocks.
ROUND_SECONDS = 1.0
_MIN_CHUNK = 1 << 12
_MAX_CHUNK = 1 << 22


class MiningError(Exception):
    """Raised when mining cannot start or continue.

    ``fatal`` marks problems that retrying cannot fix, such as a payout address
    that does not belong to the node's network.
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def _scan_task(job: tuple[bytes, int, int, int]) -> tuple[int | None, int]:
    """Worker entry point: scan a slice of the nonce space."""
    header, target, start, count = job
    result = scan_nonces(header, target, start=start, count=count)
    return result.nonce, result.hashes


@dataclass
class MinerStats:
    """Live mining statistics."""

    hashes: int = 0
    blocks_found: int = 0
    blocks_accepted: int = 0
    blocks_rejected: int = 0
    started: float = field(default_factory=time.time)
    last_rate: float = 0.0
    height: int = 0
    difficulty: float = 0.0

    @property
    def elapsed(self) -> float:
        """Seconds since mining started."""
        return max(1e-9, time.time() - self.started)

    @property
    def average_rate(self) -> float:
        """Average hashes per second since the start."""
        return self.hashes / self.elapsed

    def to_dict(self) -> dict:
        """Return a JSON-friendly snapshot."""
        return {
            "hashes": self.hashes,
            "blocks_found": self.blocks_found,
            "blocks_accepted": self.blocks_accepted,
            "blocks_rejected": self.blocks_rejected,
            "hash_rate": round(self.last_rate, 1),
            "average_hash_rate": round(self.average_rate, 1),
            "elapsed": round(self.elapsed, 1),
            "height": self.height,
            "difficulty": self.difficulty,
        }


class Miner:
    """Solo miner working against one node."""

    def __init__(
        self,
        client: RpcClient,
        address: str,
        *,
        workers: int = 1,
        refresh_seconds: float = 15.0,
        max_rate: float | None = None,
        tag: bytes = b"scarlet-miner",
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.client = client
        self.address_text = address
        self.workers = max(1, int(workers))
        self.refresh_seconds = max(1.0, float(refresh_seconds))
        self.max_rate = max_rate if max_rate is not None and max_rate > 0 else None
        self.tag = tag[:32]
        self.on_event = on_event
        self.stats = MinerStats()
        self._stop = threading.Event()
        self._chunk = 1 << 16
        self._round = 0
        self._pubkey_hash: bytes | None = None

    # ------------------------------------------------------------------ lifecycle

    def stop(self) -> None:
        """Ask the mining loop to finish after the current round."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        """``True`` once :meth:`stop` has been called."""
        return self._stop.is_set()

    def _emit(self, kind: str, **payload: object) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, dict(payload))
            except Exception:  # pragma: no cover - a bad callback must not stop mining
                logger.exception("miner event handler failed")

    # -------------------------------------------------------------------- helpers

    def _resolve_address(self, network: str) -> bytes:
        """Check the payout address belongs to the node's network."""
        version = get_params(network).address_version
        try:
            address = Address.decode(self.address_text, expected_version=version)
        except InvalidKeyError as exc:
            raise MiningError(
                f"{self.address_text!r} is not a valid {network} address: {exc}", fatal=True
            ) from exc
        return address.hash

    def _fetch_template(self) -> BlockTemplate:
        try:
            data = self.client.getblocktemplate()
        except RpcClientError as exc:
            raise MiningError(f"cannot get work from the node: {exc}") from exc
        if self._pubkey_hash is None:
            self._pubkey_hash = self._resolve_address(str(data.get("network", "mainnet")))
        return BlockTemplate.from_dict(data)

    def _extra_nonce(self) -> bytes:
        self._round += 1
        return self._round.to_bytes(8, "little") + os.urandom(4) + self.tag

    def _submit(self, block: Block) -> bool:
        self.stats.blocks_found += 1
        self._emit("found", hash=block.hash_hex(), height=None)
        try:
            result = self.client.submitblock(block.serialize().hex())
        except RpcClientError as exc:
            self.stats.blocks_rejected += 1
            logger.warning("the node rejected our block: %s", exc)
            self._emit("rejected", hash=block.hash_hex(), reason=str(exc))
            return False
        self.stats.blocks_accepted += 1
        logger.info("mined block %s at height %s", result.get("hash"), result.get("height"))
        self._emit("accepted", hash=block.hash_hex(), height=result.get("height"))
        return True

    def _tune_chunk(self, seconds: float) -> None:
        """Keep rounds close to :data:`ROUND_SECONDS` long."""
        if seconds <= 0:
            return
        factor = ROUND_SECONDS / seconds
        scaled = self._chunk * min(4.0, max(0.25, factor))
        self._chunk = int(min(_MAX_CHUNK, max(_MIN_CHUNK, scaled)))

    # ------------------------------------------------------------------- main loop

    def run(self, max_blocks: int | None = None) -> MinerStats:
        """Mine until stopped (or until ``max_blocks`` blocks have been accepted)."""
        compile_native()
        pool = multiprocessing.Pool(self.workers) if self.workers > 1 else None
        try:
            while not self.stopping:
                if max_blocks is not None and self.stats.blocks_accepted >= max_blocks:
                    return self.stats
                try:
                    template = self._fetch_template()
                except MiningError as exc:
                    self._emit("error", message=str(exc))
                    if exc.fatal:
                        raise
                    logger.warning("%s", exc)
                    if self._stop.wait(5.0):
                        break
                    continue
                self.stats.height = template.height
                self._emit("template", height=template.height, bits=f"{template.bits:#010x}")
                self._mine_template(template, pool)
        finally:
            if pool is not None:
                pool.terminate()
                pool.join()
        return self.stats

    def _mine_template(self, template: BlockTemplate, pool) -> None:
        """Search for a solution to one template until it expires."""
        assert self._pubkey_hash is not None
        candidate = template.build_block(
            pubkey_hash=self._pubkey_hash,
            extra=self._extra_nonce(),
            timestamp=int(time.time()),
        )
        header = candidate.header.serialize()
        target = template.target
        deadline = time.time() + self.refresh_seconds
        nonce = 0

        while not self.stopping and nonce < NONCE_LIMIT and time.time() < deadline:
            chunk = self._chunk
            began = time.perf_counter()
            found: int | None = None
            hashes = 0

            if pool is None:
                result = scan_nonces(header, target, start=nonce, count=chunk)
                found, hashes = result.nonce, result.hashes
                nonce += chunk
            else:
                jobs = [
                    (header, target, nonce + index * chunk, chunk) for index in range(self.workers)
                ]
                nonce += chunk * self.workers
                for candidate_nonce, count in pool.imap_unordered(_scan_task, jobs):
                    hashes += count
                    if candidate_nonce is not None and found is None:
                        found = candidate_nonce

            seconds = time.perf_counter() - began
            self.stats.hashes += hashes
            self.stats.last_rate = hashes / seconds if seconds > 0 else 0.0
            self._tune_chunk(seconds)
            self._emit("progress", hashes=hashes, rate=self.stats.last_rate)

            if self.max_rate is not None and seconds > 0:
                # Idle the workers until the average rate drops to the cap.
                idle = hashes / self.max_rate - seconds
                if idle > 0 and self._stop.wait(min(idle, 1.0)):
                    break

            if found is not None:
                solved = candidate.with_header(candidate.header.with_nonce(found))
                self._submit(solved)
                return
