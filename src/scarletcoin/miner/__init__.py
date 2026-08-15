"""Mining: the proof-of-work search and the solo miner."""

from scarletcoin.miner.miner import Miner, MinerStats, MiningError
from scarletcoin.miner.solver import scan_nonces, solve_block

__all__ = ["Miner", "MinerStats", "MiningError", "scan_nonces", "solve_block"]
