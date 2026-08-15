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
            window.send_address.setText(str(other_key.address(REGTEST.address_version)))
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


class TestMinerWindow:
    def test_it_mines_and_stops(self, qt_app, rpc, key):
        _, _, client = rpc
        address = str(key.address(REGTEST.address_version))
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
