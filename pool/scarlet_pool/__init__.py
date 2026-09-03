"""ScarletCoin merged-mining pool (Stratum V1 bridge).

Accept standard SHA-256 ASIC miners and let them mine ScarletCoin
as a side-effect of their normal Bitcoin hashing, with zero changes
to ASIC firmware.

Quick start (regtest)::

    # Terminal 1 - ScarletCoin node
    scarletcoin node regtest --rpc

    # Terminal 2 - Pool
    python -m pool.scarlet_pool.server http://127.0.0.1:40332 <your-address>

    # Terminal 3 - miner (or test with any Stratum client)
    # Connect to stratum+tcp://127.0.0.1:3333
"""

__all__: list[str] = []
