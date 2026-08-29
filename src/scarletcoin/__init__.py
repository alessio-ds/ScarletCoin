"""ScarletCoin: a small but complete proof-of-work cryptocurrency.

The package is organised in layers, each usable on its own:

``scarletcoin.crypto``
    Keys, addresses, hashes and encryption.
``scarletcoin.core``
    Consensus: transactions, blocks, the UTXO set, the chain and the mempool.
``scarletcoin.net``
    The peer-to-peer protocol, the node daemon and its JSON-RPC interface.
``scarletcoin.wallet``
    Key storage, coin selection and transaction building.
``scarletcoin.miner``
    Proof-of-work mining against a node.
``scarletcoin.gui``
    Optional Qt desktop wallet and miner.
"""

from scarletcoin.core.params import COIN, NETWORKS, get_params

__all__ = ["COIN", "NETWORKS", "__version__", "get_params"]

__version__ = "2.3.4"
