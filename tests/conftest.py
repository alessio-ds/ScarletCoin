"""Shared fixtures.

Every test runs on the ``regtest`` network, whose proof of work is trivial, so a
block can be mined in microseconds.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from scarletcoin.core.params import REGTEST
from scarletcoin.crypto.keys import PrivateKey
from scarletcoin.net.client import RpcClient
from scarletcoin.net.node import Node, NodeConfig
from scarletcoin.net.rpc import RpcServer
from scarletcoin.wallet.keystore import Keystore
from scarletcoin.wallet.wallet import Wallet
from tests.helpers import make_node_state


@pytest.fixture
def params():
    """Regtest chain parameters."""
    return REGTEST


@pytest.fixture
def key() -> PrivateKey:
    """A throwaway private key."""
    return PrivateKey.generate()


@pytest.fixture
def other_key() -> PrivateKey:
    """A second throwaway private key."""
    return PrivateKey.generate()


@pytest.fixture
def chain():
    """An in-memory regtest chain."""
    blockchain, _ = make_node_state()
    yield blockchain
    blockchain.storage.close()


@pytest.fixture
def chain_and_pool():
    """An in-memory regtest chain with a mempool attached."""
    blockchain, mempool = make_node_state()
    yield blockchain, mempool
    blockchain.storage.close()


def _start_node(tmp_path, name: str, **overrides) -> Node:
    config = NodeConfig(
        network="regtest",
        datadir=tmp_path / name,
        p2p_port=0,
        rpc_port=0,
        use_seeds=False,
        **overrides,
    )
    node = Node(config)
    node.start()
    return node


@pytest.fixture
def node(tmp_path) -> Iterator[Node]:
    """A running regtest node with no RPC server."""
    running = _start_node(tmp_path, "node", listen=False)
    yield running
    running.stop()


@pytest.fixture
def rpc(tmp_path) -> Iterator[tuple[Node, RpcServer, RpcClient]]:
    """A running node with its RPC server and a client pointed at it."""
    running = _start_node(tmp_path, "rpcnode", listen=False)
    server = RpcServer(running, port=0, token="test-token")
    server.start()
    client = RpcClient(server.url, token="test-token", timeout=15.0)
    yield running, server, client
    server.stop()
    running.stop()


@pytest.fixture
def wallet(tmp_path, rpc) -> Wallet:
    """A wallet backed by the ``rpc`` fixture's node."""
    _, _, client = rpc
    keystore = Keystore.create(tmp_path / "wallet.json", "regtest")
    return Wallet(keystore, client)


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` passes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
