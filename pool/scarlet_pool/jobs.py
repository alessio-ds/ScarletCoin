"""Merged-mining job manager.

Orchestrates the flow:
1. Fetch Bitcoin block template (from Bitcoin Core or simulated)
2. Fetch ScarletCoin AuxPoW candidate
3. Build the merged coinbase and Stratum job
4. Detect SCT-valid shares and submit AuxPoW proofs
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol

from scarletcoin.core.auxpow import (
    AuxPoW,
    ParentBlockHeader,
    build_auxpow_commitment,
)
from scarletcoin.net.client import RpcClient

from .coinbase import CoinbaseBuilder, ParentCoinbase

__all__ = ["JobManager", "ParentTemplate", "ScarletTemplate", "ShareResult"]


# ── abstract parent-chain interface ───────────────────────────────────────


class ParentChainClient(Protocol):
    """Interface for a Bitcoin parent-chain RPC client.

    Two implementations exist:
    * :class:`BitcoinCoreClient` — talks to a real ``bitcoind``.
    * :class:`SimulatedParentChain` — fake chain for testing / regtest.
    """

    def get_template(self) -> ParentTemplate:
        """Return the current Bitcoin block template."""

    def submit_block(self, raw_hex: str) -> str | None:
        """Submit a solved block; return txid or ``None``."""


@dataclass(frozen=True)
class ParentTemplate:
    """A Bitcoin block template from the parent chain."""

    version: int
    prev_hash: str  # display-order hex
    nbits: int
    height: int
    coinbase_value: int  # subsidy + fees in satoshis
    transactions: list[str]  # display-order txid hex strings
    target: int  # integer target for share-check


@dataclass(frozen=True)
class ScarletTemplate:
    """A frozen ScarletCoin AuxPoW candidate."""

    aux_hash: str  # display-order hex
    target: int  # integer target
    chain_id: int
    nonce: int  # commitment nonce


@dataclass
class ShareResult:
    """Outcome of checking a submitted share."""

    accepted: bool
    reason: str = ""
    hash_hex: str = ""  # display-order
    meets_sct_target: bool = False
    meets_btc_target: bool = False


# ── job manager ──────────────────────────────────────────────────────────


@dataclass
class _ActiveJob:
    """A currently-active Stratum job, ready for miners."""

    job_id: str
    parent: ParentTemplate
    scarlet: ScarletTemplate
    coinbase: ParentCoinbase
    merkle_branches: list[str]
    created: float


class JobManager:
    """Creates and manages merged-mining jobs."""

    def __init__(
        self,
        *,
        bitcoin: ParentChainClient,
        scarlet: RpcClient,
        payout_address: str,
        chain_id: int,
        target_spacing: int = 60,
        coinbase_builder: CoinbaseBuilder | None = None,
    ) -> None:
        self._btc = bitcoin
        self._scarlet = scarlet
        self._payout_address = payout_address
        self._chain_id = chain_id
        self._target_spacing = target_spacing
        self._coinbase_builder = coinbase_builder or CoinbaseBuilder()

        # Simple payout script: P2PKH to a fixed pubkey hash.
        # In production this would come from the pool's wallet.
        self._payout_script = bytes.fromhex("76a914" + "00" * 20 + "88ac")  # placeholder

        self._current: _ActiveJob | None = None
        self._job_counter: int = 0
        self._extranonce_counter: int = 0

        # Stats
        self.shares_accepted: int = 0
        self.shares_rejected: int = 0
        self.sct_blocks_found: int = 0
        self.sct_blocks_accepted: int = 0

    # ── template rotation ──────────────────────────────────────────────

    def refresh(self) -> _ActiveJob:
        """Fetch fresh templates from both chains and build a new job.

        Returns the new active job. Old jobs are discarded; miners receive a
        ``clean_jobs`` notification.
        """
        parent = self._btc.get_template()
        scarlet_raw = self._scarlet.call("createauxblock", self._payout_address)
        assert isinstance(scarlet_raw, dict)

        scarlet = ScarletTemplate(
            aux_hash=scarlet_raw["hash"],
            target=int(scarlet_raw["target"], 16),
            chain_id=scarlet_raw["chainid"],
            nonce=scarlet_raw["nonce"],
        )

        # Build the AuxPoW commitment
        aux_hash_bytes = bytes.fromhex(scarlet.aux_hash)[::-1]
        commitment = build_auxpow_commitment(aux_hash_bytes, tree_size=1, nonce=scarlet.nonce)

        # Generate extranonce1 for this job
        self._extranonce_counter += 1
        extranonce1 = self._extranonce_counter.to_bytes(4, "big") + os.urandom(2)

        # Build parent coinbase
        coinbase = self._coinbase_builder.build(
            coinbase_value=parent.coinbase_value,
            block_height=parent.height,
            payout_script=self._payout_script,
            aux_commitment=commitment,
            extranonce1=extranonce1,
        )

        # Compute Merkle branches (coinbase is tx0)
        txids = [bytes.fromhex(t)[::-1] for t in parent.transactions]

        # Reconstruct full coinbase to get its hash
        full_cb = bytes.fromhex(coinbase.coinbase1) + bytes.fromhex(coinbase.coinbase2)
        from scarletcoin.crypto.hashing import hash256

        cb_hash = hash256(full_cb)
        all_hashes = [cb_hash, *txids]

        # Merkle branch: path from coinbase (index 0) to root
        branches: list[str] = []
        level = all_hashes
        idx = 0
        while len(level) > 1:
            if len(level) % 2:
                level.append(level[-1])
            sibling_idx = idx ^ 1
            if sibling_idx < len(level):
                branches.append(level[sibling_idx][::-1].hex())  # internal → display
            level = [hash256(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
            idx >>= 1

        self._job_counter += 1
        self._current = _ActiveJob(
            job_id=f"{self._job_counter:08x}",
            parent=parent,
            scarlet=scarlet,
            coinbase=coinbase,
            merkle_branches=branches,
            created=time.time(),
        )
        return self._current

    @property
    def current(self) -> _ActiveJob | None:
        return self._current

    # ── share submission ───────────────────────────────────────────────

    def process_share(
        self, job_id: str, extranonce2_hex: str, ntime: int, nonce: int
    ) -> ShareResult:
        """Validate a submitted share and check for SCT-block eligibility.

        Returns a :class:`ShareResult` with ``meets_sct_target`` set when the
        parent hash satisfies the ScarletCoin target.
        """
        job = self._current
        if job is None or job.job_id != job_id:
            return ShareResult(accepted=False, reason="stale job")

        # Reconstruct the parent header
        cb = job.coinbase
        header_bytes = CoinbaseBuilder.reconstruct_header(
            cb.coinbase1,
            extranonce2_hex,
            cb.coinbase2,
            job.merkle_branches,
            job.parent.prev_hash,
            job.parent.version,
            job.parent.nbits,
            ntime,
            nonce,
        )
        parent_hash = hash256(header_bytes)
        hash_int = int.from_bytes(parent_hash, "little")
        hash_hex = parent_hash[::-1].hex()

        # Check pool share target
        if hash_int > job.parent.target:
            self.shares_rejected += 1
            return ShareResult(
                accepted=False,
                reason="share above target",
                hash_hex=hash_hex,
            )

        self.shares_accepted += 1
        result = ShareResult(
            accepted=True,
            hash_hex=hash_hex,
        )

        # Check ScarletCoin target
        if hash_int <= job.scarlet.target:
            result.meets_sct_target = True
            self.sct_blocks_found += 1

        return result

    def submit_sct_block(
        self, job_id: str, extranonce2_hex: str, ntime: int, nonce: int
    ) -> dict | None:
        """Assemble and submit an AuxPoW proof for a share that met the SCT target.

        Returns the ScarletCoin submission result, or ``None`` on failure.
        """
        job = self._current
        if job is None or job.job_id != job_id:
            return None

        # Reconstruct parent header
        cb = job.coinbase
        header_bytes = CoinbaseBuilder.reconstruct_header(
            cb.coinbase1,
            extranonce2_hex,
            cb.coinbase2,
            job.merkle_branches,
            job.parent.prev_hash,
            job.parent.version,
            job.parent.nbits,
            ntime,
            nonce,
        )

        # Reconstruct full parent coinbase transaction
        parts = [bytes.fromhex(h) for h in (cb.coinbase1, extranonce2_hex, cb.coinbase2)]
        full_cb_bytes = b"".join(parts)
        try:
            from scarletcoin.core.transaction import Transaction

            parent_cb_tx = Transaction.deserialize(full_cb_bytes)
        except Exception:
            return None

        parent_header = ParentBlockHeader.deserialize(header_bytes)

        # Build coinbase Merkle branch (display → internal order)
        cb_branch = tuple(bytes.fromhex(b)[::-1] for b in job.merkle_branches)

        auxpow = AuxPoW(
            coinbase_tx=parent_cb_tx,
            coinbase_merkle_branch=cb_branch,
            coinbase_index=0,
            aux_merkle_branch=(),  # single chain
            aux_chain_index=0,
            parent_header=parent_header,
        )

        result = self._scarlet.call(
            "submitauxblock", job.scarlet.aux_hash, auxpow.serialize().hex()
        )
        if isinstance(result, dict) and result.get("status") == "connected":
            self.sct_blocks_accepted += 1
        return result


# For the reconstruct_header method
from scarletcoin.crypto.hashing import hash256  # noqa: E402
