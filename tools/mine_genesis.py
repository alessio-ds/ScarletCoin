"""Search for the genesis-block nonces embedded in :mod:`scarletcoin.core.params`.

Run this only when the genesis definition changes::

    uv run python tools/mine_genesis.py
"""

from __future__ import annotations

import sys
from dataclasses import replace

from scarletcoin.core.params import NETWORKS, ChainParams
from scarletcoin.core.pow import check_proof_of_work


def mine(params: ChainParams) -> tuple[int, str]:
    """Return the first nonce that solves the genesis block, and its hash."""
    for nonce in range(0, 2**32):
        candidate = replace(params, genesis_nonce=nonce)
        block = candidate.genesis_block
        if check_proof_of_work(block.hash(), block.header.bits, pow_limit=params.pow_limit):
            return nonce, block.hash_hex()
    raise SystemExit("no nonce solves the genesis block; change the timestamp")


def main() -> int:
    for name, params in NETWORKS.items():
        nonce, block_hash = mine(params)
        status = "unchanged" if nonce == params.genesis_nonce else "UPDATE params.py"
        print(f"{name:8s} nonce={nonce:<12d} hash={block_hash}  [{status}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
