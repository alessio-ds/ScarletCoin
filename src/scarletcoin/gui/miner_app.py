"""``scarlet-miner-gui``: a Qt front end for the miner."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from scarletcoin.gui import require_qt

QtCore, QtGui, QtWidgets = require_qt()

from scarletcoin import __version__  # noqa: E402
from scarletcoin.gui.common import (  # noqa: E402
    ConnectionSettings,
    LocalNodeDialog,
    PublicNodeDialog,
    add_common_gui_arguments,
    apply_theme,
    ask_for_node,
    monospace,
    resolve_startup,
    show_error,
    start_node_with_progress,
)
from scarletcoin.miner.miner import Miner  # noqa: E402
from scarletcoin.net.client import RpcClient, RpcClientError  # noqa: E402
from scarletcoin.net.launcher import LocalNode  # noqa: E402
from scarletcoin.units import format_amount  # noqa: E402
from scarletcoin.wallet.cli import default_wallet_path  # noqa: E402
from scarletcoin.wallet.keystore import Keystore, WalletError  # noqa: E402

__all__ = ["MinerWindow", "main"]


def format_rate(rate: float) -> str:
    """Render a hash rate with a sensible unit."""
    for unit in ("H/s", "kH/s", "MH/s", "GH/s"):
        if rate < 1000:
            return f"{rate:.2f} {unit}"
        rate /= 1000
    return f"{rate:.2f} TH/s"  # pragma: no cover


class MinerBridge(QtCore.QObject):
    """Runs a :class:`Miner` in a worker thread and forwards its events as signals."""

    # Note: the signal must not be called "event", which would shadow QObject.event.
    miner_event = QtCore.pyqtSignal(str, object)
    stopped = QtCore.pyqtSignal()

    def __init__(self, client: RpcClient, address: str, workers: int) -> None:
        super().__init__()
        self._miner = Miner(
            client,
            address,
            workers=workers,
            on_event=lambda kind, payload: self.miner_event.emit(kind, payload),
        )

    @property
    def stats(self):
        """Live statistics of the running miner."""
        return self._miner.stats

    @QtCore.pyqtSlot()
    def run(self) -> None:
        """Mine until stopped."""
        try:
            self._miner.run()
        except Exception as exc:
            self.miner_event.emit("error", {"message": str(exc)})
        finally:
            self.stopped.emit()

    def stop(self) -> None:
        """Ask the miner to finish."""
        self._miner.stop()


class MinerWindow(QtWidgets.QMainWindow):
    """The miner window: pick an address, press start, watch the hash rate."""

    def __init__(
        self,
        client: RpcClient,
        network: str,
        address: str = "",
        *,
        datadir: Path | None = None,
        settings: ConnectionSettings | None = None,
        local_node: LocalNode | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.network = network
        self.datadir = datadir
        self.local_node = local_node
        self.settings = settings or ConnectionSettings(client.url, client.token or "")
        self._bridge: MinerBridge | None = None
        self._thread: QtCore.QThread | None = None

        self.setWindowTitle(f"ScarletCoin miner - {network}")
        self.resize(640, 480)
        node_menu = self.menuBar().addMenu("&Node")
        node_menu.addAction("&Connection...", self.change_node)
        node_menu.addAction("Choose a &public node...", self.choose_public_node)
        node_menu.addAction("&Start a local node...", self.start_local_node)
        self._build_ui(address)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._refresh_node()

    def _build_ui(self, address: str) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("ScarletCoin miner")
        title.setObjectName("title")
        layout.addWidget(title)

        form = QtWidgets.QFormLayout()
        self.address_edit = QtWidgets.QLineEdit(address)
        self.address_edit.setPlaceholderText("address the block rewards are paid to")
        self.address_edit.setFont(monospace())
        form.addRow("Payout address", self.address_edit)

        self.workers_box = QtWidgets.QSpinBox()
        self.workers_box.setRange(1, 64)
        self.workers_box.setValue(max(1, (QtCore.QThread.idealThreadCount() or 2) - 1))
        form.addRow("CPU workers", self.workers_box)
        layout.addLayout(form)

        self.rate_label = QtWidgets.QLabel("0.00 H/s")
        self.rate_label.setObjectName("balance")
        layout.addWidget(self.rate_label)

        self.detail_label = QtWidgets.QLabel("idle")
        self.detail_label.setObjectName("muted")
        layout.addWidget(self.detail_label)

        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("Start mining")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._toggle)
        buttons.addWidget(self.start_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFont(monospace())
        layout.addWidget(self.log, 1)

        self.setCentralWidget(central)
        self.status = self.statusBar()

    # --------------------------------------------------------------------- events

    def _log(self, message: str) -> None:
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def start_local_node(self) -> None:
        """Start a node here; mining always works against one of your own."""
        if self._bridge is not None:
            show_error(self, "Miner", "Stop mining before changing the node.")
            return
        if self.local_node is not None and self.local_node.running:
            QtWidgets.QMessageBox.information(
                self, "Node", f"A node started here is already running at {self.client.url}."
            )
            return
        datadir = self.datadir or Path.cwd()
        options = LocalNodeDialog(self, self.network, datadir)
        if options.exec_() != QtWidgets.QDialog.Accepted:
            return
        node = start_node_with_progress(
            self, network=self.network, datadir=datadir, extra=options.extra_arguments()
        )
        if node is None:
            return
        self.local_node = node
        self.settings = ConnectionSettings(node.url, node.token)
        self.client = node.client(timeout=20.0)
        self._log(f"started a node at {node.url}")
        self._refresh_node()

    def choose_public_node(self) -> None:
        """Pick a public node that will hand out mining work."""
        if self._bridge is not None:
            show_error(self, "Miner", "Stop mining before changing the node.")
            return
        datadir = self.datadir or Path.cwd()
        picker = PublicNodeDialog(self, self.network, datadir, for_mining=True)
        if picker.exec_() != QtWidgets.QDialog.Accepted or picker.settings is None:
            return
        picker.settings.save(datadir, self.network)
        self.settings = picker.settings
        self.client = picker.settings.client()
        self._log(f"now using {self.client.url}")
        self._refresh_node()

    def change_node(self) -> None:
        """Ask for a different node; mining always follows the node it is given."""
        if self._bridge is not None:
            show_error(self, "Miner", "Stop mining before changing the node.")
            return
        chosen = ask_for_node(self, self.settings, self.network, self.datadir or Path.cwd())
        if chosen is None:
            return
        self.settings = chosen
        self.client = chosen.client()
        self._log(f"now using {self.client.url}")
        self._refresh_node()

    def _refresh_node(self) -> None:
        try:
            info = self.client.getinfo()
        except RpcClientError as exc:
            self.status.showMessage(
                f"no answer from {self.client.url} - {exc}"
                "   (Node > Connection... to use a different one)"
            )
            return
        chain = info.get("chain_size") or ""
        if info.get("syncing"):
            message = (
                f"{info['network']}  ·  syncing: height {info['height']:,} of "
                f"{info['syncing_to']:,} ({round(100 * info['sync_progress'])}%)"
            )
        else:
            message = (
                f"{info['network']}  ·  height {info['height']}"
                f"  ·  difficulty {info['difficulty']:.6g}"
                f"  ·  supply {format_amount(info['supply'])} SCT"
                + (f"  ·  chain {chain}" if chain else "")
            )
        self.status.showMessage(message)

    def _toggle(self) -> None:
        if self._bridge is None:
            self._start()
        else:
            self._stop()

    def _start(self) -> None:
        address = self.address_edit.text().strip()
        if not address:
            show_error(self, "Miner", "Enter the address the rewards should be paid to.")
            return
        try:
            self.client.getinfo()
        except RpcClientError as exc:
            show_error(self, "Miner", f"Cannot reach a node at {self.client.url}:\n{exc}")
            return
        try:
            self.client.getblocktemplate()
        except RpcClientError as exc:
            if exc.code in (401, -32001):
                show_error(
                    self,
                    "Miner",
                    f"The node at {self.client.url} will not hand out mining work "
                    "without its token.\n\nUse Node > Start a local node to run one "
                    "here, pick a public node that hands out work, or enter that "
                    "node's token under Node > Connection.",
                )
            else:
                show_error(self, "Miner", f"That node cannot give out work:\n{exc}")
            return

        self._bridge = MinerBridge(self.client, address, self.workers_box.value())
        self._thread = QtCore.QThread(self)
        self._bridge.moveToThread(self._thread)
        self._thread.started.connect(self._bridge.run)
        self._bridge.miner_event.connect(self._on_event)
        self._bridge.stopped.connect(self._on_stopped)
        self._thread.start()

        self.address_edit.setEnabled(False)
        self.workers_box.setEnabled(False)
        self.start_button.setText("Stop mining")
        self._log(f"mining to {address} with {self.workers_box.value()} worker(s)")

    def _stop(self) -> None:
        if self._bridge is not None:
            self.start_button.setEnabled(False)
            self.start_button.setText("Stopping...")
            self._bridge.stop()

    @QtCore.pyqtSlot()
    def _on_stopped(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        self._bridge = None
        self._thread = None
        self.address_edit.setEnabled(True)
        self.workers_box.setEnabled(True)
        self.start_button.setEnabled(True)
        self.start_button.setText("Start mining")
        self.rate_label.setText("0.00 H/s")
        self.detail_label.setText("idle")
        self._log("stopped")

    @QtCore.pyqtSlot(str, object)
    def _on_event(self, kind: str, payload: dict) -> None:
        if kind == "accepted":
            self._log(f"mined block {payload['hash']} at height {payload['height']}")
            self._refresh_node()
        elif kind == "rejected":
            self._log(f"block rejected: {payload['reason']}")
        elif kind == "error":
            self._log(f"error: {payload['message']}")
        elif kind == "template":
            self._log(f"new work for height {payload['height']} (bits {payload['bits']})")

    def _tick(self) -> None:
        if self._bridge is None:
            return
        stats = self._bridge.stats
        self.rate_label.setText(format_rate(stats.last_rate))
        self.detail_label.setText(
            f"average {format_rate(stats.average_rate)}  ·  "
            f"{stats.hashes:,} hashes  ·  {stats.blocks_accepted} blocks mined"
            f"  ·  height {stats.height}"
        )

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """Stop mining, and any node this window started, before closing."""
        if self._bridge is not None:
            self._bridge.stop()
            if self._thread is not None:
                self._thread.quit()
                self._thread.wait(3000)
        if self.local_node is not None and self.local_node.running:
            self.status.showMessage("stopping the node...")
            QtWidgets.QApplication.processEvents()
            self.local_node.stop()
        super().closeEvent(event)


def _wallet_address(datadir: Path, network: str) -> str:
    """Return the first address of the local wallet, if there is one."""
    path = default_wallet_path(datadir, network)
    if not path.exists():
        return ""
    try:
        return Keystore.load(path).default_address()
    except WalletError:
        return ""


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``scarlet-miner-gui``."""
    import multiprocessing

    multiprocessing.freeze_support()
    if sys.platform != "win32":
        multiprocessing.set_start_method("forkserver")

    parser = argparse.ArgumentParser(
        prog="scarlet-miner-gui", description="ScarletCoin desktop miner."
    )
    parser.add_argument("address", nargs="?", default=None, help="payout address")
    add_common_gui_arguments(parser)
    args = parser.parse_args(argv)

    application = QtWidgets.QApplication(sys.argv[:1])
    apply_theme(application)
    application.setApplicationName(f"ScarletCoin miner {__version__}")

    address = args.address or _wallet_address(args.datadir, args.network)
    settings, local_node = resolve_startup(args, for_mining=True)
    if settings is None:
        return 1

    window = MinerWindow(
        settings.client(),
        args.network,
        address,
        datadir=args.datadir,
        settings=settings,
        local_node=local_node,
    )
    window.show()
    return application.exec_()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
