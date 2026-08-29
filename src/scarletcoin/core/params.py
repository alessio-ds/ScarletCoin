"""Network (chain) parameters.

Everything a node must agree on with its peers lives here: the money supply
schedule, the difficulty schedule, address prefixes, ports and the genesis
block.  Three networks are defined:

``mainnet``
    The real chain.
``testnet``
    Same rules, separate genesis and magic bytes, worthless coins.
``regtest``
    A local network with a trivially easy proof of work, used by the test suite
    and for development.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Final

from scarletcoin.core.block import Block
from scarletcoin.core.coinbase import build_coinbase
from scarletcoin.core.pow import bits_to_target
from scarletcoin.core.transaction import MAX_MONEY, Transaction

__all__ = ["COIN", "MAX_MONEY", "NETWORKS", "ChainParams", "get_params", "network_names"]

#: Smallest indivisible units in one ScarletCoin.  The unit is called a "scar".
COIN: Final[int] = 100_000_000

#: Genesis outputs pay this (provably unspendable) hash: nobody owns the genesis coins.
_UNSPENDABLE_HASH: Final[bytes] = b"\x00" * 20


@dataclass(frozen=True)
class ChainParams:
    """Consensus and network constants for one ScarletCoin network."""

    name: str
    magic: bytes
    address_version: int
    wif_version: int
    script_address_version: int
    """Version byte of P2SH addresses (distinct prefix from ``address_version``)."""
    default_p2p_port: int
    default_rpc_port: int

    # Difficulty
    target_spacing: int
    """Desired number of seconds between blocks."""
    retarget_interval: int
    """Number of blocks between difficulty adjustments."""
    pow_limit_bits: int
    """Compact form of the easiest target the network will ever accept."""
    max_adjustment_factor: int = 4
    """Largest difficulty change a single retarget may apply."""
    per_block_retarget: bool = False
    """Recalculate the target every block (rather than once per period).

    When enabled, the target is measured against the *next* block's own
    timestamp, so a chain that stalls for more than ``max_future_time`` falls
    back to the pow limit and recovers immediately instead of dying after a
    hashrate collapse.
    """
    retarget_fork_height: int = 0
    """First height at which per-block retargeting applies.

    Below this height the periodic rule is used, so a network that upgraded
    from periodic to per-block retargeting keeps validating its pre-fork
    history instead of rejecting it.  ``0`` means per-block retargeting applies
    from genesis.
    """
    retarget_measure_fork_height: int = 0
    """First height at which the target is measured directly from the hashrate.

    Between :attr:`retarget_fork_height` and this height the target is adjusted
    by a time ratio; from this height on it is computed from the chainwork
    actually mined in the trailing window.  ``0`` means the direct measurement
    applies from :attr:`retarget_fork_height`.
    """

    # Money
    initial_subsidy: int = 50 * COIN
    halving_interval: int = 210_000
    coinbase_maturity: int = 100
    """Confirmations a coinbase output needs before it can be spent."""

    # Limits
    max_block_size: int = 1_000_000
    max_future_time: int = 2 * 60 * 60
    """How far ahead of local time a block timestamp may be."""
    median_time_blocks: int = 11
    """Window used for the "greater than the median of the last N" timestamp rule."""
    min_relay_fee_per_kb: int = 1_000
    """Cheapest fee rate a node will relay or mine, in scar per kilobyte."""
    min_output_value: int = 1
    """Smallest output value the network will relay and mine, in scar.
    
    Outputs below this value are rejected by the mempool and by block validation,
    which prevents an attacker from filling the UTXO set with millions of
    sub-dust outputs.  The default of ``1`` (the smallest indivisible unit) keeps
    every valid transaction spendable while closing the obvious spam vector.
    """

    # BIP-0044
    bip44_coin_type: int = 0
    """Coin type used in BIP-0044 derivation paths (m/44'/coin'/...)."""

    # Checkpoints
    checkpoints: dict[int, str] = field(default_factory=dict)
    """Known-good block hashes by height, in display (big-endian) hex.

    A block at a checkpoint height whose hash differs is rejected outright.  This
    prevents reorganisations past the newest checkpoint and bounds how much work
    an attacker needs to rewrite deep history.
    """

    # Genesis
    genesis_timestamp: int = 0
    genesis_bits: int = 0
    genesis_nonce: int = 0
    genesis_message: bytes = b""

    # Bootstrap
    seeds: tuple[str, ...] = field(default_factory=tuple)
    """Host names a fresh node bootstraps from.

    Each name is resolved to every address it points at, so one entry can stand
    for several machines. Whoever runs a network publishes one or two long-lived
    names here; everything else is learned by gossip afterwards. Operators can
    add more at run time with ``--seed``.
    """

    public_nodes: tuple[str, ...] = field(default_factory=tuple)
    """Base URLs of nodes that serve the read-only RPC methods to anybody.

    These are the nodes a wallet or a miner can use without downloading the
    chain first: a node started with ``--rpc-public`` answers
    :data:`scarletcoin.net.rpc.PUBLIC_METHODS` with no token at all. Unlike
    :attr:`seeds`, which are peer-to-peer addresses, these are HTTP endpoints,
    and they are only a starting point: a client asks whichever one answers for
    the rest of the list (``getpublicnodes``).
    """

    # ------------------------------------------------------------------ derived

    @cached_property
    def pow_limit(self) -> int:
        """The easiest allowed target, as an integer."""
        return bits_to_target(self.pow_limit_bits)

    @property
    def target_timespan(self) -> int:
        """Expected duration of one retargeting period, in seconds."""
        return self.target_spacing * self.retarget_interval

    def subsidy(self, height: int) -> int:
        """Return the block subsidy at ``height``, halving every interval."""
        if height < 0:
            raise ValueError("block height must not be negative")
        halvings = height // self.halving_interval
        if halvings >= 64:
            return 0
        return self.initial_subsidy >> halvings

    @cached_property
    def genesis_coinbase(self) -> Transaction:
        """The genesis block's coinbase transaction."""
        return build_coinbase(
            height=0,
            reward=self.subsidy(0),
            pubkey_hash=_UNSPENDABLE_HASH,
            extra=self.genesis_message,
        )

    @cached_property
    def genesis_block(self) -> Block:
        """The hard-coded first block of the chain."""
        return Block.create(
            prev_hash=b"\x00" * 32,
            transactions=[self.genesis_coinbase],
            bits=self.genesis_bits,
            timestamp=self.genesis_timestamp,
            nonce=self.genesis_nonce,
        )

    @cached_property
    def genesis_hash(self) -> bytes:
        """Hash of the genesis block."""
        return self.genesis_block.hash()

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.name


