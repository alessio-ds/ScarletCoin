"""Tests for starting a node as a child process (the GUI's local-node feature)."""

from __future__ import annotations

import socket

import pytest

from scarletcoin.net.launcher import (
    LocalNodeError,
    already_running,
    generate_token,
    node_command,
    start_local_node,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestNodeCommand:
    def test_prefers_the_console_script_next_to_the_interpreter(self):
        command = node_command(network="regtest", datadir=".", rpc_port=1234, rpc_token="tok")
        assert command[0].endswith("scarlet-node")
        assert "run" in command
        for flag in ("--network", "--datadir", "--rpc-host", "--rpc-port", "--rpc-token"):
            assert flag in command
        assert "127.0.0.1" in command
        assert "--p2p-port" not in command

    def test_p2p_port_and_extra_flags_are_forwarded(self):
        command = node_command(
            network="regtest",
            datadir=".",
            rpc_port=1234,
            rpc_token="tok",
            p2p_port=0,
            extra=("--no-seeds",),
        )
        assert "--p2p-port" in command
        assert "--no-seeds" in command

    def test_tokens_are_safe_as_command_line_arguments(self):
        """A token starting with '-' would be eaten by argparse as an option."""
        tokens = [generate_token() for _ in range(400)]
        assert all(token[0].isalpha() for token in tokens)
        command = node_command(network="regtest", datadir=".", rpc_port=1234, rpc_token=tokens[0])
        assert command[command.index("--rpc-token") + 1] == tokens[0]


@pytest.mark.slow
class TestStartLocalNode:
    def test_starts_answers_writes_token_and_stops(self, tmp_path):
        datadir = tmp_path / "data"
        port = free_port()
        node = start_local_node(
            network="regtest",
            datadir=datadir,
            rpc_port=port,
            p2p_port=0,
            extra=("--no-seeds",),
        )
        try:
            assert node.running
            assert node.is_ready()
            info = node.client().getinfo()
            assert info["network"] == "regtest"
            # The token lands where the other CLI tools expect it.
            assert (datadir / "regtest" / "rpc.token").read_text() == node.token
            # A random passphrase keeps the port from being probed.
            assert len(node.token) >= 40
        finally:
            node.stop()
        assert not node.running

    def test_already_running(self, tmp_path):
        assert already_running("http://127.0.0.1:9", timeout=0.5) is False
        datadir = tmp_path / "data2"
        port = free_port()
        node = start_local_node(
            network="regtest",
            datadir=datadir,
            rpc_port=port,
            p2p_port=0,
            extra=("--no-seeds",),
        )
        try:
            assert already_running(node.url, token=node.token, timeout=1.0)
        finally:
            node.stop()

    def test_refuses_to_start_a_second_node_on_a_taken_port(self, tmp_path):
        """A second node must fail cleanly instead of crashing on the port."""
        from scarletcoin.net.launcher import LocalNode

        port = free_port()
        first = start_local_node(
            network="regtest",
            datadir=tmp_path / "a",
            rpc_port=port,
            p2p_port=0,
            extra=("--no-seeds",),
        )
        try:
            with pytest.raises(LocalNodeError, match="already in use"):
                LocalNode.launch(
                    network="regtest", datadir=tmp_path / "b", rpc_port=port, p2p_port=0
                )
        finally:
            first.stop()

    def test_wait_until_ready_reports_a_dead_process(self, tmp_path):
        from scarletcoin.net.launcher import LocalNode

        port = free_port()
        node = LocalNode.launch(
            network="regtest",
            datadir=tmp_path / "dead",
            rpc_port=port,
            p2p_port=0,
            extra=("--no-seeds", "--definitely-not-a-flag"),
        )
        with pytest.raises(LocalNodeError):
            node.wait_until_ready(timeout=20, poll=0.05)
        assert not node.running
        assert node.is_ready() is False  # the object must not crash asking
