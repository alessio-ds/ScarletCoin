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
