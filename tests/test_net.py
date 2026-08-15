"""Tests for the wire protocol, the node's peer-to-peer behaviour and the RPC layer."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from scarletcoin import __version__
from scarletcoin.core.params import REGTEST
from scarletcoin.net import protocol
from scarletcoin.net.addrbook import AddressBook, parse_address
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.net.node import Node, NodeConfig
from scarletcoin.net.protocol import InvItem, InvType, ProtocolError
from scarletcoin.net.rpc import RpcServer
from tests.conftest import wait_until
from tests.helpers import mine_block, spend


class TestProtocol:
    @pytest.mark.parametrize(
        "message",
        [
            protocol.Version(1, "/test/", 42, 12345, 20333, 1700000000),
            protocol.VerAck(),
            protocol.Ping(7),
            protocol.Pong(7),
            protocol.GetAddr(),
            protocol.Mempool(),
            protocol.Addr((protocol.NetworkAddress("example.org", 20333, 1700000000),)),
            protocol.Inv((InvItem(InvType.BLOCK, b"\x01" * 32),)),
            protocol.GetData((InvItem(InvType.TX, b"\x02" * 32),)),
            protocol.NotFound((InvItem(InvType.TX, b"\x03" * 32),)),
            protocol.GetBlocks((b"\x04" * 32, b"\x05" * 32)),
            protocol.BlockMessage(REGTEST.genesis_block),
            protocol.TxMessage(REGTEST.genesis_coinbase),
        ],
    )
    def test_round_trip(self, message):
        framed = protocol.encode_message(REGTEST.magic, message)
        command, length, checksum = protocol.parse_header(framed[:24], REGTEST.magic)
        payload = framed[24:]
        assert length == len(payload)
        assert command == message.command
        assert protocol.decode_payload(command, payload, checksum) == message

    def test_a_message_for_another_network_is_refused(self):
        framed = protocol.encode_message(b"XXXX", protocol.VerAck())
        with pytest.raises(ProtocolError, match="different network"):
            protocol.parse_header(framed[:24], REGTEST.magic)

    def test_a_bad_checksum_is_detected(self):
        framed = protocol.encode_message(REGTEST.magic, protocol.Ping(1))
        command, _, checksum = protocol.parse_header(framed[:24], REGTEST.magic)
        with pytest.raises(ProtocolError, match="checksum"):
            protocol.decode_payload(command, b"\x00" * 8, checksum)

    def test_unknown_commands_are_ignored(self):
        payload = b""
        from scarletcoin.crypto.hashing import hash256

        assert protocol.decode_payload("future", payload, hash256(payload)[:4]) is None

    def test_oversized_payloads_are_refused(self):
        header = REGTEST.magic + b"ping".ljust(12, b"\x00")
        header += (protocol.MAX_PAYLOAD + 1).to_bytes(4, "little") + b"\x00" * 4
        with pytest.raises(ProtocolError, match="too large"):
            protocol.parse_header(header, REGTEST.magic)

    def test_malformed_payloads_are_refused(self):
        from scarletcoin.crypto.hashing import hash256

        payload = b"\x01"
        with pytest.raises(ProtocolError, match="malformed"):
            protocol.decode_payload("ping", payload, hash256(payload)[:4])

    def test_inventory_items_are_validated(self):
        with pytest.raises(ProtocolError, match="32 bytes"):
            InvItem(InvType.BLOCK, b"\x00")
        with pytest.raises(ProtocolError, match="unknown inventory type"):
            InvItem(9, b"\x00" * 32)

    def test_inventory_messages_are_size_limited(self):
        items = tuple(
            InvItem(InvType.BLOCK, index.to_bytes(32, "big"))
            for index in range(protocol.MAX_INV_ITEMS + 1)
        )
        with pytest.raises(ProtocolError, match="too many"):
            protocol.Inv(items).encode()


class TestAddressBook:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("example.org", ("example.org", 20333)),
            ("example.org:1234", ("example.org", 1234)),
            ("127.0.0.1:9", ("127.0.0.1", 9)),
            ("[::1]:9", ("::1", 9)),
            ("::1", ("::1", 20333)),
        ],
    )
    def test_parse_address(self, text, expected):
        assert parse_address(text, 20333) == expected

    @pytest.mark.parametrize("text", ["", "host:0", "host:70000", "host:abc"])
    def test_parse_address_rejects_junk(self, text):
        with pytest.raises(ValueError):
            parse_address(text, 20333)

    def test_persistence(self, tmp_path):
        path = tmp_path / "peers.json"
        book = AddressBook(path)
        book.add("example.org", 20333, source="seed")
        book.save()
        assert len(AddressBook(path)) == 1

    def test_a_corrupt_file_is_ignored(self, tmp_path):
        path = tmp_path / "peers.json"
        path.write_text("not json")
        assert len(AddressBook(path)) == 0

    def test_failures_eventually_drop_an_address(self):
        book = AddressBook()
        book.add("example.org", 20333)
        for _ in range(10):
            book.mark_failure("example.org", 20333)
        assert len(book) == 0

    def test_banning_removes_and_blocks_a_host(self):
        book = AddressBook()
        book.add("example.org", 20333)
        book.ban("example.org", seconds=60)
        assert book.is_banned("example.org")
        assert len(book) == 0
        assert book.candidates(set()) == []

    def test_a_ban_expires(self):
        book = AddressBook()
        book.ban("example.org", seconds=-1)
        assert not book.is_banned("example.org")


class TestRpc:
    def test_chain_queries(self, rpc):
        _, _, client = rpc
        hashes = client.call("generate", 3)
        assert client.getblockcount() == 3
        assert client.call("getbestblockhash") == hashes[-1]
        assert client.call("getblockhash", 1) == hashes[0]

        block = client.call("getblock", hashes[0])
        assert block["height"] == 1
        assert block["confirmations"] == 3
        assert block["in_active_chain"] is True
        header = client.call("getblockheader", 1)
        assert header["hash"] == hashes[0]
        assert bytes.fromhex(client.call("getrawblock", 1))

    def test_getinfo_identifies_the_chain(self, rpc):
        _, _, client = rpc
        info = client.getinfo()
        # These are what two operators compare to prove they run the same chain.
        assert info["genesis"] == REGTEST.genesis_hash[::-1].hex()
        assert info["magic"] == "SCRR"
        assert info["protocol_version"] == 1
        assert info["version"] == __version__

    def test_getinfo_and_supply(self, rpc):
        _, _, client = rpc
        client.call("generate", 2)
        info = client.getinfo()
        assert info["network"] == "regtest"
        assert info["height"] == 2
        assert info["supply"] == REGTEST.subsidy(0) * 3
        assert client.call("getsupply")["supply"] == info["supply"]
        assert client.call("getdifficulty") == 1.0

    def test_address_and_transaction_queries(self, rpc, key):
        _, _, client = rpc
        address = str(key.address(REGTEST.address_version))
        client.call("generate", 3, address)

        balance = client.getbalance(address)
        assert balance["balance"] == REGTEST.subsidy(0) * 3
        assert balance["immature"] > 0
        utxos = client.getutxos(address)
        assert len(utxos["utxos"]) == 3
        assert utxos["utxos"][0]["coinbase"] is True

        coinbase_txid = client.call("getblock", 1)["transactions"][0]["txid"]
        transaction = client.call("gettransaction", coinbase_txid)
        assert transaction["coinbase"] is True
        assert transaction["confirmations"] == 3
        assert transaction["outputs"][0]["address"] == address
        assert bytes.fromhex(client.call("getrawtransaction", coinbase_txid))

        history = client.getaddresshistory(address)
        assert len(history["transactions"]) == 3
        rich = client.call("getrichlist", 5)
        assert rich[0]["address"] == address

    def test_validateaddress(self, rpc, key):
        _, _, client = rpc
        good = str(key.address(REGTEST.address_version))
        assert client.call("validateaddress", good)["valid"] is True
        assert client.call("validateaddress", "nonsense")["valid"] is False
        mainnet = str(key.address(63))
        assert client.call("validateaddress", mainnet)["valid"] is False

    def test_sending_a_raw_transaction(self, rpc, key, other_key):
        node, _, client = rpc
        address = str(key.address(REGTEST.address_version))
        client.call("generate", 4, address)
        transaction = spend(node.chain, key, other_key.address(REGTEST.address_version), 10**8)
        txid = client.sendrawtransaction(transaction.serialize().hex())
        assert txid == transaction.txid_hex()
        pool = client.call("getmempool")
        assert pool["count"] == 1
        assert pool["transactions"][0]["txid"] == txid
        # sending it twice is refused
        with pytest.raises(RpcClientError, match="already in the mempool"):
            client.sendrawtransaction(transaction.serialize().hex())

    def test_block_template_and_submission(self, rpc, key):
        _, _, client = rpc
        from scarletcoin.core.template import BlockTemplate
        from scarletcoin.miner.solver import solve_block

        template = BlockTemplate.from_dict(client.getblocktemplate())
        assert template.height == 1
        candidate = template.build_block(pubkey_hash=key.public_key().hash160())
        solved = solve_block(candidate)
        result = client.submitblock(solved.serialize().hex())
        assert result["status"] == "connected"
        assert result["height"] == 1
        assert client.getblockcount() == 1

    def test_a_bad_block_is_refused(self, rpc, key):
        node, _, client = rpc
        block = mine_block(node.chain, key)
        raw = bytearray(block.serialize())
        raw[40] ^= 0xFF  # corrupt the Merkle root
        with pytest.raises(RpcClientError, match="rejected"):
            client.submitblock(bytes(raw).hex())
        assert client.getblockcount() == 0

    def test_a_block_without_a_known_parent_is_reported_as_an_orphan(self, rpc, key):
        from tests.helpers import make_node_state, mine_and_add

        _, _, client = rpc
        elsewhere, pool = make_node_state()
        try:
            blocks = mine_and_add(elsewhere, key, pool, count=2)
        finally:
            elsewhere.storage.close()
        # The second block's parent is unknown to our node.
        assert client.submitblock(blocks[1].serialize().hex())["status"] == "orphan"
        assert client.getblockcount() == 0
        # Once the parent arrives, both connect.
        assert client.submitblock(blocks[0].serialize().hex())["status"] == "connected"
        assert client.getblockcount() == 2

    def test_errors_are_reported(self, rpc):
        _, _, client = rpc
        with pytest.raises(RpcClientError, match="unknown method"):
            client.call("dance")
        with pytest.raises(RpcClientError, match="no block at height"):
            client.call("getblockhash", 999)
        with pytest.raises(RpcClientError, match="not valid hexadecimal"):
            client.call("gettransaction", "zz")
        with pytest.raises(RpcClientError, match="bad parameters"):
            client.call("getblockhash", 1, 2, 3)

    def test_authentication_is_required(self, rpc):
        _, server, _ = rpc
        anonymous = RpcClient(server.url, timeout=10)
        with pytest.raises(RpcClientError, match="401"):
            anonymous.getinfo()
        wrong = RpcClient(server.url, token="nope", timeout=10)
        with pytest.raises(RpcClientError, match="401"):
            wrong.getinfo()

    def test_batch_requests(self, rpc):
        _, server, _ = rpc
        body = json.dumps(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "getblockcount"},
                {"jsonrpc": "2.0", "id": 2, "method": "getdifficulty"},
            ]
        ).encode()
        answer = server.handle_rpc(body)
        assert [item["id"] for item in answer] == [1, 2]

    def test_malformed_json(self, rpc):
        _, server, _ = rpc
        assert server.handle_rpc(b"{oops")["error"]["code"] == -32700
        assert server.handle_rpc(b'{"id": 1}')["error"]["code"] == -32600

    def test_named_parameters(self, rpc, key):
        _, _, client = rpc
        client.call("generate", count=2, address=str(key.address(REGTEST.address_version)))
        assert client.getblockcount() == 2

    def test_generate_is_only_available_on_regtest(self, tmp_path):
        from scarletcoin.net.rpc import build_methods

        config = NodeConfig(network="mainnet", datadir=tmp_path / "main", listen=False, rpc=False)
        node = Node(config)
        try:
            assert "generate" not in build_methods(node)
        finally:
            node.stop()


class TestPublicRpc:
    """A node started with --rpc-public serves wallets without handing over control."""

    def _server(self, node, **kwargs) -> RpcServer:
        server = RpcServer(node, port=0, token="secret", **kwargs)
        server.start()
        return server

    def test_public_methods_work_without_a_token(self, tmp_path, key):
        node = Node(
            NodeConfig(
                network="regtest", datadir=tmp_path / "pub", listen=False, rpc=False, p2p_port=0
            )
        )
        node.start()
        server = self._server(node, public=True)
        try:
            owner = RpcClient(server.url, token="secret", timeout=10)
            owner.call("generate", 3, str(key.address(REGTEST.address_version)))

            anonymous = RpcClient(server.url, timeout=10)
            assert anonymous.getblockcount() == 3
            assert anonymous.getinfo()["network"] == "regtest"
            stats = anonymous.call("getnetworkstats")
            assert stats["height"] == 3
            assert stats["difficulty"] > 0
            assert stats["blocks_last_day"] >= 3
            assert anonymous.getbalance(str(key.address(REGTEST.address_version)))["balance"] > 0
            assert anonymous.call("getblock", 1)["height"] == 1
            assert anonymous.call("getmempool")["count"] == 0
        finally:
            server.stop()
            node.stop()

    def test_private_methods_still_need_the_token(self, tmp_path):
        node = Node(
            NodeConfig(
                network="regtest", datadir=tmp_path / "pub2", listen=False, rpc=False, p2p_port=0
            )
        )
        node.start()
        server = self._server(node, public=True)
        try:
            anonymous = RpcClient(server.url, timeout=10)
            for method, params in (
                ("generate", [1]),
                ("stop", []),
                ("addpeer", ["127.0.0.1"]),
                ("getpeers", []),
                ("getaddresses", []),
                ("getblocktemplate", []),
            ):
                with pytest.raises(RpcClientError, match="needs the node's RPC token"):
                    anonymous.call(method, *params)
            assert node.chain.height == 0  # nothing happened
        finally:
            server.stop()
            node.stop()

    def test_a_wallet_can_broadcast_through_a_public_node(self, tmp_path, key, other_key):
        node = Node(
            NodeConfig(
                network="regtest", datadir=tmp_path / "pub3", listen=False, rpc=False, p2p_port=0
            )
        )
        node.start()
        server = self._server(node, public=True)
        try:
            RpcClient(server.url, token="secret", timeout=10).call(
                "generate", 4, str(key.address(REGTEST.address_version))
            )
            transaction = spend(node.chain, key, other_key.address(REGTEST.address_version), 10**8)
            anonymous = RpcClient(server.url, timeout=10)
            assert (
                anonymous.sendrawtransaction(transaction.serialize().hex())
                == transaction.txid_hex()
            )
            assert transaction.txid() in node.mempool
        finally:
            server.stop()
            node.stop()

    def test_public_mode_is_off_by_default(self, rpc):
        _, server, _ = rpc
        assert server.public is False
        anonymous = RpcClient(server.url, timeout=10)
        with pytest.raises(RpcClientError, match="401"):
            anonymous.getblockcount()

    def test_a_batch_is_checked_request_by_request(self, rpc):
        _, server, _ = rpc
        server.public = True
        answer = server.handle_rpc(
            json.dumps(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "getblockcount"},
                    {"jsonrpc": "2.0", "id": 2, "method": "stop"},
                ]
            ).encode(),
            authorised=False,
        )
        assert answer[0]["result"] == 0
        assert answer[1]["error"]["code"] == -32001
        server.public = False

    def test_the_public_set_covers_what_a_wallet_needs(self):
        from scarletcoin.net.rpc import PUBLIC_METHODS

        needed = {
            "getinfo",
            "getblockcount",
            "getnetworkstats",
            "getbalance",
            "getutxos",
            "getaddresshistory",
            "sendrawtransaction",
        }
        assert needed <= PUBLIC_METHODS
        # and nothing that controls the node
        assert not PUBLIC_METHODS & {"stop", "generate", "addpeer", "submitblock"}


class TestExplorer:
    def _get(self, url: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_pages_render(self, rpc, key, other_key):
        node, server, client = rpc
        address = str(key.address(REGTEST.address_version))
        client.call("generate", 4, address)
        transaction = spend(node.chain, key, other_key.address(REGTEST.address_version), 10**8)
        client.sendrawtransaction(transaction.serialize().hex())

        for path in (
            "/",
            "/blocks",
            "/blocks?from=2",
            "/mempool",
            "/peers",
            "/rich",
            "/block/1",
            f"/block/{client.call('getblockhash', 1)}",
            f"/tx/{transaction.txid_hex()}",
            f"/address/{address}",
            f"/search?q={address}",
            "/search?q=1",
            f"/search?q={transaction.txid_hex()}",
        ):
            status, body = self._get(server.url + path)
            assert status == 200, path
            assert "ScarletCoin" in body, path

    def test_overview_shows_network_statistics(self, rpc):
        _, server, client = rpc
        client.call("generate", 5)
        status, body = self._get(server.url + "/")
        assert status == 200
        for marker in ("Block rate", "Hash rate", "Next retarget", "Blocks last hour"):
            assert marker in body
        assert "H/s" in body
        assert "Measured over the last" in body

    def test_missing_pages_answer_404(self, rpc):
        _, server, _ = rpc
        for path in ("/nowhere", "/block/999", "/tx/" + "00" * 32, "/address/nonsense"):
            status, body = self._get(server.url + path)
            assert status == 404, path
            assert "Not found" in body

    def test_table_cells_render_as_markup_not_as_source(self, rpc, key, other_key):
        """Amounts and links inside tables must not arrive HTML-escaped."""
        node, server, client = rpc
        address = str(key.address(REGTEST.address_version))
        client.call("generate", 4, address)
        transaction = spend(node.chain, key, other_key.address(REGTEST.address_version), 10**8)
        client.sendrawtransaction(transaction.serialize().hex())
        client.call("generate", 1, address)

        with_amounts = (
            "/",
            "/blocks",
            "/rich",
            "/block/5",
            f"/tx/{transaction.txid_hex()}",
            f"/address/{address}",
        )
        for path in (*with_amounts, "/peers"):
            status, body = self._get(server.url + path)
            assert status == 200, path
            # Escaped markup would show up as literal text in the page.
            for leaked in ("&lt;span", "&lt;a href", "&lt;/span&gt;", "&lt;em&gt;"):
                assert leaked not in body, f"{leaked} leaked into {path}"
            if path in with_amounts:
                assert '<span class="amount">' in body, path

    def test_mempool_page_shows_amounts_as_markup(self, rpc, key, other_key):
        node, server, client = rpc
        client.call("generate", 4, str(key.address(REGTEST.address_version)))
        transaction = spend(node.chain, key, other_key.address(REGTEST.address_version), 10**8)
        client.sendrawtransaction(transaction.serialize().hex())
        status, body = self._get(server.url + "/mempool")
        assert status == 200
        assert "&lt;span" not in body
        assert transaction.txid_hex()[:16] in body

    def test_api_info(self, rpc):
        _, server, client = rpc
        client.call("generate", 1)
        status, body = self._get(server.url + "/api/info")
        assert status == 200
        assert json.loads(body)["height"] == 1

    def test_explorer_escapes_hostile_content(self, rpc):
        _, server, _ = rpc
        status, body = self._get(server.url + "/search?q=%3Cscript%3Ealert(1)%3C/script%3E")
        assert status == 404
        assert "<script>" not in body
        assert "&lt;script&gt;" in body


class TestSeeds:
    def test_mainnet_publishes_usable_seeds(self):
        """A typo in the shipped seed list would leave every new node alone."""
        from scarletcoin.core.params import MAINNET

        assert MAINNET.seeds, "mainnet must publish at least one seed"
        for seed in MAINNET.seeds:
            host, port = parse_address(seed, MAINNET.default_p2p_port)
            assert host
            assert 1 <= port <= 65535
        # A name for mobility, and a literal address as a DNS-independent fallback.
        assert any(not entry.replace(".", "").isdigit() for entry in MAINNET.seeds)
        assert any(entry.replace(".", "").isdigit() for entry in MAINNET.seeds)

    def _config(self, tmp_path, **overrides) -> NodeConfig:
        return NodeConfig(
            network="regtest",
            datadir=tmp_path / "seeded",
            listen=False,
            rpc=False,
            p2p_port=0,
            **overrides,
        )

    def test_seed_hosts_combine_the_build_in_and_the_configured_ones(self, tmp_path):
        node = Node(self._config(tmp_path, seeds=("example.org",)))
        try:
            assert node.seed_hosts == (*REGTEST.seeds, "example.org")
        finally:
            node.stop()

    def test_no_seeds_ignores_the_ones_in_the_build(self, tmp_path):
        node = Node(self._config(tmp_path, seeds=("example.org",), use_seeds=False))
        try:
            assert node.seed_hosts == ("example.org",)
        finally:
            node.stop()

    def test_a_seed_name_is_resolved_to_its_addresses(self, tmp_path):
        node = Node(self._config(tmp_path, seeds=("localhost:41999",)))
        try:
            node._bootstrap_seeds()
            entries = {(entry.host, entry.port, entry.source) for entry in node.addrbook.all()}
            assert ("localhost", 41999, "seed") in entries
            resolved = {host for host, port, source in entries if source == "dns"}
            assert resolved & {"127.0.0.1", "::1"}
            assert all(port == 41999 for _, port, _ in entries)
        finally:
            node.stop()

    def test_an_unresolvable_seed_is_only_a_warning(self, tmp_path):
        node = Node(self._config(tmp_path, seeds=("no-such-host.invalid",)))
        try:
            node._bootstrap_seeds()  # must not raise
            hosts = {entry.host for entry in node.addrbook.all()}
            assert hosts == {"no-such-host.invalid"}
        finally:
            node.stop()

    def test_a_malformed_seed_is_ignored(self, tmp_path):
        node = Node(self._config(tmp_path, seeds=("host:not-a-port",)))
        try:
            node._bootstrap_seeds()
            assert len(node.addrbook) == 0
        finally:
            node.stop()

    def test_addnode_peers_are_remembered(self, tmp_path):
        node = Node(self._config(tmp_path, connect=("example.org:1234",)))
        try:
            entries = {(entry.host, entry.port, entry.source) for entry in node.addrbook.all()}
            assert ("example.org", 1234, "config") in entries
        finally:
            node.stop()


class TestPeerToPeer:
    def _node(self, tmp_path, name: str, **overrides) -> Node:
        node = Node(
            NodeConfig(
                network="regtest",
                datadir=tmp_path / name,
                p2p_port=0,
                rpc=False,
                use_seeds=False,
                **overrides,
            )
        )
        node.start()
        return node

    def test_two_nodes_shake_hands_and_gossip(self, tmp_path):
        first = self._node(tmp_path, "a")
        second = self._node(tmp_path, "b")
        try:
            assert second.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(lambda: len(first.peers) == 1 and len(second.peers) == 1)
            peer = second.peers[0]
            assert peer.handshake_done.wait(10)
            assert peer.user_agent.startswith("/scarletcoin:")
            # the listening port is gossiped, so the address book learns about it
            assert wait_until(lambda: len(second.addrbook) >= 1)
        finally:
            first.stop()
            second.stop()

    def test_blocks_propagate(self, tmp_path, key):
        first = self._node(tmp_path, "a")
        second = self._node(tmp_path, "b")
        try:
            second.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(
                lambda: bool(second.peers) and second.peers[0].handshake_done.is_set()
            )
            for _ in range(3):
                first.submit_block(mine_block(first.chain, key))
            assert wait_until(lambda: second.chain.height == 3)
            assert second.chain.tip_hash == first.chain.tip_hash
        finally:
            first.stop()
            second.stop()

    def test_a_new_node_catches_up(self, tmp_path, key):
        first = self._node(tmp_path, "a")
        try:
            for _ in range(6):
                first.submit_block(mine_block(first.chain, key))
            second = self._node(tmp_path, "b", connect=(f"127.0.0.1:{first.p2p_port}",))
            try:
                assert wait_until(lambda: second.chain.height == 6, timeout=30)
                assert second.chain.tip_hash == first.chain.tip_hash
            finally:
                second.stop()
        finally:
            first.stop()

    def test_transactions_propagate(self, tmp_path, key, other_key):
        first = self._node(tmp_path, "a")
        second = self._node(tmp_path, "b")
        try:
            second.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(
                lambda: bool(second.peers) and second.peers[0].handshake_done.is_set()
            )
            for _ in range(4):
                first.submit_block(mine_block(first.chain, key))
            assert wait_until(lambda: second.chain.height == 4)

            transaction = spend(first.chain, key, other_key.address(REGTEST.address_version), 10**8)
            first.submit_transaction(transaction)
            assert wait_until(lambda: transaction.txid() in second.mempool)
        finally:
            first.stop()
            second.stop()

    def test_a_reorg_propagates(self, tmp_path, key, other_key):
        first = self._node(tmp_path, "a")
        second = self._node(tmp_path, "b")
        try:
            for _ in range(2):
                first.submit_block(mine_block(first.chain, key))
            for _ in range(4):
                second.submit_block(mine_block(second.chain, other_key))
            assert first.chain.height == 2
            assert second.chain.height == 4

            first.connect_peer("127.0.0.1", second.p2p_port)
            assert wait_until(lambda: first.chain.height == 4, timeout=30)
            assert first.chain.tip_hash == second.chain.tip_hash
        finally:
            first.stop()
            second.stop()

    def test_an_invalid_block_gets_the_peer_banned(self, tmp_path, key):
        first = self._node(tmp_path, "a")
        second = self._node(tmp_path, "b")
        try:
            second.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(lambda: bool(first.peers) and bool(second.peers))
            peer = second.peers[0]
            assert peer.handshake_done.wait(10)

            from scarletcoin.core.block import Block

            # Two blocks whose Merkle root does not match their transactions.
            for mask in (0x01, 0x02):
                block = mine_block(second.chain, key)
                raw = bytearray(block.serialize())
                raw[40] ^= mask  # corrupt the Merkle root
                peer.send(protocol.BlockMessage(Block.deserialize(bytes(raw))))
            assert wait_until(lambda: first.addrbook.is_banned("127.0.0.1"), timeout=20)
        finally:
            first.stop()
            second.stop()

    def test_a_node_recovers_a_missed_announcement(self, tmp_path, key):
        """A node that never heard an `inv` still catches up on the slow poll."""
        first = self._node(tmp_path, "a")
        second = self._node(tmp_path, "b")
        try:
            second.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(
                lambda: bool(second.peers) and second.peers[0].handshake_done.is_set()
            )
            # Add blocks straight to the chain, so nothing is relayed.
            for _ in range(3):
                first.chain.add_block(mine_block(first.chain, key))
            assert first.chain.height == 3
            assert second.chain.height == 0

            second._poll_for_blocks()
            assert wait_until(lambda: second.chain.height == 3, timeout=20)
            assert second.chain.tip_hash == first.chain.tip_hash
        finally:
            first.stop()
            second.stop()

    def test_the_stale_tip_threshold_follows_the_block_spacing(self, node):
        assert node.stale_tip_seconds >= 10 * REGTEST.target_spacing
        assert node.stale_tip_seconds >= 300

    def test_the_same_peer_is_not_connected_twice(self, tmp_path):
        """A seed reachable as both a name and an address must not use two slots."""
        first = self._node(tmp_path, "a")
        second = self._node(tmp_path, "b")
        try:
            assert second.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(
                lambda: bool(second.peers) and second.peers[0].handshake_done.is_set()
            )
            second.connect_peer("localhost", first.p2p_port)
            assert wait_until(lambda: len(second.peers) == 1, timeout=15)
            assert len(first.peers) == 1
        finally:
            first.stop()
            second.stop()

    def test_a_seed_node_stops_dialling_itself(self, tmp_path):
        """The node that *is* the seed must not dial its own name in a loop."""
        node = Node(
            NodeConfig(
                network="regtest",
                datadir=tmp_path / "seednode",
                p2p_port=0,
                rpc=False,
                use_seeds=False,
            )
        )
        node.start()
        try:
            port = node.p2p_port
            # Publish our own address, the way a seed operator's own node ends up
            # with itself in its address book.
            node.addrbook.add("127.0.0.1", port, source="seed")
            node.connect_peer("127.0.0.1", port)
            assert wait_until(lambda: ("127.0.0.1", port) in node.local_addresses, timeout=15)

            # It is gone from the address book and never becomes a candidate again.
            assert ("127.0.0.1", port) not in {e.key for e in node.addrbook.all()}
            assert node.addrbook.candidates(node.local_addresses) == []
            # Re-resolving the seeds must not put it back.
            node._add_address(f"127.0.0.1:{port}", source="seed")
            assert len(node.addrbook) == 0
            assert f"127.0.0.1:{port}" in node.info()["own_addresses"]

            # And no connection survives.
            assert wait_until(lambda: len(node.peers) == 0, timeout=15)
        finally:
            node.stop()

    def test_a_node_refuses_to_connect_to_itself(self, tmp_path):
        node = self._node(tmp_path, "a")
        try:
            node.connect_peer("127.0.0.1", node.p2p_port)
            assert wait_until(lambda: len(node.peers) == 0, timeout=10)
        finally:
            node.stop()
