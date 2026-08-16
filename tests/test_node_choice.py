"""Choosing a node: the public-node directory, and how the tools decide.

The point of all this is that somebody who has just installed ScarletCoin gets a
working wallet without reading anything, and that the choice they make — their
own node or somebody else's — is theirs and is remembered.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator

import pytest

from scarletcoin.cli_common import (
    NodeConnection,
    connection_path,
    forget_connection,
    load_connection,
    local_url,
    save_connection,
    write_rpc_token,
)
from scarletcoin.core.params import MAINNET, REGTEST
from scarletcoin.net import directory
from scarletcoin.net.chooser import NodeChoiceError, describe_local_chain, resolve_connection
from scarletcoin.net.node import Node, NodeConfig
from scarletcoin.net.rpc import MINING_METHODS, PUBLIC_METHODS, RpcServer


@pytest.fixture
def public_node(tmp_path) -> Iterator[tuple[Node, RpcServer]]:
    """A node anybody may read from, and mine through, without a token."""
    config = NodeConfig(
        network="regtest",
        datadir=tmp_path / "publicnode",
        listen=False,
        p2p_port=0,
        rpc_port=0,
        use_seeds=False,
        rpc_public=True,
        rpc_public_mining=True,
        rpc_advertise="https://public.example",
        public_peers=("https://friend.example",),
    )
    node = Node(config)
    node.start()
    server = RpcServer(node, port=0, token="secret", public=True, public_mining=True)
    server.start()
    yield node, server
    server.stop()
    node.stop()


def _args(tmp_path, **overrides) -> argparse.Namespace:
    values = {
        "network": "regtest",
        "datadir": tmp_path,
        "rpc_url": None,
        "rpc_token": None,
        "timeout": 5.0,
        "node": None,
        "start_node": False,
        "no_start_node": True,
        "forget_node": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class TestNormaliseUrl:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("scarletcoin.remotewire.net", "https://scarletcoin.remotewire.net"),
            ("https://node.example/", "https://node.example"),
            ("HTTPS://Node.Example", "https://node.example"),
            ("node.example:20332", "http://node.example:20332"),
            ("192.168.1.5:20332", "http://192.168.1.5:20332"),
            ("http://node.example/path?x=1#y", "http://node.example"),
            ("[::1]:20332", "http://[::1]:20332"),
            ("http://[::1]:20332", "http://[::1]:20332"),
        ],
    )
    def test_it_accepts_what_people_type(self, text, expected):
        assert directory.normalise_url(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "not a url", "ftp://node.example", "bad_host!", "http://node.example:99999"],
    )
    def test_it_rejects_what_is_not_an_address(self, text):
        """A "host" with a space in it must be caught here, not by urllib."""
        assert directory.normalise_url(text) == ""


class TestCandidates:
    def test_the_release_knows_where_the_public_mainnet_node_is(self):
        urls = [node.url for node in directory.candidates("mainnet")]
        assert "https://scarletcoin.remotewire.net" in urls
        assert MAINNET.public_nodes

    def test_a_typed_address_is_tried_before_the_built_in_ones(self, tmp_path):
        found = directory.candidates("mainnet", tmp_path, extra=("mine.example",))
        assert found[0].url == "https://mine.example"
        assert found[0].source == "typed"

    def test_saved_nodes_come_before_the_built_in_ones(self, tmp_path):
        directory.remember_node(tmp_path, "mainnet", "mine.example")
        sources = [node.source for node in directory.candidates("mainnet", tmp_path)]
        assert sources.index("saved") < sources.index("built-in")

    def test_the_environment_can_add_nodes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", "one.example,two.example")
        urls = [node.url for node in directory.candidates("regtest", tmp_path)]
        assert urls == ["https://one.example", "https://two.example"]

    def test_remembering_the_same_node_twice_does_not_duplicate_it(self, tmp_path):
        directory.remember_node(tmp_path, "regtest", "mine.example")
        directory.remember_node(tmp_path, "regtest", "https://mine.example/")
        saved = json.loads(directory.nodes_path(tmp_path, "regtest").read_text())
        assert saved == ["https://mine.example"]

    def test_a_node_can_be_forgotten(self, tmp_path):
        directory.remember_node(tmp_path, "regtest", "mine.example")
        directory.forget_node(tmp_path, "regtest", "mine.example")
        assert directory.user_nodes(tmp_path, "regtest") == []

    def test_rubbish_in_the_saved_file_is_ignored(self, tmp_path):
        path = directory.nodes_path(tmp_path, "regtest")
        path.parent.mkdir(parents=True)
        path.write_text('{"not": "a list"}')
        assert directory.user_nodes(tmp_path, "regtest") == []


class TestProbing:
    def test_a_public_node_answers_a_stranger(self, public_node):
        _, server = public_node
        status = directory.probe(directory.PublicNode(server.url))
        assert status.reachable
        assert status.network == "regtest"
        assert status.height == 0
        assert status.serves_mining is True
        assert status.latency is not None
        assert status.usable("regtest")
        assert status.usable("regtest", for_mining=True)
        assert not status.usable("mainnet")

    def test_a_private_node_is_reported_as_private_not_as_broken(self, rpc):
        _, server, _ = rpc
        status = directory.probe(directory.PublicNode(server.url))
        assert status.reachable is False
        assert status.needs_token is True
        assert "token" in status.describe()

    def test_an_unreachable_node_carries_its_reason(self):
        status = directory.probe(directory.PublicNode("http://127.0.0.1:1"), timeout=2.0)
        assert status.reachable is False
        assert status.needs_token is False
        assert status.error
        assert status.describe() == status.error

    def test_a_node_that_does_not_hand_out_work_is_usable_but_not_for_mining(self, tmp_path):
        config = NodeConfig(
            network="regtest",
            datadir=tmp_path / "readonly",
            listen=False,
            p2p_port=0,
            rpc_port=0,
            use_seeds=False,
        )
        node = Node(config)
        server = RpcServer(node, port=0, token="t", public=True)
        server.start()
        try:
            status = directory.probe(directory.PublicNode(server.url))
            assert status.usable("regtest")
            assert not status.usable("regtest", for_mining=True)
        finally:
            server.stop()
            node.stop()

    def test_the_best_node_is_the_one_that_is_furthest_along(self):
        def status(url: str, height: int, latency: float) -> directory.NodeStatus:
            return directory.NodeStatus(
                directory.PublicNode(url),
                reachable=True,
                network="regtest",
                height=height,
                latency=latency,
            )

        near = status("http://a", 5, 1.0)
        far = status("http://b", 9, 2.0)
        broken = directory.NodeStatus(directory.PublicNode("http://c"), error="no")
        ordered = sorted([broken, near, far], key=lambda status: status.sort_key("regtest"))
        assert [status.url for status in ordered] == ["http://b", "http://a", "http://c"]


class TestDiscovery:
    def test_a_public_node_passes_on_the_ones_it_knows(self, public_node, tmp_path, monkeypatch):
        node, server = public_node
        assert node.public_nodes() == ["https://public.example", "https://friend.example"]
        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", server.url)
        found = directory.discover("regtest", tmp_path, timeout=4.0)
        urls = [status.url for status in found]
        assert server.url in urls
        # The node's own list was followed, so nodes nobody had heard of turn up.
        assert "https://public.example" in urls
        assert "https://friend.example" in urls
        assert found[0].url == server.url  # the only one that actually answers

    def test_discovery_can_be_turned_off(self, public_node, tmp_path, monkeypatch):
        _, server = public_node
        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", server.url)
        found = directory.discover("regtest", tmp_path, timeout=4.0, follow=False)
        assert [status.url for status in found] == [server.url]

    def test_a_private_node_is_not_asked_for_referrals(self, rpc, tmp_path, monkeypatch):
        _, server, _ = rpc
        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", server.url)
        found = directory.discover("regtest", tmp_path, timeout=4.0)
        assert [status.url for status in found] == [server.url]
        assert found[0].needs_token


class TestPublicRpcSurface:
    def test_the_size_of_the_chain_is_public(self, public_node):
        _, server = public_node
        assert "getchainsize" in PUBLIC_METHODS
        answer = directory.RpcClient(server.url, timeout=5.0).call("getchainsize")
        assert answer["chain_bytes"] > 0
        assert answer["chain_size"].endswith("B")
        assert answer["disk_bytes"] >= answer["chain_bytes"]

    def test_getinfo_says_how_big_the_chain_is(self, rpc):
        _, _, client = rpc
        info = client.getinfo()
        for field in ("chain_bytes", "chain_size", "disk_bytes", "disk_size", "prune_height"):
            assert field in info
        assert info["chain_size"] == "214 B"

    def test_getinfo_says_whether_the_node_is_public(self, public_node, rpc):
        _, server = public_node
        public = directory.RpcClient(server.url, timeout=5.0).getinfo()
        assert public["public"] is True
        assert public["public_mining"] is True
        assert public["public_url"] == "https://public.example"
        _, _, private_client = rpc
        assert private_client.getinfo()["public"] is False

    def test_mining_needs_the_token_unless_it_was_opened_up(self, rpc, public_node):
        _, private, _ = rpc
        assert MINING_METHODS.isdisjoint(PUBLIC_METHODS)
        assert not private.allows_anonymous("getblocktemplate")
        _, opened = public_node
        assert opened.allows_anonymous("getblocktemplate")
        assert opened.allows_anonymous("submitblock")

    def test_public_mining_implies_public_reading(self, tmp_path):
        config = NodeConfig(
            network="regtest",
            datadir=tmp_path / "mineonly",
            listen=False,
            p2p_port=0,
            rpc_port=0,
            use_seeds=False,
        )
        node = Node(config)
        server = RpcServer(node, port=0, token="t", public=False, public_mining=True)
        try:
            assert server.public is True
            assert node.config.rpc_public is True
        finally:
            server.stop()
            node.stop()

    def test_pruning_stays_behind_the_token(self, public_node):
        _, server = public_node
        assert not server.allows_anonymous("prune")
        anonymous = directory.RpcClient(server.url, timeout=5.0)
        with pytest.raises(Exception, match="token"):
            anonymous.call("prune", 5000)

    def test_prune_over_rpc_reports_what_it_did(self, rpc):
        node, _, client = rpc
        client.call("generate", 20)
        result = client.call("prune", 5)
        assert result["blocks"] == 15
        assert result["prune_height"] == 15
        assert result["freed_size"]
        assert result["disk_size"]
        assert node.chain.height == 20

    def test_prune_without_a_target_is_refused(self, rpc):
        _, _, client = rpc
        with pytest.raises(Exception, match="number of recent blocks"):
            client.call("prune")


class TestRememberingTheChoice:
    def test_nothing_is_remembered_to_begin_with(self, tmp_path):
        assert load_connection(tmp_path, "regtest") is None

    def test_a_choice_survives_a_round_trip(self, tmp_path):
        save_connection(tmp_path, "regtest", NodeConnection("http://node.example:1", "tok"))
        assert load_connection(tmp_path, "regtest") == NodeConnection(
            "http://node.example:1", "tok"
        )

    def test_the_file_the_old_desktop_wrote_is_still_read(self, tmp_path):
        """Upgrading must not throw away an address somebody typed once."""
        legacy = tmp_path / "regtest" / "gui.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"rpc_url": "http://old.example:2", "rpc_token": "old"}))
        assert load_connection(tmp_path, "regtest") == NodeConnection("http://old.example:2", "old")

    def test_the_new_file_wins_over_the_old_one(self, tmp_path):
        legacy = tmp_path / "regtest" / "gui.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"rpc_url": "http://old.example:2"}))
        save_connection(tmp_path, "regtest", NodeConnection("http://new.example:3"))
        assert load_connection(tmp_path, "regtest").url == "http://new.example:3"

    def test_a_corrupt_file_is_ignored(self, tmp_path):
        path = connection_path(tmp_path, "regtest")
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        assert load_connection(tmp_path, "regtest") is None

    def test_forgetting_removes_both_files(self, tmp_path):
        save_connection(tmp_path, "regtest", NodeConnection("http://node.example:1"))
        forget_connection(tmp_path, "regtest")
        assert load_connection(tmp_path, "regtest") is None


class TestResolveConnection:
    def test_an_explicit_url_is_used_verbatim(self, tmp_path):
        args = _args(tmp_path, rpc_url="http://given.example:9/")
        assert resolve_connection(args).url == "http://given.example:9"

    def test_an_explicit_local_url_picks_up_the_nodes_token(self, tmp_path):
        write_rpc_token(tmp_path, "regtest", "from-file")
        args = _args(tmp_path, rpc_url=local_url("regtest"))
        assert resolve_connection(args).token == "from-file"

    def test_a_url_given_to_node_is_remembered(self, tmp_path):
        args = _args(tmp_path, node="node.example:20332")
        chosen = resolve_connection(args)
        assert chosen.url == "http://node.example:20332"
        assert load_connection(tmp_path, "regtest") == chosen

    def test_a_meaningless_node_option_is_reported_clearly(self, tmp_path):
        with pytest.raises(NodeChoiceError, match="is not a node address"):
            resolve_connection(_args(tmp_path, node="not a url"))

    def test_a_remembered_node_that_answers_is_used_without_a_word(self, public_node, tmp_path):
        _, server = public_node
        save_connection(tmp_path, "regtest", NodeConnection(server.url))
        assert resolve_connection(_args(tmp_path)).url == server.url

    def test_a_node_running_here_is_found_and_remembered(self, tmp_path, monkeypatch):
        """With no saved answer, a node already listening locally is the obvious one."""
        config = NodeConfig(
            network="regtest",
            datadir=tmp_path / "here",
            listen=False,
            p2p_port=0,
            rpc_port=REGTEST.default_rpc_port,
            use_seeds=False,
        )
        node = Node(config)
        server = RpcServer(node, port=REGTEST.default_rpc_port, token="here-token")
        try:
            server.start()
            write_rpc_token(tmp_path, "regtest", "here-token")
            chosen = resolve_connection(_args(tmp_path))
            assert chosen.url == local_url("regtest")
            assert chosen.token == "here-token"
            assert load_connection(tmp_path, "regtest") == chosen
        finally:
            server.stop()
            node.stop()

    def test_local_without_a_node_explains_how_to_start_one(self, tmp_path):
        with pytest.raises(NodeChoiceError, match="Start one with"):
            resolve_connection(_args(tmp_path, node="local"))

    def test_public_with_nothing_reachable_says_so(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", "http://127.0.0.1:1")
        with pytest.raises(NodeChoiceError, match="no public regtest node answered"):
            resolve_connection(_args(tmp_path, node="public"))

    def test_public_picks_the_best_one_without_asking(self, public_node, tmp_path, monkeypatch):
        _, server = public_node
        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", f"http://127.0.0.1:1,{server.url}")
        chosen = resolve_connection(_args(tmp_path, node="public"))
        assert chosen.url == server.url
        assert chosen.token == ""

    def test_forgetting_the_node_makes_it_choose_again(self, public_node, tmp_path, monkeypatch):
        _, server = public_node
        save_connection(tmp_path, "regtest", NodeConnection(server.url))
        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", "http://127.0.0.1:1")
        with pytest.raises(NodeChoiceError):
            resolve_connection(_args(tmp_path, forget_node=True, node="public"))
        assert load_connection(tmp_path, "regtest") is None


class TestDescribeLocalChain:
    def test_it_admits_when_there_is_nothing_stored(self, tmp_path):
        text = describe_local_chain("regtest", tmp_path)
        assert "no regtest chain here yet" in text
        assert "nothing stored here yet" in describe_local_chain("regtest", tmp_path, short=True)

    def test_it_reports_the_size_of_a_chain_that_exists(self, tmp_path, key):
        from scarletcoin.core.chain import Blockchain
        from scarletcoin.core.storage import Storage
        from tests.helpers import mine_and_add

        path = NodeConfig(network="regtest", datadir=tmp_path).chain_path
        storage = Storage(path)
        mine_and_add(Blockchain(storage, REGTEST), key, count=3)
        storage.close()

        assert "height 3" in describe_local_chain("regtest", tmp_path)
        assert "on disk" in describe_local_chain("regtest", tmp_path, short=True)
