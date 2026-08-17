"""Consensus core: transactions, blocks, the chain and its rules."""

from scarletcoin.core.block import Block, BlockError, BlockHeader, merkle_root
from scarletcoin.core.chain import AddBlockResult, Blockchain, BlockStatus
from scarletcoin.core.coinbase import build_coinbase, coinbase_height
from scarletcoin.core.mempool import Mempool, MempoolEntry, MempoolError
from scarletcoin.core.params import COIN, MAX_MONEY, NETWORKS, ChainParams, get_params
from scarletcoin.core.storage import Storage
from scarletcoin.core.template import BlockTemplate, create_block_template
from scarletcoin.core.transaction import Transaction, TxInput, TxOutput
from scarletcoin.core.utxo import Coin
from scarletcoin.core.validation import MissingInputError, ValidationError

__all__ = [
    "COIN",
    "MAX_MONEY",
    "NETWORKS",
    "AddBlockResult",
    "Block",
    "BlockError",
    "BlockHeader",
    "BlockStatus",
    "BlockTemplate",
    "Blockchain",
    "ChainParams",
    "Coin",
    "Mempool",
    "MempoolEntry",
    "MempoolError",
    "MissingInputError",
    "Storage",
    "Transaction",
    "TxInput",
    "TxOutput",
    "ValidationError",
    "build_coinbase",
    "coinbase_height",
    "create_block_template",
    "get_params",
    "merkle_root",
]
