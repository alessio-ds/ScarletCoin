"""Tests for the wire protocol, the node's peer-to-peer behaviour and the RPC layer."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import deque
from types import SimpleNamespace

import pytest

from scarletcoin import __version__
from scarletcoin.core.block import BlockHeader
from scarletcoin.core.chain import BlockStatus
from scarletcoin.core.params import REGTEST
from scarletcoin.net import protocol
from scarletcoin.net.addrbook import AddressBook, parse_address
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.net.node import Node, NodeConfig
from scarletcoin.net.protocol import InvItem, InvType, ProtocolError
from scarletcoin.net.rpc import RpcServer
from tests.conftest import wait_until
from tests.helpers import make_chain, mine_block, spend


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
            protocol.GetHeaders((b"\x04" * 32, b"\x05" * 32)),
            protocol.Headers((b"\x06" * 80, b"\x07" * 80)),
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
        assert info["protocol_version"] == 2
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

    def test_coins_spent_by_mempool_transactions_are_not_spendable(self, rpc, key, other_key):
        node, _, client = rpc
        address = str(key.address(REGTEST.address_version))
        client.call("generate", 4, address)

        before = client.getbalance(address)
        assert before["mempool_spent"] == 0
        assert before["mempool_spent_count"] == 0

        transaction = spend(node.chain, key, other_key.address(REGTEST.address_version), 10**8)
        client.sendrawtransaction(transaction.serialize().hex())

        # The coin this transaction spent is pooled: still listed, but not spendable.
        utxos = client.getutxos(address)
        spent = [item for item in utxos["utxos"] if item["mempool_spent"]]
        assert spent
        assert all(item["spendable"] is False for item in spent)

        after = client.getbalance(address)
        spent_value = sum(item["value"] for item in spent)
        assert after["mempool_spent"] == spent_value
        assert after["mempool_spent_count"] == len(spent)
        assert after["spendable"] == before["spendable"] - spent_value
        assert after["balance"] == before["balance"]
        assert after["balance"] == after["spendable"] + after["immature"] + after["mempool_spent"]

        # Once the transaction is mined the coin is gone from the chain entirely.
        client.call("generate", 1)
        final = client.getbalance(address)
        assert final["mempool_spent"] == 0
        assert final["mempool_spent_count"] == 0
        assert final["balance"] == final["spendable"] + final["immature"]
        assert final["utxo_count"] == before["utxo_count"]

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


class TestRpcClientRetry:
    """Transient connection drops are retried; real errors are not."""

    def _stub(self, monkeypatch, responses):
        """Replace urlopen with a callable returning or raising from a queue."""
        import contextlib
        from types import SimpleNamespace

        calls = []

        def fake_open(request, timeout=None, context=None):
            calls.append(1)
            outcome = responses.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            payload = json.dumps(outcome).encode("utf-8")
            response = SimpleNamespace(read=lambda: payload)
            return contextlib.nullcontext(response)

        monkeypatch.setattr("urllib.request.urlopen", fake_open)
        return calls

    def test_retries_a_truncated_response(self, monkeypatch):
        import http.client

        calls = self._stub(
            monkeypatch,
            [
                http.client.IncompleteRead(b"", 55),
                {"jsonrpc": "2.0", "id": 1, "result": {"network": "regtest"}},
            ],
        )
        client = RpcClient("http://127.0.0.1:1")
        assert client.getinfo()["network"] == "regtest"
        assert calls == [1, 1]

    def test_retries_a_reset_connection(self, monkeypatch):
        import urllib.error

        calls = self._stub(
            monkeypatch,
            [
                urllib.error.URLError(ConnectionResetError(104, "connection reset")),
                {"jsonrpc": "2.0", "id": 1, "result": "ok"},
            ],
        )
        client = RpcClient("http://127.0.0.1:1")
        assert client.call("ping") == "ok"
        assert len(calls) == 2

    def test_gives_up_after_the_last_attempt(self, monkeypatch):
        import http.client

        calls = self._stub(monkeypatch, [http.client.IncompleteRead(b"", 55)] * 3)
        client = RpcClient("http://127.0.0.1:1")
        with pytest.raises(RpcClientError, match="closed the connection"):
            client.getinfo()
        assert len(calls) == 3

    def test_does_not_retry_a_refused_connection(self, monkeypatch):
        import urllib.error

        calls = self._stub(
            monkeypatch, [urllib.error.URLError(ConnectionRefusedError(111, "refused"))]
        )
        client = RpcClient("http://127.0.0.1:1")
        with pytest.raises(RpcClientError, match="cannot reach"):
            client.getinfo()
        assert len(calls) == 1

    def test_does_not_retry_an_http_error(self, monkeypatch):
        class FlakyHTTPError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("http://127.0.0.1/rpc", 401, "unauthorised", None, None)

            def read(self, *args):  # pragma: no cover - not reached
                return b"{}"

        calls = self._stub(monkeypatch, [FlakyHTTPError()])
        client = RpcClient("http://127.0.0.1:1")
        with pytest.raises(RpcClientError, match="401"):
            client.getinfo()
        assert len(calls) == 1


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


class TestCors:
    """The CORS headers a browser wallet hosted elsewhere depends on."""

    def _get_headers(self, url: str, *, method: str = "GET", data: bytes | None = None):
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method=method
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers.items())

    def test_public_node_advertises_wildcard_cors(self, tmp_path):
        node = Node(
            NodeConfig(
                network="regtest", datadir=tmp_path / "cors1", listen=False, rpc=False, p2p_port=0
            )
        )
        node.start()
        server = RpcServer(node, port=0, token="secret", public=True)
        server.start()
        try:
            _, headers = self._get_headers(server.url + "/")
            assert headers["Access-Control-Allow-Origin"] == "*"
            _, headers = self._get_headers(
                server.url + "/rpc", method="POST", data=b'{"method":"getblockcount"}'
            )
            assert headers["Access-Control-Allow-Origin"] == "*"
            assert headers["Access-Control-Allow-Methods"] == "POST, GET, OPTIONS"
        finally:
            server.stop()
            node.stop()

    def test_private_node_serves_no_cors(self, tmp_path):
        node = Node(
            NodeConfig(
                network="regtest", datadir=tmp_path / "cors2", listen=False, rpc=False, p2p_port=0
            )
        )
        node.start()
        server = RpcServer(node, port=0, token="secret")
        server.start()
        try:
            _, headers = self._get_headers(server.url + "/")
            assert "Access-Control-Allow-Origin" not in headers
        finally:
            server.stop()
            node.stop()

    def test_preflight_options_is_answered(self, tmp_path):
        node = Node(
            NodeConfig(
                network="regtest", datadir=tmp_path / "cors3", listen=False, rpc=False, p2p_port=0
            )
        )
        node.start()
        server = RpcServer(node, port=0, token="secret", public=True)
        server.start()
        try:
            status, headers = self._get_headers(server.url + "/rpc", method="OPTIONS")
            assert status == 204
            assert headers["Access-Control-Allow-Origin"] == "*"
            assert "Content-Type" in headers["Access-Control-Allow-Headers"]
        finally:
            server.stop()
            node.stop()

    def test_an_explicit_origin_is_respected(self, tmp_path):
        node = Node(
            NodeConfig(
                network="regtest",
                datadir=tmp_path / "cors4",
                listen=False,
                rpc=False,
                p2p_port=0,
                rpc_public=True,
                rpc_cors="https://wallet.example.net",
            )
        )
        node.start()
        server = RpcServer(node, port=0, token="secret")
        server.start()
        try:
            _, headers = self._get_headers(server.url + "/")
            assert headers["Access-Control-Allow-Origin"] == "https://wallet.example.net"
            assert headers["Vary"] == "Origin"
        finally:
            server.stop()
            node.stop()


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
            "/hashrate",
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

    def test_address_page_caps_the_unspent_list(self, rpc, key):
        """An address with hundreds of coins must not crash or produce a huge page."""
        _, server, client = rpc
        address = str(key.address(REGTEST.address_version))
        client.call("generate", 205, address)

        status, body = self._get(server.url + f"/address/{address}")
        assert status == 200
        assert "… and 5 more" in body

    def test_overview_shows_network_statistics(self, rpc):
        _, server, client = rpc
        client.call("generate", 5)
        status, body = self._get(server.url + "/")
        assert status == 200
        for marker in ("Block rate", "Hash rate", "Next difficulty", "Blocks last hour"):
            assert marker in body
        assert "H/s" in body
        assert "Measured over the last" in body

    def test_overview_shows_how_big_the_chain_is(self, rpc):
        """The "chain weight" card replaced the unspent-output count: how much
        room the chain takes up is the question a newcomer actually has."""
        _, server, client = rpc
        client.call("generate", 5)
        status, body = self._get(server.url + "/")
        assert status == 200
        assert "Chain weight" in body
        assert "on disk" in body
        assert "per block" in body
        # The UTXO count did not vanish, it moved under the supply it explains.
        assert "unspent outputs" in body
        network_section = body.split("<h2>Network</h2>", 1)[1]
        assert "Unspent outputs" not in network_section

    def test_a_pruned_block_says_so_instead_of_looking_missing(self, rpc, key):
        node, server, client = rpc
        client.call("generate", 20, str(key.address(REGTEST.address_version)))
        node.chain.prune(2)

        status, body = self._get(server.url + "/block/1")
        assert status == 200
        assert "pruned" in body
        assert "only the header is still stored" in body

        status, listing = self._get(server.url + "/blocks")
        assert status == 200
        # Every height still has a row, pruned or not, so the list has no holes.
        for height in range(1, 21):
            assert f'/block/{height}"' in listing

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

    def test_hashrate_page_renders_chart_and_history(self, rpc):
        node, server, client = rpc
        client.call("generate", 25)
        status, body = self._get(server.url + "/hashrate")
        assert status == 200
        assert "Hash rate history" in body
        assert "H/s" in body
        assert "<svg" in body
        assert "<polyline" in body
        assert "Recent samples" in body
        assert "Difficulty" in body

        history = node.chain.hashrate_history(window=5)
        assert history
        heights = [point["height"] for point in history]
        assert heights == sorted(heights)
        assert all(point["hash_rate"] > 0 for point in history)
        assert all(point["difficulty"] > 0 for point in history)

    def test_hashrate_page_honours_window(self, rpc):
        _, server, client = rpc
        client.call("generate", 25)
        status, body = self._get(server.url + "/hashrate?window=5")
        assert status == 200
        assert "5 blocks" in body

    def test_hashrate_page_rejects_bad_window(self, rpc):
        _, server, _ = rpc
        status, body = self._get(server.url + "/hashrate?window=nonsense")
        assert status == 404
        assert "Not found" in body

    def test_explorer_escapes_hostile_content(self, rpc):
        _, server, _ = rpc
        status, body = self._get(server.url + "/search?q=%3Cscript%3Ealert(1)%3C/script%3E")
        assert status == 404
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body

    def test_favicon_is_served(self, rpc):
        _, server, _ = rpc
        status, body = self._get(server.url + "/icon.svg")
        assert status == 200
        assert "<svg" in body
        status, _ = self._get(server.url + "/favicon.ico")
        assert status == 200

    def test_pages_render_with_a_live_reload_script(self, rpc):
        node, server, _ = rpc
        status, body = self._get(server.url + "/")
        assert status == 200
        assert 'type="image/svg+xml"' in body  # favicon link
        if node.config.ws:
            assert "new WebSocket" in body


class TestWrongClock:
    """A node whose clock is behind must not conclude the network is broken.

    "Too far in the future" is the one rejection that is relative to the machine
    doing the checking. Treated as a consensus violation it was catastrophic: a
    fresh install with a slow clock refused every honest block, banned the seed
    that served them, and sat at height zero looking perfectly healthy.
    """

    SKEW = 3 * 3600  # regtest allows two hours

    def _slow_clock(self, monkeypatch, seconds: int) -> None:
        real = time.time
        monkeypatch.setattr(
            "scarletcoin.core.chain.time",
            SimpleNamespace(time=lambda: real() - seconds),
        )

    def test_a_block_from_the_future_is_not_a_consensus_violation(self, chain, key, monkeypatch):
        block = mine_block(chain, key)
        self._slow_clock(monkeypatch, self.SKEW)
        result = chain.add_block(block)
        assert result.status is BlockStatus.PREMATURE
        assert "clock" in result.reason
        assert not result.accepted

    def test_it_is_not_remembered_as_invalid(self, chain, key, monkeypatch):
        """Caching it would mean refusing the block for the life of the process,
        even after the clock was put right."""
        block = mine_block(chain, key)
        self._slow_clock(monkeypatch, self.SKEW)
        assert chain.add_block(block).status is BlockStatus.PREMATURE
        assert chain._invalid == {}
        monkeypatch.undo()
        assert chain.add_block(block).status is BlockStatus.CONNECTED
        assert chain.height == 1

    def test_the_peer_that_sent_it_is_not_punished(self, tmp_path, key, monkeypatch):
        good = Node(
            NodeConfig(
                network="regtest",
                datadir=tmp_path / "good",
                p2p_port=0,
                rpc=False,
                use_seeds=False,
            )
        )
        slow = Node(
            NodeConfig(
                network="regtest",
                datadir=tmp_path / "slow",
                p2p_port=0,
                rpc=False,
                use_seeds=False,
            )
        )
        good.start()
        slow.start()
        try:
            for _ in range(3):
                good.submit_block(mine_block(good.chain, key))
            self._slow_clock(monkeypatch, self.SKEW)

            assert slow.connect_peer("127.0.0.1", good.p2p_port)
            assert wait_until(lambda: bool(slow.peers) and slow.peers[0].handshake_done.is_set())
            peer = slow.peers[0]
            assert wait_until(lambda: slow._premature_blocks > 0, timeout=20)

            assert peer.misbehaviour == 0
            assert not peer.closed
            assert not slow.addrbook.is_banned("127.0.0.1")
            assert slow.chain._invalid == {}
            assert slow.chain.height == 0
            assert slow._premature  # kept, to try again

            # And it says so, rather than looking like an empty healthy chain.
            note = " ".join(slow.info()["warnings"])
            assert "clock" in note

            # Once the clock is right the node catches up by itself.
            monkeypatch.undo()
            slow._retry_premature()
            assert wait_until(lambda: slow.chain.height == good.chain.height, timeout=25)
            assert not slow._premature
            assert not any("clock" in note for note in slow.info()["warnings"])
        finally:
            slow.stop()
            good.stop()

    def test_a_deferred_block_is_not_requested_again_in_a_loop(self, node, key, monkeypatch):
        """Re-requesting a block our own clock forbids would spin: every arrival
        would be refused, then chased again."""
        block = mine_block(node.chain, key)
        self._slow_clock(monkeypatch, self.SKEW)
        assert node.submit_block(block).status is BlockStatus.PREMATURE

        sent: list[protocol.Message] = []
        peer = SimpleNamespace(
            send=sent.append,
            note_inventory=lambda _hash: None,
            requested_blocks=set(),
            pending_blocks=deque(),
        )
        node._on_inv(peer, protocol.Inv((InvItem(InvType.BLOCK, block.hash()),)))  # type: ignore[arg-type]
        assert not peer.pending_blocks
        assert not sent


class TestNodeWarnings:
    def test_a_network_without_seeds_says_so(self, tmp_path, monkeypatch):
        """The symptom of this is a node at height 0 with 0 peers, which is
        indistinguishable from a healthy new chain unless it is spelled out."""
        node = Node(
            NodeConfig(
                network="regtest",
                datadir=tmp_path / "alone",
                p2p_port=0,
                rpc=False,
                use_seeds=False,
                listen=True,
            )
        )
        try:
            monkeypatch.setattr(node, "started_at", time.time() - 60)
            note = " ".join(node.warnings())
            assert "no seed nodes built in" in note
            assert "--addnode" in note
            assert node.info()["warnings"]
        finally:
            node.stop()

    def test_a_node_that_has_just_started_does_not_complain(self, tmp_path):
        node = Node(
            NodeConfig(
                network="regtest",
                datadir=tmp_path / "young",
                p2p_port=0,
                rpc=False,
                use_seeds=False,
            )
        )
        try:
            assert node.warnings() == []
        finally:
            node.stop()

    def test_a_connected_node_has_nothing_to_report(self, tmp_path, monkeypatch):
        first = Node(
            NodeConfig(
                network="regtest", datadir=tmp_path / "a", p2p_port=0, rpc=False, use_seeds=False
            )
        )
        second = Node(
            NodeConfig(
                network="regtest", datadir=tmp_path / "b", p2p_port=0, rpc=False, use_seeds=False
            )
        )
        first.start()
        second.start()
        try:
            assert second.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(lambda: bool(second.peers))
            monkeypatch.setattr(second, "started_at", time.time() - 60)
            assert second.warnings() == []
        finally:
            second.stop()
            first.stop()


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

    def test_a_node_syncing_from_two_peers_does_not_stall(self, tmp_path, key):
        """Consecutive blocks must not be split across peers.

        Round-robin handed block N to one peer and N+1 to the other, so each block
        arrived before its parent and the orphan pool overflowed, stalling the sync
        forever.  The blocks must be downloaded in order, whichever peers serve them.
        """
        first = self._node(tmp_path, "a")
        relay = self._node(tmp_path, "b")
        try:
            for index in range(300):
                first.chain.add_block(
                    mine_block(
                        first.chain,
                        key,
                        timestamp=REGTEST.genesis_timestamp + 1 + index * 10,
                    )
                )
            assert first.chain.height == 300
            relay.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(lambda: relay.chain.height == 300, timeout=60)

            syncing = self._node(
                tmp_path,
                "c",
                connect=(f"127.0.0.1:{first.p2p_port}", f"127.0.0.1:{relay.p2p_port}"),
            )
            try:
                assert wait_until(lambda: syncing.chain.height == 300, timeout=60)
                assert syncing.chain.tip_hash == first.chain.tip_hash
            finally:
                syncing.stop()
        finally:
            relay.stop()
            first.stop()

    def test_info_reports_syncing_until_the_chain_is_caught_up(self, tmp_path, key):
        source = make_chain()
        headers = []
        for index in range(4):
            source.add_block(
                mine_block(source, key, timestamp=REGTEST.genesis_timestamp + 1 + index)
            )
            headers.append(source.tip.header.serialize())
        source.storage.close()

        node = self._node(tmp_path, "sync")
        try:
            for raw in headers:
                node.chain.add_header(BlockHeader.deserialize(raw))
            info = node.info()
            assert info["height"] == 0
            assert info["header_height"] == 4
            assert info["syncing"] is True
            assert info["syncing_to"] >= 4
            assert 0.0 <= info["sync_progress"] < 1.0
        finally:
            node.stop()

    def test_missing_blocks_are_handed_out_contiguously(self, tmp_path, key):
        """Blocks must be handed to peers in order, never round-robin.

        Splitting consecutive blocks across peers made each one arrive before its
        parent, flooding the orphan pool. Each peer must instead get a contiguous
        run, and a peer must not start its run before the previous run's last block
        is on disk.
        """
        source = make_chain()
        headers = []
        for index in range(20):
            source.add_block(
                mine_block(source, key, timestamp=REGTEST.genesis_timestamp + 1 + index * 10)
            )
            headers.append(source.tip.header.serialize())
        hashes = [BlockHeader.deserialize(raw).hash() for raw in headers]
        source.storage.close()

        node = Node(
            NodeConfig(
                network="regtest",
                datadir=tmp_path / "order",
                p2p_port=0,
                rpc=False,
                use_seeds=False,
            )
        )
        try:
            for raw in headers:
                node.chain.add_header(BlockHeader.deserialize(raw))

            sent: list[object] = []

            def make_peer() -> SimpleNamespace:
                return SimpleNamespace(
                    send=sent.append,
                    close=lambda: None,
                    handshake_done=SimpleNamespace(is_set=lambda: True),
                    requested_blocks=set(),
                    pending_blocks=deque(),
                )

            peers = [make_peer(), make_peer()]
            node._peers = dict(enumerate(peers))  # type: ignore[attr-defined]

            node._queue_missing_blocks()

            # The first peer gets the first half and requests it; the second gets
            # the second half but must wait for the first half's last block.
            assert peers[0].requested_blocks == set(hashes[:10])
            assert list(peers[1].pending_blocks) == hashes[10:]
            assert peers[1].requested_blocks == set()
        finally:
            node.stop()

    def test_a_pruned_node_still_relays_what_it_receives(self, tmp_path, key):
        """Pruning costs the ability to serve history, not to take part."""
        first = self._node(tmp_path, "a")
        second = self._node(tmp_path, "b")
        try:
            for _ in range(10):
                first.submit_block(mine_block(first.chain, key))
            second.connect_peer("127.0.0.1", first.p2p_port)
            assert wait_until(lambda: second.chain.height == 10, timeout=30)

            second.chain.prune(2)
            assert second.chain.prune_height == 8
            # New blocks still arrive, validate and connect on the pruned node.
            for _ in range(3):
                first.submit_block(mine_block(first.chain, key))
            assert wait_until(lambda: second.chain.height == 13, timeout=30)
            assert second.chain.tip_hash == first.chain.tip_hash
        finally:
            first.stop()
            second.stop()

    def test_a_pruned_node_does_not_offer_history_it_cannot_send(self, tmp_path, key):
        """Announcing blocks it has thrown away would only hand a syncing node
        orphans, so a pruned node stays quiet and lets it ask somebody else."""
        pruned = self._node(tmp_path, "pruned")
        try:
            for _ in range(10):
                pruned.submit_block(mine_block(pruned.chain, key))
            pruned.chain.prune(2)
            assert pruned.chain.prune_height == 8

            fresh = self._node(tmp_path, "fresh", connect=(f"127.0.0.1:{pruned.p2p_port}",))
            try:
                assert wait_until(lambda: bool(fresh.peers), timeout=15)
                assert fresh.peers[0].handshake_done.wait(10)
                # It connects and stays connected, but is never sent the old chain.
                assert not wait_until(lambda: fresh.chain.height > 0, timeout=6)
                assert fresh.chain.height == 0
                assert fresh.peers
            finally:
                fresh.stop()
        finally:
            pruned.stop()

    def test_a_late_joining_miner_is_told_about_pending_transactions(
        self, tmp_path, key, other_key
    ):
        """A miner is the node that is *ahead*, and it is the one that has to hear
        about an unconfirmed transaction. Offering the pool only when we are not
        behind meant the one peer that mattered was never told, and the
        transaction sat unconfirmed for ever while the miner made empty blocks."""
        sender = self._node(tmp_path, "sender")
        miner = self._node(tmp_path, "miner")
        try:
            sender.connect_peer("127.0.0.1", miner.p2p_port)
            assert wait_until(
                lambda: bool(sender.peers) and sender.peers[0].handshake_done.is_set()
            )
            for _ in range(5):
                sender.submit_block(mine_block(sender.chain, key))
            assert wait_until(lambda: miner.chain.height == 5)

            # The link drops and the miner pulls ahead, as a miner does.
            for peer in [*sender.peers, *miner.peers]:
                peer.close()
            assert wait_until(lambda: not sender.peers and not miner.peers)
            for _ in range(3):
                miner.submit_block(mine_block(miner.chain, other_key))
            assert sender.chain.height < miner.chain.height

            transaction = spend(
                sender.chain, key, other_key.address(REGTEST.address_version), 10**8
            )
            sender.submit_transaction(transaction)
            txid = transaction.txid()

            sender.connect_peer("127.0.0.1", miner.p2p_port)
            assert wait_until(
                lambda: bool(sender.peers) and sender.peers[0].handshake_done.is_set()
            )
            assert wait_until(lambda: txid in miner.mempool, timeout=20)

            miner.submit_block(mine_block(miner.chain, other_key, miner.mempool))
            assert miner.chain.get_transaction(txid) is not None
            assert wait_until(lambda: txid not in sender.mempool, timeout=20)
        finally:
            sender.stop()
            miner.stop()

    def test_the_pool_is_re_offered_so_a_lost_transaction_comes_back(
        self, tmp_path, key, other_key
    ):
        """Relay happens once. A peer that was mid-sync and dropped the
        transaction, or that restarted and lost its pool, has to be told again."""
        sender = self._node(tmp_path, "sender")
        miner = self._node(tmp_path, "miner")
        try:
            sender.connect_peer("127.0.0.1", miner.p2p_port)
            assert wait_until(
                lambda: bool(sender.peers) and sender.peers[0].handshake_done.is_set()
            )
            for _ in range(5):
                sender.submit_block(mine_block(sender.chain, key))
            assert wait_until(lambda: miner.chain.height == 5)

            transaction = spend(
                sender.chain, key, other_key.address(REGTEST.address_version), 10**8
            )
            sender.submit_transaction(transaction)
            txid = transaction.txid()
            assert wait_until(lambda: txid in miner.mempool)

            # The miner forgets it: a restart, or an earlier rejection while it
            # was still catching up. Nothing else would ever mention it again.
            miner.mempool.clear()
            assert txid not in miner.mempool

            sender._reannounce_mempool()
            assert wait_until(lambda: txid in miner.mempool, timeout=20)
        finally:
            sender.stop()
            miner.stop()

    def test_announcing_a_huge_pool_does_not_ban_the_peer(self, tmp_path, monkeypatch):
        """``Inv`` refuses more than MAX_INV_ITEMS, and the peer loop reads that
        refusal as misbehaviour — so an oversized announcement would have banned
        a peer for our own mistake."""
        node = self._node(tmp_path, "big")
        try:
            fake = [bytes([index % 256]) * 32 for index in range(protocol.MAX_INV_ITEMS + 50)]
            monkeypatch.setattr(node.mempool, "txids", lambda: fake)
            sent: list[protocol.Message] = []
            peer = SimpleNamespace(send=sent.append)
            node._announce_mempool(peer)  # type: ignore[arg-type]
            assert len(sent) == 1
            assert len(sent[0].items) == protocol.MAX_INV_ITEMS
            sent[0].encode()  # would raise if the cap were not applied
        finally:
            node.stop()

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
            assert not second.connect_peer("localhost", first.p2p_port)
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
