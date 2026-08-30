"""Reference merged-mining coordinator.

Talks to a ScarletCoin node (``createauxblock`` / ``submitauxblock``) and
simulates a Bitcoin parent chain so a regtest or testnet deployment can exercise
the full AuxPoW flow without needing a real Bitcoin node.

For production use you would swap the simulated parent chain for a real
Bitcoin-core RPC client (or a Stratum pool proxy).  This file documents the
integration and doubles as a functional test harness.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from scarletcoin.core.auxpow import (
    AuxPoW,
    build_auxpow_commitment,
)
from scarletcoin.core.block import merkle_root
from scarletcoin.core.coinbase import encode_coinbase_data
from scarletcoin.core.transaction import COINBASE_OUTPOINT, Transaction, TxInput, TxOutput
from scarletcoin.net.client import RpcClient

# ── simulated parent chain (stand-in for a Bitcoin node) ──────────────────────


@dataclass
class ParentChainState:
    """Mutable state of the simulated parent (Bitcoin) chain."""

    height: int = 800_000
    prev_hash: bytes = b"\x11" * 32
    bits: int = 0x207FFFFF  # regtest-easy target for simulation


class ParentChain:
    """A tiny in-process "Bitcoin" chain that lets us build and solve blocks.

    In production this would be replaced by a ``bitcoind`` RPC client.
    """

    def __init__(self) -> None:
        self.state = ParentChainState()

    def build_block(self, coinbase: Transaction, extra_txids: list[bytes] | None = None) -> bytes:
        """Return an 80-byte parent header whose Merkle root commits to ``coinbase``.

        The header is returned unsolved (nonce=0); the caller runs ``solve``.
        """
        txids = [coinbase.txid()] + (extra_txids or [])
        root = merkle_root(txids)
        from scarletcoin.core.auxpow import ParentBlockHeader

        header = ParentBlockHeader(
            version=1,
            prev_hash=self.state.prev_hash,
            merkle_root=root,
            timestamp=int(time.time()),
            bits=self.state.bits,
            nonce=0,
        )
        return header.serialize()

    def solve(self, header_bytes: bytes, target: int) -> bytes:
        """Brute-force a nonce so that double-SHA256d(header) <= ``target``."""
        from dataclasses import replace

        from scarletcoin.core.auxpow import ParentBlockHeader

        header = ParentBlockHeader.deserialize(header_bytes)
        nonce = 0
        while nonce < 1_000_000:
            candidate = replace(header, nonce=nonce)
            if int.from_bytes(candidate.hash(), "little") <= target:
                return candidate.serialize()
            nonce += 1
        raise RuntimeError("could not solve parent header within nonce limit")

    def advance(self, new_header_bytes: bytes) -> None:
        """Move the simulated tip to the given solved header."""
        from scarletcoin.core.auxpow import ParentBlockHeader

        header = ParentBlockHeader.deserialize(new_header_bytes)
        self.state.height += 1
        self.state.prev_hash = header.hash()


# ── reference coordinator ────────────────────────────────────────────────────


class MergedMiningCoordinator:
    """Coordinates merged mining between a ScarletCoin node and a parent chain.

    Usage::

        mm = MergedMiningCoordinator(ScarletRpc("http://127.0.0.1:40332"))
        mm.mine_loop(pubkey_hash=b"...")
    """

    def __init__(self, scarlet: RpcClient, parent: ParentChain | None = None) -> None:
        self.scarlet = scarlet
        self.parent = parent or ParentChain()

    # ── single-round helper (most useful for tests) ──────────────────────

    def mine_one_block(self, pubkey_hash: bytes) -> dict | None:
        """Run one complete merged-mining cycle and return the submission result.

        Returns ``None`` when the parent proof did not meet the SCT target.
        """
        # 1. Get ScarletCoin aux work -------------------------------------------
        aux = self.scarlet.call("createauxblock", pubkey_hash.hex())  # type: ignore[arg-type]
        sct_target = int(aux["target"], 16)
        sct_hash = bytes.fromhex(aux["hash"])[::-1]

        # 2. Build the merged-mining commitment ---------------------------------
        commitment = build_auxpow_commitment(sct_hash, tree_size=1, nonce=aux["nonce"])

        # 3. Build the parent Bitcoin coinbase ----------------------------------
        parent_cb = self._build_parent_coinbase(commitment)

        # 4. Build the parent header (Merkle root commits to coinbase) ----------
        header_bytes = self.parent.build_block(parent_cb)
        solved = self.parent.solve(header_bytes, sct_target)

        # 5. Assemble AuxPoW proof ----------------------------------------------
        from scarletcoin.core.auxpow import ParentBlockHeader

        solved_header = ParentBlockHeader.deserialize(solved)
        auxpow = AuxPoW(
            coinbase_tx=parent_cb,
            coinbase_merkle_branch=(),  # coinbase is the only transaction
            coinbase_index=0,
            aux_merkle_branch=(),  # single aux chain
            aux_chain_index=0,
            parent_header=solved_header,
        )

        # 6. Submit to ScarletCoin -----------------------------------------------
        return self.scarlet.call("submitauxblock", aux["hash"], auxpow.serialize().hex())  # type: ignore[arg-type]

    # ── helpers ──────────────────────────────────────────────────────────

    def _build_parent_coinbase(self, commitment: bytes) -> Transaction:
        """Build a minimal parent-coinbase containing ``commitment``."""
        # The parent coinbase data carries the merged-mining commitment.
        # Bitcoin coinbases normally start with the block height (BIP-34), but
        # for this reference implementation we put the commitment in directly.
        coinbase_data = encode_coinbase_data(self.parent.state.height, commitment)
        return Transaction(
            version=1,
            inputs=(TxInput(COINBASE_OUTPOINT),),
            outputs=(TxOutput.p2pkh(50 * 10**8, b"\x01" * 20)),  # dummy payout
            coinbase_data=coinbase_data,
        )

    # ── continuous loop ───────────────────────────────────────────────────

    def mine_loop(self, pubkey_hash: bytes, *, max_blocks: int | None = None) -> int:
        """Continuously mine blocks until stopped or ``max_blocks`` reached."""
        mined = 0
        while max_blocks is None or mined < max_blocks:
            try:
                result = self.mine_one_block(pubkey_hash)
                if result is not None:
                    mined += 1
                    print(f"[{mined}] Block {result['hash']} accepted at height {result['height']}")
                else:
                    time.sleep(0.5)
            except Exception as exc:
                print(f"Mining error: {exc}")
                time.sleep(2.0)
        return mined


# ── demonstration entry point ────────────────────────────────────────────────

if __name__ == "__main__":
    """Demonstrate merged mining against a local regtest ScarletCoin node.

    Start the node first::

        scarletcoin node regtest --rpc

    Then run this script::

        python -m tools.merged_mining.coordinator
    """
    import sys

    pubkey_hash = bytes.fromhex("0000000000000000000000000000000000000000")
    if len(sys.argv) > 1:
        try:
            from scarletcoin.crypto.keys import Address

            pubkey_hash = Address.decode(sys.argv[1]).hash
        except Exception as exc:
            print(f"Cannot parse address {sys.argv[1]}: {exc}")
            sys.exit(1)
        print(f"Paying to {sys.argv[1]}")
    else:
        print("No payout address given — rewards will be burned")
        print("Usage: python -m tools.merged_mining.coordinator <address>")

    print("Connecting to ScarletCoin regtest node...")
    scarlet = RpcClient("http://127.0.0.1:40332", timeout=30.0)

    try:
        info = scarlet.call("getinfo")
        print(f"Connected to {info['network']} at height {info['height']}")
    except Exception as exc:
        print(f"Cannot reach ScarletCoin node: {exc}")
        print("Start one with: scarletcoin node regtest --rpc")
        sys.exit(1)

    mm = MergedMiningCoordinator(scarlet)
    print("Merged mining started. Press Ctrl-C to stop.")
    try:
        mm.mine_loop(pubkey_hash)
    except KeyboardInterrupt:
        print("\nStopped.")
