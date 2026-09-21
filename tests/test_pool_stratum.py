"""Tests for the Stratum V1 merged-mining pool bridge.

These cover the parts that must be exactly right for stock ASIC firmware to
work: the coinbase split around the extranonces, the byte order of everything
sent in ``mining.notify``, the Merkle branch, and the full
share -> AuxPoW -> accepted-block path.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from pool.scarlet_pool.coinbase import (
    MAX_COINBASE_DATA,
    CoinbaseBuilder,
    coinbase_merkle_branch,
    parse_coinbase_body,
)
from pool.scarlet_pool.jobs import (
    _DIFF1_TARGET,
    DEFAULT_SHARE_EASE,
    JobManager,
)
from pool.scarlet_pool.server import SimulatedParentChain, StratumServer

from scarletcoin.core.block import merkle_root
from scarletcoin.core.coinbase import build_coinbase
from scarletcoin.core.serialize import Writer
from scarletcoin.crypto.hashing import hash256
from scarletcoin.crypto.keys import Address
from scarletcoin.net.client import RpcClientError

# ------------------------------------------------------------------ helpers

PAYOUT_HASH = b"\x11" * 20
COMMITMENT = b"\xfa\xbemmx" + b"\x22" * 32


def _fold_branch(coinbase_hash: bytes, branches: list[bytes]) -> bytes:
    root = coinbase_hash
    for sibling in branches:
        root = hash256(root + sibling)
    return root


def _build_header(
    *,
    version: int,
    prev_hash_internal: bytes,
    merkle_root_internal: bytes,
    ntime: int,
    nbits: int,
    nonce: int,
) -> bytes:
    return (
        Writer()
        .uint32(version)
        .hash32(prev_hash_internal)
        .hash32(merkle_root_internal)
        .uint32(ntime)
        .uint32(nbits)
        .uint32(nonce)
        .getvalue()
    )


def _miner_coinbase(coinbase1: str, en1: str, en2: str, coinbase2: str) -> bytes:
    """Exactly what a Stratum miner assembles."""
    return bytes.fromhex(coinbase1 + en1 + en2 + coinbase2)


def _solve(
    *,
    version: int,
    prev_hash: bytes,
    coinbase: bytes,
    branches: list[str],
    ntime: int,
    nbits: int,
    target: int,
    limit: int = 100_000,
) -> tuple[int, bytes]:
    """Search for a nonce whose parent header hash beats *target*."""
    root = _fold_branch(hash256(coinbase), [bytes.fromhex(b) for b in branches])
    for nonce in range(limit):
        header = _build_header(
            version=version,
            prev_hash_internal=prev_hash,
            merkle_root_internal=root,
            ntime=ntime,
            nbits=nbits,
            nonce=nonce,
        )
        if int.from_bytes(hash256(header), "little") <= target:
            return nonce, header
    raise AssertionError("no nonce found within the limit")


def _manager(node, client, address, **kwargs) -> JobManager:
    return JobManager(
        bitcoin=SimulatedParentChain(),
        scarlet=client,
        payout_address=address,
        chain_id=node.params.auxpow_chain_id,
        share_difficulty=kwargs.pop("share_difficulty", 1e-9),
        **kwargs,
    )


# ------------------------------------------------------------ coinbase layout


class TestCoinbaseLayout:
    def _build(self, **kwargs):
        params = {
            "coinbase_value": 50 * 10**8,
            "block_height": 46894,
            "payout_hash": PAYOUT_HASH,
            "aux_commitment": COMMITMENT,
        }
        params.update(kwargs)
        return CoinbaseBuilder().build(**params)

    def test_matches_the_consensus_coinbase_layout(self):
        """The split must reassemble into exactly ``Transaction.serialize_body``.

        This is the guarantee that the pool and the node agree on the coinbase
        transaction id, and therefore on the Merkle leaf the miner hashes.
        """
        height, value = 46894, 50 * 10**8
        en1, en2 = b"\xde\xad\xbe\xef", b"\x01\x02\x03\x04"
        cb = self._build(block_height=height, coinbase_value=value)

        body = _miner_coinbase(cb.coinbase1, en1.hex(), en2.hex(), cb.coinbase2)
        expected = build_coinbase(
            height=height,
            reward=value,
            pubkey_hash=PAYOUT_HASH,
            extra=en1 + en2 + COMMITMENT,
        )
        assert body == expected.serialize_body()
        assert hash256(body) == expected.txid()

    def test_coinbase_data_starts_with_the_height(self):
        cb = self._build(block_height=46894)
        body = _miner_coinbase(cb.coinbase1, "deadbeef", "01020304", cb.coinbase2)
        tx = parse_coinbase_body(body)
        assert tx.is_coinbase
        assert tx.coinbase_data[:4] == (46894).to_bytes(4, "little")

    def test_coinbase1_excludes_extranonce1(self):
        """coinbase1 must stop before extranonce1.

        If the pool baked extranonce1 into coinbase1 as well as handing it to
        the miner, the two sides would build different coinbases and every
        share would be rejected.
        """
        cb = self._build()
        assert "deadbeef" not in cb.coinbase1
        assert cb.extranonce1_size == 4
        assert cb.extranonce2_size == 4

    def test_coinbase_data_stays_within_the_consensus_limit(self):
        assert self._build().coinbase_data_size <= MAX_COINBASE_DATA

    def test_oversized_commitment_is_refused(self):
        with pytest.raises(ValueError, match="coinbase_data"):
            self._build(aux_commitment=b"\x00" * 200)

    def test_bad_payout_hash_length_is_refused(self):
        with pytest.raises(ValueError, match="payout hash"):
            self._build(payout_hash=b"\x11" * 19)

    def test_assembled_coinbase_carries_the_commitment(self):
        from scarletcoin.core.auxpow import build_auxpow_commitment, parse_auxpow_commitment

        commitment = build_auxpow_commitment(b"\xab" * 32, tree_size=1, nonce=7)
        cb = self._build(aux_commitment=commitment)
        body = _miner_coinbase(cb.coinbase1, "01020304", "05060708", cb.coinbase2)
        tx = parse_coinbase_body(body)
        assert parse_auxpow_commitment(tx.coinbase_data).aux_root == b"\xab" * 32


# ------------------------------------------------------------- merkle branch


class TestMerkleBranch:
    @pytest.mark.parametrize("tx_count", [0, 1, 2, 3, 4, 5, 7, 8, 9, 16, 17])
    def test_branch_reproduces_the_merkle_root(self, tx_count):
        txids = [hash256(bytes([i])) for i in range(tx_count)]
        coinbase_hash = hash256(b"coinbase")
        branches = coinbase_merkle_branch(txids)
        assert _fold_branch(coinbase_hash, branches) == merkle_root([coinbase_hash, *txids])

    def test_branch_is_empty_with_no_other_transactions(self):
        assert coinbase_merkle_branch([]) == []


# -------------------------------------------------------- header reconstruction


class TestHeaderReconstruction:
    def test_reconstruct_matches_an_independent_header(self):
        cb = CoinbaseBuilder().build(
            coinbase_value=50 * 10**8,
            block_height=100,
            payout_hash=PAYOUT_HASH,
            aux_commitment=COMMITMENT,
        )
        en1, en2 = "aabbccdd", "01020304"
        txids = [hash256(bytes([i])) for i in range(3)]
        branches = coinbase_merkle_branch(txids)
        prev_hash = hash256(b"parent")
        version, nbits, ntime, nonce = 1, 0x207FFFFF, 1_700_000_000, 42

        coinbase = _miner_coinbase(cb.coinbase1, en1, en2, cb.coinbase2)
        expected = _build_header(
            version=version,
            prev_hash_internal=prev_hash,
            merkle_root_internal=merkle_root([hash256(coinbase), *txids]),
            ntime=ntime,
            nbits=nbits,
            nonce=nonce,
        )
        got = CoinbaseBuilder.reconstruct_header(
            cb.coinbase1,
            en1,
            en2,
            cb.coinbase2,
            [b.hex() for b in branches],
            prev_hash.hex(),
            version,
            nbits,
            ntime,
            nonce,
        )
        assert got == expected
        assert len(got) == 80


# --------------------------------------------------------------- job manager


class TestJobManager:
    def test_refresh_reports_the_node_chain_id(self, rpc, key):
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        job = manager.refresh()
        assert job.scarlet.chain_id == node.params.auxpow_chain_id
        assert job.scarlet.height == node.chain.height + 1

    def test_share_difficulty_tracks_the_chain_by_default(self, rpc, key):
        """With no explicit difficulty the share rate follows the chain target."""
        node, _server, client = rpc
        manager = JobManager(
            bitcoin=SimulatedParentChain(),
            scarlet=client,
            payout_address=str(key.address(node.params.address_version)),
            chain_id=node.params.auxpow_chain_id,
        )
        job = manager.refresh()
        assert manager.share_target == job.scarlet.target * DEFAULT_SHARE_EASE
        # Easier than a block, so shares arrive more often than blocks.
        assert manager.share_target > job.scarlet.target
        assert manager.share_difficulty == pytest.approx(_DIFF1_TARGET / manager.share_target)

    def test_an_explicit_share_difficulty_pins_the_target(self, rpc, key):
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        manager.refresh()
        assert manager.share_target == int(_DIFF1_TARGET / 1e-9)

    def test_wrong_chain_id_is_refused(self, rpc, key):
        node, _server, client = rpc
        manager = JobManager(
            bitcoin=SimulatedParentChain(),
            scarlet=client,
            payout_address=str(key.address(node.params.address_version)),
            chain_id=1,  # the regtest node reports 3
            share_difficulty=1e-9,
        )
        with pytest.raises(RuntimeError, match="chain id"):
            manager.refresh()

    def test_share_with_bad_extranonce2_length_is_rejected(self, rpc, key):
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        job = manager.refresh()
        result = manager.process_share(job, "aabbccdd", "00", job.ntime, 0)
        assert result.accepted is False
        assert "extranonce2" in result.reason

    def test_two_miners_are_paid_two_different_addresses(self, rpc, key):
        """The whole point of per-miner jobs: each block pays its finder.

        A Stratum miner cannot build its own coinbase, so if the pool hands
        every miner the same one, everyone who connects mines for the pool
        operator.  Building a job per payout address is what fixes that, and
        the only proof is where the mined blocks actually pay.
        """
        node, _server, client = rpc
        version = node.params.address_version
        first = Address(version, b"\x11" * 20)
        second = Address(version, b"\x22" * 20)

        manager = _manager(node, client, str(first))

        def mine_with(address: Address) -> str:
            job = manager.refresh(str(address))
            en1, en2 = "aabbccdd", "00000000"
            coinbase = _miner_coinbase(job.coinbase.coinbase1, en1, en2, job.coinbase.coinbase2)
            nonce, _header = _solve(
                version=job.parent.version,
                prev_hash=bytes.fromhex(job.parent.prev_hash),
                coinbase=coinbase,
                branches=job.merkle_branches,
                ntime=job.ntime,
                nbits=job.parent.nbits,
                target=min(job.scarlet.target, manager.share_target),
            )
            result = manager.submit_sct_block(job, en1, en2, job.ntime, nonce)
            assert result is not None and result["status"] == "connected", result
            block = node.chain.storage.get_block(node.chain.tip_hash)
            paid = block.transactions[0].outputs[0].payload
            return str(Address(version, paid))

        assert mine_with(first) == str(first)
        assert mine_with(second) == str(second)
        assert manager.sct_blocks_accepted == 2

    def test_an_older_job_is_still_judged_against_its_own_coinbase(self, rpc, key):
        """Every miner holds its own job, so a job must not be rejected merely
        for not being the most recently built one."""
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        mine = manager.refresh()
        manager.refresh()  # another miner's job becomes the newest
        result = manager.process_share(mine, "aabbccdd", "00000000", mine.ntime, 0)
        # What matters is that a job is never refused for not being the newest.
        assert result.reason != "stale job"

    def test_a_valid_share_becomes_an_accepted_block(self, rpc, key):
        """Drive a whole share through the manager and into a real block."""
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        job = manager.refresh()

        en1, en2 = "aabbccdd", "00000000"
        coinbase = _miner_coinbase(job.coinbase.coinbase1, en1, en2, job.coinbase.coinbase2)
        nonce, _header = _solve(
            version=job.parent.version,
            prev_hash=bytes.fromhex(job.parent.prev_hash),
            coinbase=coinbase,
            branches=job.merkle_branches,
            ntime=job.ntime,
            nbits=job.parent.nbits,
            # Beat whichever target is harder: the pool's share target or the
            # chain's block target.
            target=min(job.scarlet.target, manager.share_target),
        )

        share = manager.process_share(job, en1, en2, job.ntime, nonce)
        assert share.accepted is True, share.reason
        assert share.meets_sct_target is True

        result = manager.submit_sct_block(job, en1, en2, job.ntime, nonce)
        assert result is not None
        assert result["status"] == "connected"
        assert manager.sct_blocks_accepted == 1
        assert node.chain.height == 1

    def test_a_stale_candidate_is_reported_and_not_raised(self, rpc, key, monkeypatch):
        """A tip that moved must not take the miner's connection down with it."""
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        job = manager.refresh()

        en1, en2 = "aabbccdd", "00000000"
        coinbase = _miner_coinbase(job.coinbase.coinbase1, en1, en2, job.coinbase.coinbase2)
        nonce, _header = _solve(
            version=job.parent.version,
            prev_hash=bytes.fromhex(job.parent.prev_hash),
            coinbase=coinbase,
            branches=job.merkle_branches,
            ntime=job.ntime,
            nbits=job.parent.nbits,
            target=min(job.scarlet.target, manager.share_target),
        )

        def stale(*_args, **_kwargs):
            raise RpcClientError("no AuxPoW candidate with that hash; the tip advanced")

        monkeypatch.setattr(client, "call", stale)
        result = manager.submit_sct_block(job, en1, en2, job.ntime, nonce)
        assert result is not None
        assert result["status"] == "rejected"
        assert manager.sct_blocks_rejected == 1
        assert manager.sct_blocks_accepted == 0

    def test_a_share_below_the_share_target_is_rejected(self, rpc, key):
        """A share difficulty that is too high rejects work even if a block would qualify."""
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        job = manager.refresh()
        manager.share_target = 1  # astronomically hard
        result = manager.process_share(job, "aabbccdd", "00000000", job.ntime, 0)
        assert result.accepted is False
        assert result.reason == "share above target"


