"""Merged-mining job manager.

Orchestrates the flow:

1. fetch a ScarletCoin AuxPoW candidate (``createauxblock``),
2. fetch a parent-chain block template (Bitcoin Core, or the simulated chain),
3. build the merged parent coinbase and the Stratum job,
4. check submitted shares and, when one meets the ScarletCoin target,
   assemble an AuxPoW proof and submit it (``submitauxblock``).

Byte order: every 32-byte hash handed to a miner, and every hash read back
from one, is in **internal** (little-endian) order — the order it occupies in a
serialised block header.  That is the Stratum convention, so stock ASIC
firmware needs no translation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final, Protocol

from scarletcoin.core.auxpow import (
    AuxPoW,
    ParentBlockHeader,
    build_auxpow_commitment,
)
from scarletcoin.crypto.hashing import hash256
from scarletcoin.net.client import RpcClient, RpcClientError

from .coinbase import (
    DEFAULT_PARENT_PAYOUT_HASH,
    CoinbaseBuilder,
    ParentCoinbase,
    coinbase_merkle_branch,
    parse_coinbase_body,
)

__all__ = [
    "JobManager",
    "ParentChainClient",
    "ParentTemplate",
    "ScarletTemplate",
    "ShareResult",
]

#: Bitcoin's difficulty-1 target, used to convert a Stratum difficulty into a
#: share target.  ``target = _DIFF1_TARGET // difficulty``.
_DIFF1_TARGET: Final[int] = 0x00000000FFFF0000_000000000000000000000000000000000000000000000000

#: How much easier than a block a share should be when the share difficulty is
#: derived automatically.  One share in eight is a block, which is enough to
#: keep a miner busy without drowning the pool in submissions.
DEFAULT_SHARE_EASE: Final[int] = 8


# ── abstract parent-chain interface ───────────────────────────────────────


class ParentChainClient(Protocol):
    """Interface for a Bitcoin parent-chain client.

    Two implementations exist:

    * :class:`~pool.scarlet_pool.server.SimulatedParentChain` — a fake chain
      used for testing and for mining ScarletCoin on its own.
    * ``BitcoinCoreClient`` — talks to a real ``bitcoind`` for genuine BTC
      merged mining (not implemented yet).
    """

    def get_template(self) -> ParentTemplate:
        """Return the current parent-chain block template."""

    def submit_block(self, raw_hex: str) -> str | None:
        """Submit a solved parent block; return its hash or ``None``."""


@dataclass(frozen=True)
class ParentTemplate:
    """A parent-chain block template."""

    version: int
    prev_hash: str
    """Previous parent block hash in **internal** byte order (as it appears in
    the header, and as Stratum puts it on the wire)."""
    nbits: int
    height: int
    coinbase_value: int
    """Subsidy plus fees, in satoshis."""
    transactions: list[str]
    """Non-coinbase transaction ids in **display** order (as ``bitcoind``
    reports them)."""
    target: int
    """Integer target a parent block must not exceed (for the BTC side)."""


@dataclass(frozen=True)
class ScarletTemplate:
    """A frozen ScarletCoin AuxPoW candidate."""

    aux_hash: str
    """ScarletCoin block hash, display order."""
    target: int
    """Integer target the parent proof must not exceed."""
    chain_id: int
    nonce: int
    """Commitment nonce for the deterministic aux-index calculation."""
    height: int


@dataclass
class ShareResult:
    """Outcome of checking a submitted share."""

    accepted: bool
    reason: str = ""
    hash_hex: str = ""
    """Parent header hash, display order."""
    meets_sct_target: bool = False
    meets_btc_target: bool = False


# ── job manager ──────────────────────────────────────────────────────────


@dataclass
class _ActiveJob:
    """A currently-active Stratum job."""

    job_id: str
    parent: ParentTemplate
    scarlet: ScarletTemplate
    coinbase: ParentCoinbase
    merkle_branches: list[str]
    """Merkle branch in internal byte order, as hex."""
    created: float
    ntime: int
    """Header timestamp advertised to miners."""
    clean: bool = True


class JobManager:
    """Creates and manages merged-mining jobs."""

    def __init__(
        self,
        *,
        bitcoin: ParentChainClient,
        scarlet: RpcClient,
        payout_address: str,
        chain_id: int,
        share_difficulty: float | None = None,
        parent_payout_hash: bytes = DEFAULT_PARENT_PAYOUT_HASH,
        coinbase_builder: CoinbaseBuilder | None = None,
    ) -> None:
        self._btc = bitcoin
        self._scarlet = scarlet
        self._payout_address = payout_address
        self._chain_id = chain_id
        self._coinbase_builder = coinbase_builder or CoinbaseBuilder()
        self._parent_payout_hash = parent_payout_hash

        if share_difficulty is not None and share_difficulty <= 0:
            raise ValueError("share difficulty must be positive")
        #: ``None`` means "derive it from the chain target each job" (see
        #: :attr:`share_target`).  An explicit value pins it instead.
        self._fixed_share_target: int | None = (
            None
            if share_difficulty is None
            else max(1, int(_DIFF1_TARGET / float(share_difficulty)))
        )

        self._current: _ActiveJob | None = None
        self._job_counter: int = 0

        # Stats
        self.shares_accepted: int = 0
        self.shares_rejected: int = 0
        self.sct_blocks_found: int = 0
        self.sct_blocks_accepted: int = 0
        self.sct_blocks_rejected: int = 0

    # ── share target ───────────────────────────────────────────────────

    @property
    def share_target(self) -> int:
        """The target a submitted share must beat.

        Pinned when the operator passed an explicit share difficulty.
        Otherwise it is derived from the current block target, so the share
        rate follows the chain as its difficulty moves.  Without that, a
        fixed difficulty is either so easy that an ASIC floods the pool with
        tens of thousands of submissions a second, or so hard that a small
        miner never submits anything.
        """
        if self._fixed_share_target is not None:
            return self._fixed_share_target
        job = self._current
        if job is None:
            return _DIFF1_TARGET
        return max(1, job.scarlet.target * DEFAULT_SHARE_EASE)

    @share_target.setter
    def share_target(self, value: int) -> None:
        """Pin the share target, bypassing the derived value."""
        self._fixed_share_target = max(1, int(value))

    @property
    def share_difficulty(self) -> float:
        """The current share target as a Bitcoin difficulty, for the miner."""
        return _DIFF1_TARGET / self.share_target

    # ── template rotation ──────────────────────────────────────────────

    def refresh(self, payout_address: str | None = None) -> _ActiveJob:
        """Fetch fresh templates from both chains and build a new job.

        ``payout_address`` is the address that will receive the block reward if
        this job wins.  A Stratum miner cannot choose its own coinbase, so the
        pool has to build one per miner for each miner to be paid.

        Returns the new job.  Miners receive a ``clean_jobs`` notification
        telling them to drop everything older.
        """
        address = payout_address or self._payout_address
        scarlet_raw = self._scarlet.call("createauxblock", address)
        if not isinstance(scarlet_raw, dict):
            raise RuntimeError(f"unexpected createauxblock reply: {scarlet_raw!r}")

        parent = self._btc.get_template()

        scarlet = ScarletTemplate(
            aux_hash=str(scarlet_raw["hash"]),
            target=int(str(scarlet_raw["target"]), 16),
            chain_id=int(scarlet_raw["chainid"]),
            nonce=int(scarlet_raw["nonce"]),
            height=int(scarlet_raw["height"]),
        )

        # A pool pointed at the wrong network would build proofs the node
        # rejects; fail loudly instead of silently burning hashpower.
        if self._chain_id and scarlet.chain_id != self._chain_id:
            raise RuntimeError(
                f"ScarletCoin node reports chain id {scarlet.chain_id}, "
                f"but this pool was started for chain id {self._chain_id}"
            )

        # The AuxPoW commitment goes into the parent coinbase's coinbase_data.
        aux_hash_internal = bytes.fromhex(scarlet.aux_hash)[::-1]
        commitment = build_auxpow_commitment(aux_hash_internal, tree_size=1, nonce=scarlet.nonce)

        coinbase = self._coinbase_builder.build(
            coinbase_value=parent.coinbase_value,
            block_height=parent.height,
            payout_hash=self._parent_payout_hash,
            aux_commitment=commitment,
        )

        # The coinbase is leaf 0, so its Merkle branch depends only on the
        # other transactions and can be computed before any extranonce exists.
        txids_internal = [bytes.fromhex(t)[::-1] for t in parent.transactions]
        branches = [b.hex() for b in coinbase_merkle_branch(txids_internal)]

        self._job_counter += 1
        self._current = _ActiveJob(
            job_id=f"{self._job_counter:08x}",
            parent=parent,
            scarlet=scarlet,
            coinbase=coinbase,
            merkle_branches=branches,
            created=time.time(),
            ntime=int(time.time()),
        )
        return self._current

    @property
    def current(self) -> _ActiveJob | None:
        return self._current

    # ── share submission ───────────────────────────────────────────────

    def _parent_header(
        self,
        job: _ActiveJob,
        extranonce1_hex: str,
        extranonce2_hex: str,
        ntime: int,
        nonce: int,
    ) -> bytes:
        return CoinbaseBuilder.reconstruct_header(
            job.coinbase.coinbase1,
            extranonce1_hex,
            extranonce2_hex,
            job.coinbase.coinbase2,
            job.merkle_branches,
            job.parent.prev_hash,
            job.parent.version,
            job.parent.nbits,
            ntime,
            nonce,
        )

    def process_share(
        self,
        job: _ActiveJob,
        extranonce1_hex: str,
        extranonce2_hex: str,
        ntime: int,
        nonce: int,
    ) -> ShareResult:
        """Validate a submitted share and check for block eligibility.

        ``job`` is the job the submitting session was given, which is not
        necessarily the most recently built one: every miner gets its own job so
        that it can be paid its own address.
        """
        if len(extranonce2_hex) != job.coinbase.extranonce2_size * 2:
            self.shares_rejected += 1
            return ShareResult(accepted=False, reason="bad extranonce2 size")

        header_bytes = self._parent_header(job, extranonce1_hex, extranonce2_hex, ntime, nonce)
        parent_hash = hash256(header_bytes)
        hash_int = int.from_bytes(parent_hash, "little")
        hash_hex = parent_hash[::-1].hex()

        if hash_int > self.share_target:
            self.shares_rejected += 1
            return ShareResult(accepted=False, reason="share above target", hash_hex=hash_hex)

        self.shares_accepted += 1
        result = ShareResult(accepted=True, hash_hex=hash_hex)
        if hash_int <= job.scarlet.target:
            result.meets_sct_target = True
            self.sct_blocks_found += 1
        if hash_int <= job.parent.target:
            result.meets_btc_target = True
        return result

    def submit_sct_block(
        self,
        job: _ActiveJob,
        extranonce1_hex: str,
        extranonce2_hex: str,
        ntime: int,
        nonce: int,
    ) -> dict | None:
        """Assemble and submit an AuxPoW proof for a share that met the SCT target."""
        header_bytes = self._parent_header(job, extranonce1_hex, extranonce2_hex, ntime, nonce)

        parts = (
            job.coinbase.coinbase1,
            extranonce1_hex,
            extranonce2_hex,
            job.coinbase.coinbase2,
        )
        body = b"".join(bytes.fromhex(part) for part in parts)
        try:
            parent_coinbase_tx = parse_coinbase_body(body)
        except Exception:
            self.sct_blocks_rejected += 1
            return None

        auxpow = AuxPoW(
            coinbase_tx=parent_coinbase_tx,
            coinbase_merkle_branch=tuple(bytes.fromhex(b) for b in job.merkle_branches),
            coinbase_index=0,
            aux_merkle_branch=(),  # a single auxiliary chain: the root is the hash
            aux_chain_index=0,
            parent_header=ParentBlockHeader.deserialize(header_bytes),
        )

        try:
            result = self._scarlet.call(
                "submitauxblock", job.scarlet.aux_hash, auxpow.serialize().hex()
            )
        except RpcClientError as exc:
            # Routine: the tip moved between mining this job and submitting it,
            # so the node no longer holds the candidate.  Report it instead of
            # letting it escape, which would drop the miner's connection.
            self.sct_blocks_rejected += 1
            return {"status": "rejected", "reason": str(exc)}
        if isinstance(result, dict) and result.get("status") == "connected":
            self.sct_blocks_accepted += 1
        else:
            self.sct_blocks_rejected += 1
        return result
