"""``scarlet-wallet-gui``: a Qt desktop wallet.

The window is a thin layer over :class:`scarletcoin.wallet.wallet.Wallet`: every
balance and history query goes to a node over RPC in a worker thread, and every
signature is produced locally.  Nothing blocks the interface and no Qt object is
ever touched from a background thread.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from scarletcoin.gui import require_qt

QtCore, QtGui, QtWidgets = require_qt()

from scarletcoin import __version__  # noqa: E402
from scarletcoin.gui.common import (  # noqa: E402
    ConnectionSettings,
    LocalNodeDialog,
    PollWorker,
    PublicNodeDialog,
    add_common_gui_arguments,
    apply_theme,
    ask_for_node,
    monospace,
    resolve_startup,
    run_in_thread,
    show_error,
    start_node_with_progress,
)
from scarletcoin.net.client import RpcClient  # noqa: E402
from scarletcoin.net.launcher import LocalNode  # noqa: E402
from scarletcoin.units import format_amount, format_bytes, parse_amount  # noqa: E402
from scarletcoin.wallet.cli import default_wallet_path  # noqa: E402
from scarletcoin.wallet.keystore import Keystore, WalletError  # noqa: E402
from scarletcoin.wallet.wallet import Wallet  # noqa: E402

__all__ = ["WalletWindow", "main"]


def _table(headers: list[str]) -> QtWidgets.QTableWidget:
    table = QtWidgets.QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.horizontalHeader().setStretchLastSection(True)
    table.setWordWrap(False)
    return table


def _fill(table: QtWidgets.QTableWidget, rows: list[list[str]], *, mono_columns: set[int]) -> None:
    table.setRowCount(len(rows))
    font = monospace()
    for row, values in enumerate(rows):
        for column, text in enumerate(values):
            item = QtWidgets.QTableWidgetItem(text)
            if column in mono_columns:
                item.setFont(font)
            table.setItem(row, column, item)
    table.resizeColumnsToContents()


class WalletWindow(QtWidgets.QMainWindow):
    """The main wallet window."""

    def __init__(
        self,
        keystore: Keystore,
        client: RpcClient,
        *,
        datadir: Path | None = None,
        settings: ConnectionSettings | None = None,
        local_node: LocalNode | None = None,
    ) -> None:
        super().__init__()
        self.keystore = keystore
        self.client = client
        self.datadir = datadir
        self.local_node = local_node
        """A node this window started, and is therefore responsible for."""
        self.settings = settings or ConnectionSettings(client.url, client.token or "")
        self.wallet = Wallet(keystore, client)
        self._threads: list[QtCore.QThread] = []
        self._snapshot: dict = {}

        self.setWindowTitle(f"ScarletCoin wallet - {keystore.params.name}")
        self.resize(880, 620)
        self._build_menu()
        self._build_ui()
        self._start_polling()

    # ------------------------------------------------------------------ interface

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("&Open wallet...", self._open_wallet)
        file_menu.addAction("&New wallet...", self._create_wallet)
        file_menu.addSeparator()
        file_menu.addAction("&Quit", self.close, QtGui.QKeySequence("Ctrl+Q"))

        wallet_menu = self.menuBar().addMenu("&Wallet")
        wallet_menu.addAction("New &address", self._new_address)
        wallet_menu.addAction("&Import private key...", self._import_key)
        wallet_menu.addAction("&Export private key...", self._export_key)
        wallet_menu.addSeparator()
        wallet_menu.addAction("Set or change &password...", self._change_password)

        node_menu = self.menuBar().addMenu("&Node")
        node_menu.addAction("&Connection...", self.change_node)
        node_menu.addAction("Choose a &public node...", self.choose_public_node)
        node_menu.addAction("&Start a local node...", self.start_local_node)
        node_menu.addAction("Open the node's &log", self.open_node_log)

        view_menu = self.menuBar().addMenu("&View")
        view_menu.addAction("Open block &explorer", self._open_explorer)
        view_menu.addAction("&Refresh now", self._refresh_now, QtGui.QKeySequence("F5"))

        self.menuBar().addMenu("&Help").addAction("&About", self._about)

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(18, 14, 18, 10)
        layout.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("ScarletCoin")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        self.lock_label = QtWidgets.QLabel()
        self.lock_label.setObjectName("muted")
        header.addWidget(self.lock_label)
        layout.addLayout(header)

        summary = QtWidgets.QHBoxLayout()
        self.balance_label = QtWidgets.QLabel("0 SCT")
        self.balance_label.setObjectName("balance")
        summary.addWidget(self.balance_label)
        summary.addSpacing(24)
        self.detail_label = QtWidgets.QLabel()
        self.detail_label.setObjectName("muted")
        summary.addWidget(self.detail_label)
        summary.addStretch(1)
        layout.addLayout(summary)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._send_tab(), "Send")
        self.tabs.addTab(self._receive_tab(), "Receive")
        self.tabs.addTab(self._history_tab(), "History")
        self.tabs.addTab(self._coins_tab(), "Coins")
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self.status = self.statusBar()
        self.status.showMessage("connecting to the node...")
        self._update_lock_label()

    def _send_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(page)
        form.setContentsMargins(16, 18, 16, 16)
        form.setSpacing(10)

        self.send_address = QtWidgets.QLineEdit()
        self.send_address.setPlaceholderText("destination address")
        self.send_address.setFont(monospace())
        form.addRow("Pay to", self.send_address)

        amount_row = QtWidgets.QHBoxLayout()
        self.send_amount = QtWidgets.QLineEdit()
        self.send_amount.setPlaceholderText("0.00000000")
        amount_row.addWidget(self.send_amount, 1)
        amount_row.addWidget(QtWidgets.QLabel("SCT"))
        self.send_everything = QtWidgets.QCheckBox("send everything")
        self.send_everything.toggled.connect(self.send_amount.setDisabled)
        amount_row.addWidget(self.send_everything)
        form.addRow("Amount", amount_row)

        self.fee_rate = QtWidgets.QSpinBox()
        self.fee_rate.setRange(0, 10_000_000)
        self.fee_rate.setSingleStep(500)
        self.fee_rate.setValue(self.keystore.params.min_relay_fee_per_kb)
        self.fee_rate.setSuffix(" scar per kB")
        form.addRow("Fee rate", self.fee_rate)

        self.send_button = QtWidgets.QPushButton("Send")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._send)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.send_button)
        buttons.addStretch(1)
        form.addRow("", buttons)

        self.send_status = QtWidgets.QLabel()
        self.send_status.setWordWrap(True)
        self.send_status.setObjectName("hint")
        form.addRow("", self.send_status)
        if self.keystore.locked:
            self.send_button.setEnabled(False)
            self.send_status.setText("This wallet is locked. Unlock it to spend.")
        return page

    def _receive_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)

        hint = QtWidgets.QLabel(
            "Give any of these addresses to whoever is paying you. "
            "A new address per payment keeps your history private."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.address_table = _table(["Address", "Label", "Balance"])
        self.address_table.doubleClicked.connect(self._copy_selected_address)
        layout.addWidget(self.address_table, 1)

        buttons = QtWidgets.QHBoxLayout()
        new_button = QtWidgets.QPushButton("New address")
        new_button.clicked.connect(self._new_address)
        copy_button = QtWidgets.QPushButton("Copy address")
        copy_button.clicked.connect(self._copy_selected_address)
        explorer_button = QtWidgets.QPushButton("View in explorer")
        explorer_button.clicked.connect(self._open_selected_address)
        for button in (new_button, copy_button, explorer_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    def _history_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        self.history_table = _table(["Height", "Amount", "Confirmations", "Transaction"])
        self.history_table.doubleClicked.connect(self._open_selected_transaction)
        layout.addWidget(self.history_table)
        hint = QtWidgets.QLabel("Double-click a row to open it in the block explorer.")
        hint.setObjectName("hint")
        layout.addWidget(hint)
        return page

    def _coins_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        self.coins_table = _table(["Amount", "Confirmations", "Type", "Output"])
        layout.addWidget(self.coins_table)
        return page

    # -------------------------------------------------------------------- polling

    def _start_polling(self) -> None:
        self._poll_thread = QtCore.QThread(self)
        self._poller = PollWorker(self._collect, interval_ms=5000)
        self._poller.moveToThread(self._poll_thread)
        self._poll_thread.started.connect(self._poller.start)
        self._poller.ready.connect(self._apply_snapshot)
        self._poller.failed.connect(self._on_poll_error)
        self._poll_thread.start()

    def _collect(self) -> dict:
        """Gather everything the window shows.  Runs in the worker thread."""
        wallet = self.wallet  # re-read every poll, so reconnecting takes effect
        return {
            "info": wallet.client.getinfo(),
            "balance": wallet.balance(),
            "addresses": wallet.balances_by_address(),
            "history": wallet.history(50),
            "coins": wallet.coins(spendable_only=False),
        }

    @QtCore.pyqtSlot(object)
    def _apply_snapshot(self, snapshot: dict) -> None:
        self._snapshot = snapshot
        balance = snapshot["balance"]
        info = snapshot["info"]
        self.balance_label.setText(f"{format_amount(balance.spendable)} SCT")
        details = [f"{balance.utxo_count} unspent outputs"]
        if balance.immature:
            details.append(f"{format_amount(balance.immature)} SCT still maturing")
        self.detail_label.setText("  ·  ".join(details))
        chain = ""
        if info.get("chain_size"):
            chain = f"  ·  chain {info['chain_size']}"
        elif info.get("chain_bytes") is not None:
            chain = f"  ·  chain {format_bytes(info['chain_bytes'])}"
        # A node that cannot reach the network looks just like a healthy empty one.
        # Say so here rather than leaving it in the log.
        notes = info.get("warnings") or []
        if notes:
            self.status.showMessage(f"{info['network']}  ·  height {info['height']}  ·  {notes[0]}")
        else:
            self.status.showMessage(
                f"{info['network']}  ·  height {info['height']}  ·  {info['peers']} peers"
                f"  ·  {info['mempool_size']} unconfirmed  ·  difficulty {info['difficulty']:.6g}"
                f"{chain}"
            )
        _fill(
            self.address_table,
            [
                [address, label, f"{format_amount(value)} SCT"]
                for address, label, value in snapshot["addresses"]
            ],
            mono_columns={0},
        )
        _fill(
            self.history_table,
            [
                [
                    str(item["height"]),
                    f"{'+' if item['net'] >= 0 else '-'}{format_amount(abs(item['net']))} SCT",
                    str(item["confirmations"]),
                    item["txid"],
                ]
                for item in snapshot["history"]
            ],
            mono_columns={3},
        )
        _fill(
            self.coins_table,
            [
                [
                    f"{format_amount(coin.value)} SCT",
                    str(max(0, info["height"] - coin.height + 1)),
                    "coinbase" if coin.is_coinbase else "payment",
                    _otk[:16].hex(),
                ]
                for _otk, coin, _spend in snapshot["coins"]
            ],
            mono_columns={3},
        )

    @QtCore.pyqtSlot(str)
    def _on_poll_error(self, message: str) -> None:
        self.status.showMessage(
            f"no answer from {self.client.url} - {message}"
            "   (Node > Connection... to use a different one)"
        )

    def start_local_node(self) -> None:
        """Start a node on this machine and use it."""
        if self.local_node is not None and self.local_node.running:
            QtWidgets.QMessageBox.information(
                self, "Node", f"A node started here is already running at {self.client.url}."
            )
            return
        network = self.keystore.params.name
        options = LocalNodeDialog(self, network, self._datadir())
        if options.exec_() != QtWidgets.QDialog.Accepted:
            return
        node = start_node_with_progress(
            self,
            network=network,
            datadir=self._datadir(),
            extra=options.extra_arguments(),
        )
        if node is None:
            return
        self.local_node = node
        self._use(ConnectionSettings(node.url, node.token))
        self.status.showMessage(f"started a node at {node.url}", 5000)

    def choose_public_node(self) -> None:
        """Pick from the public nodes that are up right now."""
        network = self.keystore.params.name
        picker = PublicNodeDialog(self, network, self._datadir())
        if picker.exec_() != QtWidgets.QDialog.Accepted or picker.settings is None:
            return
        picker.settings.save(self._datadir(), network)
        self._use(picker.settings)
        self.status.showMessage(f"now using {self.client.url}", 5000)

    def _use(self, settings: ConnectionSettings) -> None:
        """Point the window at a different node."""
        self.settings = settings
        self.client = settings.client()
        self.wallet = Wallet(self.keystore, self.client)
        self._refresh_now()

    def open_node_log(self) -> None:
        """Show the log of the node this window started."""
        if self.local_node is None:
            QtWidgets.QMessageBox.information(self, "Node", "This window did not start a node.")
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(str(self.local_node.log_path))
        dialog.resize(760, 420)
        layout = QtWidgets.QVBoxLayout(dialog)
        view = QtWidgets.QPlainTextEdit(self.local_node.tail_log(400))
        view.setReadOnly(True)
        view.setFont(monospace())
        layout.addWidget(view)
        dialog.exec_()

    def _datadir(self) -> Path:
        return self.datadir or self.keystore.path.parent.parent

    def change_node(self) -> None:
        """Ask for a different node and reconnect to it."""
        chosen = ask_for_node(self, self.settings, self.keystore.params.name, self._datadir())
        if chosen is None:
            return
        self._use(chosen)
        self.status.showMessage(f"now using {self.client.url}", 5000)

    def _refresh_now(self) -> None:
        QtCore.QMetaObject.invokeMethod(self._poller, "poll", QtCore.Qt.QueuedConnection)

    # -------------------------------------------------------------------- actions

    def _selected(self, table: QtWidgets.QTableWidget, column: int) -> str | None:
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, column)
        return None if item is None else item.text()

    def _copy_selected_address(self) -> None:
        address = self._selected(self.address_table, 0) or self.keystore.default_address()
        QtWidgets.QApplication.clipboard().setText(address)
        self.status.showMessage(f"copied {address}", 4000)

    def _open_explorer(self, path: str = "/") -> None:
        webbrowser.open(f"{self.client.url}{path}")

    def _open_selected_address(self) -> None:
        address = self._selected(self.address_table, 0)
        if address:
            self._open_explorer(f"/address/{address}")

    def _open_selected_transaction(self) -> None:
        txid = self._selected(self.history_table, 3)
        if txid:
            self._open_explorer(f"/tx/{txid}")

    def _unlock_if_needed(self) -> bool:
        """Ask for the password when the wallet is locked.  Returns ``True`` if usable."""
        if not self.keystore.locked:
            return True
        password, accepted = QtWidgets.QInputDialog.getText(
            self,
            "Unlock wallet",
            "Wallet password:",
            QtWidgets.QLineEdit.Password,
        )
        if not accepted:
            return False
        try:
            self.keystore.unlock(password)
        except WalletError as exc:
            show_error(self, "Wrong password", str(exc))
            return False
        self.send_button.setEnabled(True)
        self.send_status.clear()
        self._update_lock_label()
        return True

    def _update_lock_label(self) -> None:
        if not self.keystore.encrypted:
            self.lock_label.setText("unencrypted wallet")
        elif self.keystore.locked:
            self.lock_label.setText("locked")
        else:
            self.lock_label.setText("unlocked")

    def _new_address(self) -> None:
        if not self._unlock_if_needed():
            return
        label, _ = QtWidgets.QInputDialog.getText(self, "New address", "Label (optional):")
        try:
            address = self.wallet.new_address(label)
        except WalletError as exc:
            show_error(self, "Could not create an address", str(exc))
            return
        QtWidgets.QApplication.clipboard().setText(address)
        QtWidgets.QMessageBox.information(
            self, "New address", f"{address}\n\nThe address has been copied to the clipboard."
        )
        self._refresh_now()

    def _import_key(self) -> None:
        if not self._unlock_if_needed():
            return
        key, accepted = QtWidgets.QInputDialog.getText(
            self, "Import key", "Private key (view:spend, space separated):"
        )
        if not accepted or not key.strip():
            return
        try:
            address = self.keystore.import_key(key.strip())
            self.keystore.save()
        except WalletError as exc:
            show_error(self, "Could not import that key", str(exc))
            return
        QtWidgets.QMessageBox.information(self, "Key imported", f"Imported {address}")
        self._refresh_now()

    def _export_key(self) -> None:
        if not self._unlock_if_needed():
            return
        address = self._selected(self.address_table, 0) or self.keystore.default_address()
        try:
            key = self.keystore.export_key(address)
        except WalletError as exc:
            show_error(self, "Could not export that key", str(exc))
            return
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle("Private key")
        dialog.setIcon(QtWidgets.QMessageBox.Warning)
        dialog.setText(
            f"Private key for {address}:\n\n{key}\n\n"
            "Anyone who has this string can spend the coins on that address."
        )
        copy_button = dialog.addButton("Copy", QtWidgets.QMessageBox.ActionRole)
        dialog.addButton(QtWidgets.QMessageBox.Close)
        dialog.exec_()
        if dialog.clickedButton() is copy_button:
            QtWidgets.QApplication.clipboard().setText(key)

    def _change_password(self) -> None:
        if not self._unlock_if_needed():
            return
        password, accepted = QtWidgets.QInputDialog.getText(
            self,
            "Wallet password",
            "New password (leave empty to store the keys unencrypted):",
            QtWidgets.QLineEdit.Password,
        )
        if not accepted:
            return
        if password:
            repeat, accepted = QtWidgets.QInputDialog.getText(
                self, "Wallet password", "Repeat the password:", QtWidgets.QLineEdit.Password
            )
            if not accepted:
                return
            if repeat != password:
                show_error(self, "Password", "The passwords do not match.")
                return
        try:
            self.keystore.set_password(password or None)
        except WalletError as exc:
            show_error(self, "Could not change the password", str(exc))
            return
        self._update_lock_label()
        QtWidgets.QMessageBox.information(
            self,
            "Password",
            "The wallet is now encrypted." if password else "The wallet is no longer encrypted.",
        )

    def _send(self) -> None:
        if not self._unlock_if_needed():
            return
        destination = self.send_address.text().strip()
        if not destination:
            show_error(self, "Send", "Enter the address you want to pay.")
            return
        send_all = self.send_everything.isChecked()
        amount = 0
        if not send_all:
            try:
                amount = parse_amount(self.send_amount.text())
            except ValueError as exc:
                show_error(self, "Send", str(exc))
                return
            if amount <= 0:
                show_error(self, "Send", "The amount must be greater than zero.")
                return

        fee_rate = self.fee_rate.value()

        def build():
            if send_all:
                return self.wallet.send_everything(
                    destination, fee_per_kb=fee_rate, broadcast=False
                )
            return self.wallet.send(destination, amount, fee_per_kb=fee_rate, broadcast=False)

        self.send_button.setEnabled(False)
        self.send_status.setText("building the transaction...")
        self._run(build, self._confirm_send, self._send_failed)

    def _confirm_send(self, result) -> None:
        paid = sum(output.value for output in result.transaction.outputs) - result.change
        answer = QtWidgets.QMessageBox.question(
            self,
            "Confirm payment",
            f"Pay {format_amount(paid)} SCT to\n{self.send_address.text().strip()}\n\n"
            f"Fee: {format_amount(result.fee)} SCT ({result.size} bytes)\n"
            f"Transaction id: {result.transaction.txid_hex()}",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            self.send_button.setEnabled(True)
            self.send_status.setText("cancelled")
            return
        raw = result.transaction.serialize().hex()
        self.send_status.setText("broadcasting...")
        self._run(
            lambda: self.client.sendrawtransaction(raw),
            self._send_done,
            self._send_failed,
        )

    def _send_done(self, txid: str) -> None:
        self.send_button.setEnabled(True)
        self.send_amount.clear()
        self.send_address.clear()
        self.send_everything.setChecked(False)
        self.send_status.setText(f"sent: {txid}")
        self._refresh_now()

    def _send_failed(self, message: str) -> None:
        self.send_button.setEnabled(True)
        self.send_status.setText("")
        show_error(self, "Payment failed", message)

    def _run(self, task, on_success, on_error) -> None:
        thread = run_in_thread(self, task, on_success, on_error)
        self._threads = [item for item in self._threads if item.isRunning()]
        self._threads.append(thread)

    # ------------------------------------------------------------------- wallets

    def _open_wallet(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open wallet", str(self.keystore.path.parent), "Wallets (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            keystore = load_wallet(Path(path), self)
        except WalletError as exc:
            show_error(self, "Could not open that wallet", str(exc))
            return
        if keystore is not None:
            self._swap(keystore)

    def _create_wallet(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "New wallet", str(self.keystore.path.parent / "wallet.json"), "Wallets (*.json)"
        )
        if not path:
            return
        keystore = create_wallet(Path(path), self.keystore.params.name, self)
        if keystore is not None:
            self._swap(keystore)

    def _swap(self, keystore: Keystore) -> None:
        self._stop_polling()
        window = WalletWindow(keystore, self.client)
        window.show()
        self.close()

    def _about(self) -> None:
        QtWidgets.QMessageBox.about(
            self,
            "About ScarletCoin",
            f"<h3>ScarletCoin wallet {__version__}</h3>"
            "<p>A proof-of-work cryptocurrency with a real blockchain, "
            "peer-to-peer nodes, a wallet and a miner.</p>"
            f"<p>Network: {self.keystore.params.name}<br>"
            f"Node: {self.client.url}<br>"
            f"Wallet file: {self.keystore.path}</p>",
        )

    # -------------------------------------------------------------------- closing

    def _stop_polling(self) -> None:
        if self._poll_thread.isRunning():
            QtCore.QMetaObject.invokeMethod(self._poller, "stop", QtCore.Qt.QueuedConnection)
            self._poll_thread.quit()
            self._poll_thread.wait(2000)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Stop the worker threads, and any node this window started."""
        self._stop_polling()
        for thread in self._threads:
            thread.quit()
            thread.wait(2000)
        if self.local_node is not None and self.local_node.running:
            self.status.showMessage("stopping the node...")
            QtWidgets.QApplication.processEvents()
            self.local_node.stop()
        super().closeEvent(event)