# -------------------------------------------------------- end-to-end over TCP


async def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write((json.dumps(obj) + "\n").encode())
    await writer.drain()


async def _recv(reader: asyncio.StreamReader) -> dict:
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    assert line, "server closed the connection"
    return json.loads(line)


async def _recv_id(reader: asyncio.StreamReader, req_id: int) -> dict:
    for _ in range(10):
        msg = await _recv(reader)
        if msg.get("id") == req_id:
            return msg
    raise AssertionError(f"no response with id {req_id}")


async def _recv_method(reader: asyncio.StreamReader, method: str) -> dict:
    for _ in range(10):
        msg = await _recv(reader)
        if msg.get("method") == method:
            return msg
    raise AssertionError(f"no {method} notification")


class TestStratumWireProtocol:
    """A real TCP client speaking Stratum to the real server."""

    def test_landing_a_block_pushes_a_fresh_job_immediately(self, rpc, key):
        """The pool must re-template the moment a block lands.

        Otherwise every miner keeps hashing a prevhash that can no longer win,
        and each block-worthy share it submits is bounced by the node until the
        next periodic refresh.
        """
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        stratum = StratumServer(host="127.0.0.1", port=0, manager=manager, job_interval=3600)
        asyncio.run(self._run(stratum, manager, expect_refresh=True))

    def test_an_idle_miner_is_disconnected_after_the_timeout(self, rpc, key):
        """The idle timeout must reap dead sockets, at the configured value."""
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        stratum = StratumServer(
            host="127.0.0.1", port=0, manager=manager, job_interval=3600, client_timeout=1.0
        )
        asyncio.run(self._idle(stratum))
        assert stratum.sessions == 0

    async def _idle(self, stratum: StratumServer) -> None:
        await stratum.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", stratum.port)
            try:
                await _send(writer, {"id": 1, "method": "mining.subscribe", "params": ["t/1"]})
                await _recv_id(reader, 1)
                # Say nothing else; the pool should hang up on its own.  It
                # may still send us a difficulty and a job first.
                for _ in range(10):
                    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                    if line == b"":
                        break
                else:
                    raise AssertionError("the pool never closed the idle connection")
            finally:
                writer.close()
        finally:
            await stratum.stop()

    def test_a_worker_without_an_address_is_refused(self, rpc, key):
        """Nobody should be able to mine for the operator by accident.

        If the pool silently accepted a worker name with no address, that
        miner's hashrate would pay the pool's address without the miner ever
        being told.  Refuse instead, and say what to do.
        """
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        stratum = StratumServer(host="127.0.0.1", port=0, manager=manager, job_interval=3600)
        asyncio.run(self._authorize(stratum, "rig1", expect_ok=False))

    def test_a_worker_without_an_address_is_accepted_when_the_pool_opts_in(self, rpc, key):
        """--allow-pool-payout is the explicit escape hatch, not the default."""
        node, _server, client = rpc
        operator = str(key.address(node.params.address_version))
        manager = _manager(node, client, operator)
        stratum = StratumServer(
            host="127.0.0.1",
            port=0,
            manager=manager,
            job_interval=3600,
            default_payout_address=operator,
            allow_pool_payout=True,
        )
        asyncio.run(self._authorize(stratum, "rig1", expect_ok=True))

    async def _authorize(self, stratum: StratumServer, worker: str, *, expect_ok: bool) -> None:
        await stratum.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", stratum.port)
            try:
                await _send(writer, {"id": 1, "method": "mining.subscribe", "params": ["t/1"]})
                await _recv_id(reader, 1)
                await _send(
                    writer, {"id": 2, "method": "mining.authorize", "params": [worker, "x"]}
                )
                reply = await _recv_id(reader, 2)
                if expect_ok:
                    assert reply.get("result") is True
                else:
                    assert reply.get("result") is not True
                    assert "address" in json.dumps(reply.get("error", "")).lower()
            finally:
                writer.close()
        finally:
            await stratum.stop()

    def test_subscribe_authorize_and_submit_a_block(self, rpc, key):
        node, _server, client = rpc
        manager = _manager(node, client, str(key.address(node.params.address_version)))
        stratum = StratumServer(host="127.0.0.1", port=0, manager=manager, job_interval=3600)
        asyncio.run(self._run(stratum, manager))
        assert node.chain.height == 1
        assert manager.sct_blocks_accepted == 1

    async def _run(
        self, stratum: StratumServer, manager: JobManager, *, expect_refresh: bool = False
    ) -> None:
        await stratum.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", stratum.port)
            try:
                await _send(writer, {"id": 1, "method": "mining.subscribe", "params": ["t/1"]})
                sub = await _recv_id(reader, 1)
                assert sub.get("error") is None
                _, extranonce1, extranonce2_size = sub["result"]
                assert len(extranonce1) == 8
                assert extranonce2_size == 4

                await _send(
                    writer,
                    {
                        "id": 2,
                        "method": "mining.authorize",
                        "params": [f"{manager._payout_address}.rig1", "x"],
                    },
                )
                assert (await _recv_id(reader, 2))["result"] is True

                notify = await _recv_method(reader, "mining.notify")
                (
                    job_id,
                    prevhash,
                    coinbase1,
                    coinbase2,
                    branches,
                    version,
                    nbits,
                    ntime,
                    _clean,
                ) = notify["params"]

                en2 = "00000000"
                coinbase = _miner_coinbase(coinbase1, extranonce1, en2, coinbase2)
                job = manager.current
                nonce, _header = _solve(
                    version=int(version, 16),
                    prev_hash=bytes.fromhex(prevhash),
                    coinbase=coinbase,
                    branches=branches,
                    ntime=int(ntime, 16),
                    nbits=int(nbits, 16),
                    target=min(job.scarlet.target, manager.share_target),
                )

                await _send(
                    writer,
                    {
                        "id": 3,
                        "method": "mining.submit",
                        "params": ["worker", job_id, en2, ntime, f"{nonce:08x}"],
                    },
                )
                assert (await _recv_id(reader, 3))["result"] is True

                if expect_refresh:
                    # The block moved the tip, so the job we just solved against
                    # is dead.  A fresh one must arrive without waiting for the
                    # periodic refresh (job_interval is an hour here).
                    fresh = await asyncio.wait_for(
                        _recv_method(reader, "mining.notify"), timeout=10.0
                    )
                    assert fresh["params"][0] != job_id, "expected a new job after the block"
                    assert fresh["params"][8] is True, "the refresh should be a clean job"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await stratum.stop()
