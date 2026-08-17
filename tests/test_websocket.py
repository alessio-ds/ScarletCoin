"""Tests for the explorer's live WebSocket endpoint."""

from __future__ import annotations

import json

import websockets.sync.client

from scarletcoin.net.websocket import WebSocketHub
from tests.helpers import mine_block


class TestWebSocketHub:
    def test_broadcasts_to_connected_clients(self):
        hub = WebSocketHub(port=0)
        hub.start()
        try:
            with websockets.sync.client.connect(hub.url) as client:
                hub.broadcast({"type": "block", "height": 1})
                message = json.loads(client.recv(timeout=5.0))
                assert message == {"type": "block", "height": 1}
        finally:
            hub.stop()

    def test_broadcast_is_a_noop_before_start(self):
        hub = WebSocketHub(port=0)
        hub.broadcast({"type": "block", "height": 1})


class TestNodeWebSocket:
    def test_a_node_pushes_block_events(self, tmp_path, key):
        from scarletcoin.net.node import Node, NodeConfig

        config = NodeConfig(network="regtest", datadir=tmp_path, listen=False, rpc=False)
        node = Node(config)
        node.start()
        try:
            assert node.ws_hub.running
            with websockets.sync.client.connect(node.ws_hub.url) as client:
                block = mine_block(node.chain, key)
                node.submit_block(block)
                message = json.loads(client.recv(timeout=5.0))
                assert message["type"] == "block"
                assert message["height"] == 1
        finally:
            node.stop()

    def test_ws_port_is_reported(self, tmp_path):
        from scarletcoin.net.node import Node, NodeConfig

        config = NodeConfig(network="regtest", datadir=tmp_path, listen=False, rpc=False)
        node = Node(config)
        node.start()
        try:
            assert node.info()["ws_port"] == node.ws_hub.port
        finally:
            node.stop()
