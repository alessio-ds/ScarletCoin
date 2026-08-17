"""Smoke tests for the optional Qt applications.

These run with Qt's ``offscreen`` platform plugin, so they work on a headless
machine.  They are skipped when PyQt5 is not installed.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt5", reason="the graphical interface needs PyQt5")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

from scarletcoin.core.params import REGTEST
from scarletcoin.gui.common import apply_theme
from scarletcoin.gui.miner_app import MinerWindow, format_rate
from scarletcoin.gui.wallet_app import WalletWindow
from scarletcoin.units import format_amount
from tests.conftest import wait_until


@pytest.fixture(scope="session")
def qt_app():
    """A single Qt application shared by every GUI test."""
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    apply_theme(application)
    return application


def pump(application, seconds: float = 0.3) -> None:
    """Process Qt events for a while so worker threads can deliver their results."""
    deadline = QtCore.QTime.currentTime().addMSecs(int(seconds * 1000))
    while QtCore.QTime.currentTime() < deadline:
        application.processEvents(QtCore.QEventLoop.AllEvents, 50)


class TestWalletWindow:
    def test_it_shows_balances_and_history(self, qt_app, rpc, wallet):
        _, _, client = rpc
        address = wallet.keystore.default_address()
        client.call("generate", 4, address)

        window = WalletWindow(wallet.keystore, client)
        try:
            window.show()
            assert wait_until(lambda: (pump(qt_app), bool(window._snapshot))[1], timeout=15)
            pump(qt_app)
            assert format_amount(REGTEST.subsidy(0) * 3) in window.balance_label.text()
            assert window.address_table.rowCount() == 1
            assert window.address_table.item(0, 0).text() == address
            assert window.history_table.rowCount() == 4
            assert window.coins_table.rowCount() == 4
            assert "regtest" in window.status.currentMessage()
            assert "unencrypted" in window.lock_label.text()
        finally:
            window.close()
            pump(qt_app)

    def test_sending_from_the_window(self, qt_app, rpc, wallet, other_key):
        _, _, client = rpc
        client.call("generate", 4, wallet.keystore.default_address())
        window = WalletWindow(wallet.keystore, client)
        try:
            window.show()
            window.send_address.setText(str(other_key.address(REGTEST.stealth_version)))
            window.send_amount.setText("5")
            # Confirm the payment automatically instead of showing a dialog.
            sent: list[str] = []
            window._confirm_send = lambda result: sent.append(  # type: ignore[method-assign]
                client.sendrawtransaction(result.transaction.serialize().hex())
            )
            window._send()
            assert wait_until(lambda: (pump(qt_app), bool(sent))[1], timeout=15)
            assert client.call("getmempool")["count"] == 1
            assert client.call("getmempool")["transactions"][0]["txid"] == sent[0]
        finally:
            window.close()
            pump(qt_app)

    def test_a_locked_wallet_cannot_send(self, qt_app, rpc, tmp_path):
        from scarletcoin.wallet.keystore import Keystore

        _, _, client = rpc
        path = tmp_path / "locked.json"
        Keystore.create(path, "regtest", password="hunter2")
        window = WalletWindow(Keystore.load(path), client)
        try:
            assert not window.send_button.isEnabled()
            assert "locked" in window.lock_label.text()
            assert "locked" in window.send_status.text()
        finally:
            window.close()
            pump(qt_app)


class TestNodeConnection:
    def test_settings_round_trip(self, tmp_path):
        from scarletcoin.gui.common import ConnectionSettings

        assert ConnectionSettings.load(tmp_path, "regtest") is None
        ConnectionSettings("http://node.example:20332", "tok").save(tmp_path, "regtest")
        loaded = ConnectionSettings.load(tmp_path, "regtest")
        assert loaded == ConnectionSettings("http://node.example:20332", "tok")

    def test_a_corrupt_settings_file_is_ignored(self, tmp_path):
        from scarletcoin.gui.common import ConnectionSettings

        path = ConnectionSettings.path(tmp_path, "regtest")
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        assert ConnectionSettings.load(tmp_path, "regtest") is None

    def test_command_line_wins_over_saved_settings(self, tmp_path):
        import argparse

        from scarletcoin.gui.common import ConnectionSettings, settings_from_args

        ConnectionSettings("http://saved:1", "saved-token").save(tmp_path, "regtest")
        args = argparse.Namespace(network="regtest", datadir=tmp_path, rpc_url=None, rpc_token=None)
        assert settings_from_args(args).url == "http://saved:1"
        args.rpc_url = "http://given:2"
        assert settings_from_args(args).url == "http://given:2"

    def test_the_local_node_token_is_picked_up(self, tmp_path):
        import argparse

        from scarletcoin.cli_common import write_rpc_token
        from scarletcoin.gui.common import default_url, settings_from_args

        write_rpc_token(tmp_path, "regtest", "from-file")
        args = argparse.Namespace(network="regtest", datadir=tmp_path, rpc_url=None, rpc_token=None)
        settings = settings_from_args(args)
        assert settings.url == default_url("regtest")
        assert settings.token == "from-file"

    def test_a_stale_saved_token_does_not_shadow_the_local_nodes(self, tmp_path):
        """The connection dialog saves settings; an old saved token must not win
        over the token of the node that is actually running here."""
        import argparse

        from scarletcoin.cli_common import write_rpc_token
        from scarletcoin.gui.common import ConnectionSettings, default_url, settings_from_args

        ConnectionSettings(default_url("regtest"), "stale-token").save(tmp_path, "regtest")
        write_rpc_token(tmp_path, "regtest", "fresh-token")
        args = argparse.Namespace(network="regtest", datadir=tmp_path, rpc_url=None, rpc_token=None)
        settings = settings_from_args(args)
        assert settings.url == default_url("regtest")
        assert settings.token == "fresh-token"

    def test_saved_settings_still_apply_to_remote_nodes(self, tmp_path):
        import argparse

        from scarletcoin.gui.common import ConnectionSettings, settings_from_args

        ConnectionSettings("http://other:20332", "remote-token").save(tmp_path, "regtest")
        args = argparse.Namespace(network="regtest", datadir=tmp_path, rpc_url=None, rpc_token=None)
        settings = settings_from_args(args)
        assert settings.url == "http://other:20332"
        assert settings.token == "remote-token"

    def test_the_dialog_rejects_an_unreachable_node(self, qt_app, tmp_path):
        from scarletcoin.gui.common import ConnectionSettings, NodeDialog

        dialog = NodeDialog(None, ConnectionSettings("http://127.0.0.1:1"), "regtest")
        dialog._accept()
        assert dialog.result() != QtWidgets.QDialog.Accepted
        assert "No answer" in dialog.status.text()

    def test_the_dialog_rejects_the_wrong_network(self, qt_app, rpc):
        from scarletcoin.gui.common import ConnectionSettings, NodeDialog

        _, server, _ = rpc
        dialog = NodeDialog(None, ConnectionSettings(server.url, "test-token"), "mainnet")
        dialog._accept()
        assert dialog.result() != QtWidgets.QDialog.Accepted
        assert "regtest network" in dialog.status.text()

    def test_the_dialog_accepts_a_working_node(self, qt_app, rpc):
        from scarletcoin.gui.common import ConnectionSettings, NodeDialog

        _, server, _ = rpc
        dialog = NodeDialog(None, ConnectionSettings(server.url, "test-token"), "regtest")
        dialog._accept()
        assert dialog.result() == QtWidgets.QDialog.Accepted
        assert dialog.status.text().startswith("Connected")

    def test_the_wallet_window_can_switch_nodes(self, qt_app, rpc, wallet, monkeypatch, tmp_path):
        from scarletcoin.gui.common import ConnectionSettings

        _, server, client = rpc
        client.call("generate", 3, wallet.keystore.default_address())
        window = WalletWindow(wallet.keystore, client, datadir=tmp_path)
        try:
            monkeypatch.setattr(
                "scarletcoin.gui.wallet_app.ask_for_node",
                lambda *a, **k: ConnectionSettings(server.url, "test-token"),
            )
            window.change_node()
            assert window.client.url == server.url
            assert wait_until(lambda: (pump(qt_app), bool(window._snapshot))[1], timeout=15)
        finally:
            window.close()
            pump(qt_app)

    def test_an_unreachable_node_shows_a_hint_not_a_crash(self, qt_app, rpc, wallet, tmp_path):
        from scarletcoin.net.client import RpcClient

        window = WalletWindow(
            wallet.keystore, RpcClient("http://127.0.0.1:1", timeout=1), datadir=tmp_path
        )
        try:
            assert wait_until(
                lambda: (pump(qt_app), "no answer from" in window.status.currentMessage())[1],
                timeout=20,
            )
            assert "Connection" in window.status.currentMessage()
        finally:
            window.close()
            pump(qt_app)


class TestChoosingANode:
    """The question a newcomer is asked, and the three answers it accepts."""

    def test_the_startup_dialog_offers_a_local_and_a_public_node(self, qt_app, tmp_path):
        from scarletcoin.gui.common import StartupDialog

        dialog = StartupDialog(None, "regtest", tmp_path, reason="No node answered.")
        try:
            assert dialog.answer is None
            dialog._pick(StartupDialog.PUBLIC)
            assert dialog.answer == StartupDialog.PUBLIC
            assert dialog.result() == QtWidgets.QDialog.Accepted
        finally:
            dialog.close()

    def test_the_startup_dialog_reports_the_size_of_the_chain_already_here(
        self, qt_app, tmp_path, key
    ):
        from scarletcoin.core.chain import Blockchain
        from scarletcoin.core.storage import Storage
        from scarletcoin.gui.common import StartupDialog
        from scarletcoin.net.node import NodeConfig
        from tests.helpers import mine_and_add

        storage = Storage(NodeConfig(network="regtest", datadir=tmp_path).chain_path)
        mine_and_add(Blockchain(storage, REGTEST), key, count=3)
        storage.close()

        dialog = StartupDialog(None, "regtest", tmp_path)
        try:
            text = " ".join(
                widget.text() for widget in dialog.findChildren(QtWidgets.QLabel) if widget.text()
            )
            assert "height 3" in text
            assert "on disk" in text
        finally:
            dialog.close()

    def test_the_local_node_dialog_turns_answers_into_node_options(self, qt_app, tmp_path):
        from scarletcoin.core.chain import MIN_PRUNE_KEEP
        from scarletcoin.gui.common import LocalNodeDialog

        dialog = LocalNodeDialog(None, "regtest", tmp_path)
        try:
            assert dialog.extra_arguments() == ()
            assert dialog.keep_spin.minimum() == MIN_PRUNE_KEEP
            assert not dialog.keep_spin.isEnabled()
            assert not dialog.public_mining_box.isEnabled()

            dialog.prune_box.setChecked(True)
            dialog.keep_spin.setValue(5000)
            dialog.public_box.setChecked(True)
            dialog.public_mining_box.setChecked(True)
            dialog.advertise_edit.setText("https://mine.example")
            assert dialog.extra_arguments() == (
                "--prune",
                "5000",
                "--rpc-public",
                "--rpc-public-mining",
                "--rpc-advertise",
                "https://mine.example",
            )
        finally:
            dialog.close()

    def test_the_local_node_dialog_shows_the_size_before_anything_starts(
        self, qt_app, tmp_path, key
    ):
        from scarletcoin.core.chain import Blockchain
        from scarletcoin.core.storage import Storage
        from scarletcoin.gui.common import LocalNodeDialog
        from scarletcoin.net.node import NodeConfig
        from tests.helpers import mine_and_add

        empty = LocalNodeDialog(None, "regtest", tmp_path)
        assert "No regtest chain here yet" in empty.size_label.text()
        assert not empty.prune_now_button.isEnabled()
        empty.close()

        storage = Storage(NodeConfig(network="regtest", datadir=tmp_path).chain_path)
        mine_and_add(Blockchain(storage, REGTEST), key, count=4)
        storage.close()

        dialog = LocalNodeDialog(None, "regtest", tmp_path)
        try:
            assert "height 4" in dialog.size_label.text()
            assert "on disk" in dialog.size_label.text()
            # Pruning an existing chain is offered only once it is asked for.
            assert not dialog.prune_now_button.isEnabled()
            dialog.prune_box.setChecked(True)
            assert dialog.prune_now_button.isEnabled()
        finally:
            dialog.close()

    def test_pruning_from_the_dialog_shrinks_the_chain(self, qt_app, tmp_path, key, monkeypatch):
        from scarletcoin.core.chain import Blockchain
        from scarletcoin.core.storage import Storage
        from scarletcoin.gui.common import LocalNodeDialog
        from scarletcoin.net.node import NodeConfig
        from tests.helpers import mine_and_add

        storage = Storage(NodeConfig(network="regtest", datadir=tmp_path).chain_path)
        mine_and_add(Blockchain(storage, REGTEST), key, count=20)
        storage.close()

        dialog = LocalNodeDialog(None, "regtest", tmp_path)
        try:
            dialog.prune_box.setChecked(True)
            dialog.keep_spin.setMinimum(2)
            dialog.keep_spin.setValue(2)
            monkeypatch.setattr(
                QtWidgets.QMessageBox, "question", lambda *a, **k: QtWidgets.QMessageBox.Yes
            )
            dialog._prune_now()
            assert "pruned 18 block(s)" in dialog.status.text()
            assert "freeing" in dialog.status.text()
            assert "pruned" in dialog.size_label.text().lower()
        finally:
            dialog.close()

    def test_the_public_node_picker_lists_what_actually_answers(
        self, qt_app, tmp_path, monkeypatch, rpc
    ):
        from scarletcoin.gui.common import PublicNodeDialog

        node, server, _ = rpc
        node.config.rpc_public = True
        server.public = True
        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", f"{server.url},http://127.0.0.1:1")

        dialog = PublicNodeDialog(None, "regtest", tmp_path)
        try:
            assert wait_until(
                lambda: (pump(qt_app), not dialog.status.text().startswith("looking"))[1],
                timeout=25,
            )
            assert dialog.table.rowCount() == 2
            assert "1 node(s) answered" in dialog.status.text()
            dialog._accept()
            assert dialog.settings is not None
            assert dialog.settings.url == server.url
            assert dialog.settings.token == ""
        finally:
            dialog.close()
            pump(qt_app)

    def test_the_picker_refuses_a_node_that_did_not_answer(self, qt_app, tmp_path, monkeypatch):
        from scarletcoin.gui.common import PublicNodeDialog

        monkeypatch.setenv("SCARLETCOIN_PUBLIC_NODES", "http://127.0.0.1:1")
        dialog = PublicNodeDialog(None, "regtest", tmp_path)
        try:
            assert wait_until(
                lambda: (pump(qt_app), not dialog.status.text().startswith("looking"))[1],
                timeout=25,
            )
            assert "no public regtest node answered" in dialog.status.text()
            assert not dialog.use_button.isEnabled()
            dialog.table.selectRow(0)
            dialog._accept()
            assert dialog.settings is None
            assert "cannot be used" in dialog.status.text()
        finally:
            dialog.close()
            pump(qt_app)

    def test_a_node_that_answers_is_used_without_asking(self, qt_app, tmp_path, rpc):
        """The question is only worth asking when there is no obvious answer."""
        import argparse

        from scarletcoin.gui.common import ConnectionSettings, resolve_startup

        _, server, _ = rpc
        ConnectionSettings(server.url, "test-token").save(tmp_path, "regtest")
        args = argparse.Namespace(
            network="regtest",
            datadir=tmp_path,
            rpc_url=None,
            rpc_token=None,
            node=None,
            no_start_node=True,
        )
        settings, local_node = resolve_startup(args)
        assert settings is not None and settings.url == server.url
        assert local_node is None

    def test_a_node_on_the_wrong_network_is_not_silently_accepted(
        self, qt_app, tmp_path, rpc, monkeypatch
    ):
        import argparse

        from scarletcoin.gui import common

        _, server, _ = rpc
        settings = common.ConnectionSettings(server.url, "test-token")
        assert "regtest network" in common._why_not(settings, "mainnet")

        asked: list[str] = []
        monkeypatch.setattr(
            common,
            "choose_startup_node",
            lambda *a, **kwargs: (asked.append(kwargs.get("reason", "")), (None, None))[1],
        )
        args = argparse.Namespace(
            network="mainnet",
            datadir=tmp_path,
            rpc_url=server.url,
            rpc_token="test-token",
            node=None,
            no_start_node=True,
        )
        # The user is asked rather than shown mainnet balances from a regtest node.
        assert common.resolve_startup(args) == (None, None)
        assert asked and "regtest" in asked[0]


class TestMinerWindow:
    def test_it_mines_and_stops(self, qt_app, rpc, key):
        _, _, client = rpc
        address = str(key.address(REGTEST.stealth_version))
        window = MinerWindow(client, "regtest", address)
        try:
            window.show()
            assert address in window.address_edit.text()
            assert "regtest" in window.status.currentMessage()

            window.workers_box.setValue(1)
            window._toggle()  # start
            assert wait_until(lambda: (pump(qt_app), client.getblockcount() >= 1)[1], timeout=30)
            window._toggle()  # stop
            assert wait_until(lambda: (pump(qt_app), window._bridge is None)[1], timeout=30)
            assert "mined block" in window.log.toPlainText()
        finally:
            window.close()
            pump(qt_app)

    def test_it_refuses_an_empty_address(self, qt_app, rpc, monkeypatch):
        _, _, client = rpc
        errors: list[str] = []
        monkeypatch.setattr(
            "scarletcoin.gui.miner_app.show_error",
            lambda parent, title, message: errors.append(message),
        )
        window = MinerWindow(client, "regtest", "")
        try:
            window._toggle()
            assert errors and "address" in errors[0]
            assert window._bridge is None
        finally:
            window.close()
            pump(qt_app)

    @pytest.mark.parametrize(
        ("rate", "expected"),
        [(12.0, "12.00 H/s"), (1500.0, "1.50 kH/s"), (2_500_000.0, "2.50 MH/s")],
    )
    def test_hash_rate_formatting(self, rate, expected):
        assert format_rate(rate) == expected
