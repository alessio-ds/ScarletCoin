"""Consensus core: transactions, blocks, the chain and its rules."""

from scarletcoin.core.auxpow import (
    AuxPoW,
    AuxPoWCommitment,
    AuxPoWError,
    ParentBlockHeader,
    build_auxpow_commitment,
    check_merkle_branch,
    get_expected_index,
    parse_auxpow_commitment,
    validate_auxpow,
)
from scarletcoin.core.block import Block, BlockError, BlockHeader, merkle_root
from scarletcoin.core.chain import AddBlockResult, Blockchain, BlockStatus
from scarletcoin.core.coinbase import build_coinbase, coinbase_height
from scarletcoin.core.mempool import Mempool, MempoolEntry, MempoolError
from scarletcoin.core.params import COIN, MAX_MONEY, NETWORKS, ChainParams, get_params
from scarletcoin.core.storage import Storage
from scarletcoin.core.template import (
    AuxBlockCandidate,
    BlockTemplate,
    create_aux_block,
    create_block_template,
)
from scarletcoin.core.transaction import OutPoint, Transaction, TxInput, TxOutput
from scarletcoin.core.utxo import Coin
from scarletcoin.core.validation import MissingInputError, ValidationError

__all__ = [
    "COIN",
    "MAX_MONEY",
    "NETWORKS",
    "AddBlockResult",
    "AuxBlockCandidate",
    "AuxPoW",
    "AuxPoWCommitment",
    "AuxPoWError",
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
    "OutPoint",
    "ParentBlockHeader",
    "Storage",
    "Transaction",
    "TxInput",
    "TxOutput",
    "ValidationError",
    "build_auxpow_commitment",
    "build_coinbase",
    "check_merkle_branch",
    "coinbase_height",
    "create_aux_block",
    "create_block_template",
    "get_expected_index",
    "get_params",
    "merkle_root",
    "parse_auxpow_commitment",
    "validate_auxpow",
]