# --------------------------------------------------------------------- start-up


def load_wallet(path: Path, parent: QtWidgets.QWidget | None) -> Keystore | None:
    """Open a wallet file, asking for the password when it is encrypted."""
    keystore = Keystore.load(path)
    if not keystore.encrypted:
        return keystore
    while True:
        password, accepted = QtWidgets.QInputDialog.getText(
            parent,
            "Unlock wallet",
            f"Password for {path.name}\n(cancel to open it in watch-only mode):",
            QtWidgets.QLineEdit.Password,
        )
        if not accepted:
            return keystore  # locked: balances only
        try:
            keystore.unlock(password)
            return keystore
        except WalletError as exc:
            show_error(parent, "Wrong password", str(exc))


def create_wallet(path: Path, network: str, parent: QtWidgets.QWidget | None) -> Keystore | None:
    """Create a new wallet file, asking for an optional password."""
    password, accepted = QtWidgets.QInputDialog.getText(
        parent,
        "New wallet",
        "Choose a password (leave empty to store the keys unencrypted):",
        QtWidgets.QLineEdit.Password,
    )
    if not accepted:
        return None
    if password:
        repeat, accepted = QtWidgets.QInputDialog.getText(
            parent, "New wallet", "Repeat the password:", QtWidgets.QLineEdit.Password
        )
        if not accepted or repeat != password:
            show_error(parent, "New wallet", "The passwords do not match.")
            return None
    try:
        return Keystore.create(path, network, password=password or None)
    except WalletError as exc:
        show_error(parent, "Could not create that wallet", str(exc))
        return None


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``scarlet-wallet-gui``."""
    parser = argparse.ArgumentParser(
        prog="scarlet-wallet-gui", description="ScarletCoin desktop wallet."
    )
    parser.add_argument("--wallet", type=Path, help="wallet file to open")
    add_common_gui_arguments(parser)
    args = parser.parse_args(argv)

    application = QtWidgets.QApplication(sys.argv[:1])
    apply_theme(application)

    path = args.wallet or default_wallet_path(args.datadir, args.network)
    if path.exists():
        try:
            keystore = load_wallet(path, None)
        except WalletError as exc:
            show_error(None, "Could not open that wallet", str(exc))
            return 1
    else:
        answer = QtWidgets.QMessageBox.question(
            None,
            "ScarletCoin",
            f"No wallet at\n{path}\n\nCreate one now?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return 1
        keystore = create_wallet(path, args.network, None)
    if keystore is None:
        return 1

    settings, local_node = resolve_startup(args)
    if settings is None:
        return 1

    window = WalletWindow(
        keystore,
        settings.client(),
        datadir=args.datadir,
        settings=settings,
        local_node=local_node,
    )
    window.show()
    return application.exec_()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