#: Text embedded in the genesis coinbase of every network.
_GENESIS_MESSAGE: Final[bytes] = b"ScarletCoin: a small chain, honestly built"


MAINNET = ChainParams(
    name="mainnet",
    magic=b"SCRL",
    address_version=63,  # addresses start with "S"
    wif_version=191,
    script_address_version=50,  # P2SH addresses start with "M"
    default_p2p_port=20333,
    default_rpc_port=20332,
    target_spacing=60,
    retarget_interval=60,
    pow_limit_bits=0x1E0FFFFF,
    per_block_retarget=True,
    retarget_fork_height=10496,
    retarget_measure_fork_height=10563,
    genesis_timestamp=1_700_000_000,
    genesis_bits=0x1E0FFFFF,
    genesis_nonce=816_317,
    genesis_message=_GENESIS_MESSAGE,
    # Long-lived host names of the network's public nodes. A name may hold
    # several A/AAAA records; a starting node tries all of them and then learns
    # the rest of the network by gossip. Port 20333 is assumed when omitted.
    # The literal address is a fallback for when DNS is broken, filtered, or
    # answered by a proxy that cannot carry the peer-to-peer protocol.
    seeds=("scarletcoin.remotewire.net", "45.126.126.139"),
    # Nodes that serve the public RPC methods over HTTPS, so a wallet or a miner
    # can be useful before it has a chain of its own. Whichever of these answers
    # first is asked for the others, so this list only has to get a client
    # started, not stay complete.
    public_nodes=("https://scarletcoin.remotewire.net",),
)

TESTNET = ChainParams(
    name="testnet",
    magic=b"SCRT",
    address_version=127,  # addresses start with "t"
    wif_version=239,
    script_address_version=65,  # P2SH addresses start with "T"
    default_p2p_port=30333,
    default_rpc_port=30332,
    target_spacing=60,
    retarget_interval=60,
    pow_limit_bits=0x1E0FFFFF,
    per_block_retarget=True,
    coinbase_maturity=20,
    bip44_coin_type=1,
    genesis_timestamp=1_700_000_001,
    genesis_bits=0x1E0FFFFF,
    genesis_nonce=154_650,
    genesis_message=_GENESIS_MESSAGE + b" (testnet)",
    seeds=(),
)

REGTEST = ChainParams(
    name="regtest",
    magic=b"SCRR",
    address_version=127,
    wif_version=239,
    script_address_version=65,
    default_p2p_port=40333,
    default_rpc_port=40332,
    target_spacing=10,
    retarget_interval=20,
    pow_limit_bits=0x207FFFFF,
    coinbase_maturity=2,
    max_future_time=2 * 60 * 60,
    bip44_coin_type=1,
    genesis_timestamp=1_700_000_002,
    genesis_bits=0x207FFFFF,
    genesis_nonce=5,
    genesis_message=_GENESIS_MESSAGE + b" (regtest)",
    seeds=(),
)

#: Every known network, by name.
NETWORKS: Final[dict[str, ChainParams]] = {
    MAINNET.name: MAINNET,
    TESTNET.name: TESTNET,
    REGTEST.name: REGTEST,
}


def network_names() -> tuple[str, ...]:
    """Return the names of all known networks."""
    return tuple(NETWORKS)


def get_params(name: str) -> ChainParams:
    """Look up chain parameters by network name.

    Raises:
        KeyError: if ``name`` is not a known network.
    """
    try:
        return NETWORKS[name]
    except KeyError:
        known = ", ".join(NETWORKS)
        raise KeyError(f"unknown network {name!r}; choose one of: {known}") from None
