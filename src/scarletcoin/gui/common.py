"""Shared pieces for the Qt applications: theme, worker threads and dialogs."""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from PyQt5 import QtCore, QtGui, QtWidgets

from scarletcoin.cli_common import (
    DEFAULT_DATADIR,
    NodeConnection,
    connection_path,
    load_connection,
    local_url,
    parse_proxy,
    read_rpc_token,
    save_connection,
)
from scarletcoin.core.chain import MIN_PRUNE_KEEP, prune_database
from scarletcoin.core.params import get_params, network_names
from scarletcoin.core.storage import inspect_database
from scarletcoin.net import directory
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.net.launcher import LocalNode, LocalNodeError, already_running
from scarletcoin.net.node import NodeConfig
from scarletcoin.units import format_bytes

__all__ = [
    "STYLESHEET",
    "ConnectionSettings",
    "LocalNodeDialog",
    "NodeDialog",
    "PollWorker",
    "PublicNodeDialog",
    "StartupDialog",
    "add_common_gui_arguments",
    "apply_theme",
    "ask_for_node",
    "choose_startup_node",
    "client_from_args",
    "is_loopback",
    "monospace",
    "resolve_startup",
    "settings_from_args",
    "show_error",
    "start_node_with_progress",
]

logger = logging.getLogger(__name__)

#: A dark, scarlet-accented theme applied to both applications.
STYLESHEET = """
QWidget { background: #12100f; color: #e8e2df; font-size: 13px; }
QMainWindow, QDialog { background: #12100f; }
QLabel#title { font-size: 20px; font-weight: 600; letter-spacing: 1px; color: #e33a4e; }
QLabel#balance { font-size: 30px; font-weight: 600; color: #f4eeea; }
QLabel#muted, QLabel#hint { color: #97877f; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: #1b1817; border: 1px solid #2e2825; border-radius: 6px; padding: 6px 8px;
    selection-background-color: #e33a4e;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #e33a4e; }
QLineEdit[readOnly="true"] { color: #c7bcb6; }
QPushButton {
    background: #26211f; border: 1px solid #362f2c; border-radius: 6px; padding: 7px 14px;
}
QPushButton:hover { border-color: #e33a4e; }
QPushButton:disabled { color: #6b5e58; border-color: #262120; }
QPushButton#primary { background: #e33a4e; border: 0; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #f04a5e; }
QPushButton#primary:disabled { background: #5a2b32; color: #c9b8b8; }
QTabWidget::pane { border: 1px solid #2e2825; border-radius: 6px; }
QTabBar::tab {
    background: #1b1817; padding: 8px 16px; border: 1px solid #2e2825; border-bottom: 0;
    border-top-left-radius: 6px; border-top-right-radius: 6px; color: #97877f;
}
QTabBar::tab:selected { color: #e8e2df; border-color: #4a3f3b; }
QHeaderView::section {
    background: #1b1817; color: #97877f; border: 0; border-bottom: 1px solid #2e2825;
    padding: 6px 8px; font-weight: 600;
}
QTableWidget, QTableView {
    background: #1b1817; gridline-color: #2e2825; border: 1px solid #2e2825;
    border-radius: 6px; alternate-background-color: #191615;
}
QTableWidget::item:selected { background: #3a2429; }
QStatusBar { color: #97877f; border-top: 1px solid #2e2825; }
QProgressBar { border: 1px solid #2e2825; border-radius: 6px; text-align: center; }
QProgressBar::chunk { background: #e33a4e; }
QMenuBar, QMenu { background: #1b1817; }
QMenu::item:selected, QMenuBar::item:selected { background: #3a2429; }
"""


def apply_theme(app: QtWidgets.QApplication) -> None:
    """Apply the ScarletCoin look to a Qt application."""
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setApplicationName("ScarletCoin")


def monospace() -> QtGui.QFont:
    """Return the font used for hashes and addresses."""
    font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    font.setPointSize(max(9, font.pointSize()))
    return font


def show_error(parent: QtWidgets.QWidget | None, title: str, message: str) -> None:
    """Show a modal error dialog."""
    QtWidgets.QMessageBox.critical(parent, title, message)


def add_common_gui_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the network and RPC options shared by both applications."""
    parser.add_argument("--network", default="mainnet", choices=network_names())
    parser.add_argument("--datadir", type=Path, default=DEFAULT_DATADIR)
    parser.add_argument("--rpc-url", help="node RPC URL")
    parser.add_argument("--rpc-token", help="node RPC token")
    parser.add_argument(
        "--proxy",
        metavar="HOST:PORT",
        help="route RPC requests through a SOCKS5 proxy, e.g. 127.0.0.1:9050 for Tor",
    )
    parser.add_argument(
        "--node",
        metavar="local|public|ask|URL",
        help="which node to use: 'local' for one on this machine, 'public' to pick a"
        " public node, 'ask' to be offered the choice again, or a node URL",
    )
    parser.add_argument(
        "--no-start-node",
        action="store_true",
        help="do not start a node automatically when none is running locally",
    )


def default_url(network: str) -> str:
    """The node URL assumed when nothing else is configured."""
    return local_url(network)


def settings_from_args(args: argparse.Namespace) -> ConnectionSettings:
    """Work out which node to use: command line first, then saved, then localhost.

    The node running on this machine owns ``rpc.token``; a token saved by the
    connection dialog may belong to an older node, so for the default localhost
    URL the file on disk is the source of truth.
    """
    saved = ConnectionSettings.load(args.datadir, args.network)
    url = args.rpc_url or (saved.url if saved else default_url(args.network))
    if args.rpc_token:
        token = args.rpc_token
    elif url == default_url(args.network):
        token = read_rpc_token(args.datadir, args.network) or (saved.token if saved else "")
    else:
        token = saved.token if saved else ""
    proxy_host = saved.proxy_host if saved else ""
    proxy_port = saved.proxy_port if saved else 9050
    if getattr(args, "proxy", None):
        try:
            parsed = parse_proxy(args.proxy)
        except ValueError:
            parsed = None
        if parsed is not None:
            proxy_host, proxy_port = parsed
    return ConnectionSettings(url, token, proxy_host, proxy_port)


def client_from_args(args: argparse.Namespace) -> RpcClient:
    """Build an RPC client from parsed arguments and saved settings."""
    return settings_from_args(args).client()


class PollWorker(QtCore.QObject):
    """Runs a callable on a timer inside its own thread and reports the result.

    Qt widgets may only be touched from the GUI thread, so the worker
    communicates exclusively through the :attr:`ready` and :attr:`failed`
    signals.
    """

    ready = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, task, interval_ms: int = 5000) -> None:
        super().__init__()
        self._task = task
        self._interval = interval_ms
        self._timer: QtCore.QTimer | None = None

    @QtCore.pyqtSlot()
    def start(self) -> None:
        """Begin polling; called once the worker's thread is running."""
        self._timer = QtCore.QTimer()
        self._timer.setInterval(self._interval)
        self._timer.timeout.connect(self.poll)
        self._timer.start()
        self.poll()

    @QtCore.pyqtSlot()
    def poll(self) -> None:
        """Run the task once and emit the outcome."""
        try:
            self.ready.emit(self._task())
        except Exception as exc:
            self.failed.emit(str(exc))

    @QtCore.pyqtSlot()
    def stop(self) -> None:
        """Stop polling."""
        if self._timer is not None:
            self._timer.stop()


class CallWorker(QtCore.QObject):
    """Runs one blocking call in a worker thread and reports the result."""

    finished = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, task) -> None:
        super().__init__()
        self._task = task

    @QtCore.pyqtSlot()
    def run(self) -> None:
        """Execute the task."""
        try:
            self.finished.emit(self._task())
        except Exception as exc:
            self.failed.emit(str(exc))


def run_in_thread(parent: QtCore.QObject, task, on_success, on_error) -> QtCore.QThread:
    """Run ``task`` off the GUI thread and deliver the outcome to the callbacks."""
    thread = QtCore.QThread(parent)
    worker = CallWorker(task)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(on_success)
    worker.failed.connect(on_error)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    # Keep references alive until the thread finishes.
    thread._worker = worker  # type: ignore[attr-defined]
    thread.start()
    return thread


@dataclass
class ConnectionSettings:
    """Where a desktop application should look for a node.

    Saved in ``<datadir>/<network>/node.json`` — the same file the command line
    tools read — so a node chosen in the wallet is also the one ``scarlet-wallet``
    uses in a terminal. Command line options always win over the saved value.
    """

    url: str
    token: str = ""
    proxy_host: str = ""
    proxy_port: int = 9050

    @staticmethod
    def path(datadir: Path, network: str) -> Path:
        """Location of the settings file."""
        return connection_path(datadir, network)

    @classmethod
    def load(cls, datadir: Path, network: str) -> ConnectionSettings | None:
        """Read saved settings, or ``None`` if there are none."""
        found = load_connection(datadir, network)
        return (
            None
            if found is None
            else cls(found.url, found.token, found.proxy_host, found.proxy_port)
        )

    def save(self, datadir: Path, network: str) -> None:
        """Store the settings, keeping the file private."""
        save_connection(
            datadir, network, NodeConnection(self.url, self.token, self.proxy_host, self.proxy_port)
        )

    def client(self, timeout: float = 20.0) -> RpcClient:
        """Build a client from these settings."""
        return RpcClient(
            self.url,
            token=self.token or None,
            timeout=timeout,
            proxy_host=self.proxy_host or None,
            proxy_port=self.proxy_port,
        )

    def answers(self, timeout: float = 6.0) -> bool:
        """Whether a node actually replies here."""
        try:
            self.client(timeout=timeout).getinfo()
        except RpcClientError:
            return False
        return True


class NodeDialog(QtWidgets.QDialog):
    """Asks which node to talk to, and checks that it answers before accepting."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        settings: ConnectionSettings,
        network: str,
        *,
        reason: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Node connection")
        self.setMinimumWidth(520)
        self._network = network
        self.settings = ConnectionSettings(
            settings.url, settings.token, settings.proxy_host, settings.proxy_port
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        message = reason or f"Choose the {network} node this application should use."
        headline = QtWidgets.QLabel(message)
        headline.setWordWrap(True)
        layout.addWidget(headline)

        hint = QtWidgets.QLabel(
            "Either run a node on this machine and keep the address below, or point "
            "this at somebody else's node. A public node needs no token; your own "
            "node writes one to <datadir>/&lt;network&gt;/rpc.token."
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        layout.addWidget(hint)

        form = QtWidgets.QFormLayout()
        self.url_edit = QtWidgets.QLineEdit(self.settings.url)
        self.url_edit.setPlaceholderText("http://127.0.0.1:20332")
        form.addRow("Node URL", self.url_edit)
        self.token_edit = QtWidgets.QLineEdit(self.settings.token)
        self.token_edit.setPlaceholderText("only for your own node")
        self.token_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        form.addRow("RPC token", self.token_edit)

        self.proxy_box = QtWidgets.QCheckBox("Use a SOCKS5 proxy (e.g. Tor)")
        self.proxy_box.setChecked(bool(self.settings.proxy_host))
        form.addRow("", self.proxy_box)

        proxy_row = QtWidgets.QHBoxLayout()
        self.proxy_host_edit = QtWidgets.QLineEdit(self.settings.proxy_host or "127.0.0.1")
        self.proxy_host_edit.setPlaceholderText("127.0.0.1")
        proxy_row.addWidget(self.proxy_host_edit, 1)
        proxy_row.addWidget(QtWidgets.QLabel(":"))
        self.proxy_port_edit = QtWidgets.QLineEdit(str(self.settings.proxy_port or 9050))
        self.proxy_port_edit.setPlaceholderText("9050")
        self.proxy_port_edit.setMaximumWidth(80)
        proxy_row.addWidget(self.proxy_port_edit)
        form.addRow("Proxy host:port", proxy_row)
        self.proxy_box.toggled.connect(self.proxy_host_edit.setEnabled)
        self.proxy_box.toggled.connect(self.proxy_port_edit.setEnabled)
        self.proxy_host_edit.setEnabled(self.proxy_box.isChecked())
        self.proxy_port_edit.setEnabled(self.proxy_box.isChecked())
        layout.addLayout(form)

        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("hint")
        layout.addWidget(self.status)

        buttons = QtWidgets.QHBoxLayout()
        test_button = QtWidgets.QPushButton("Test")
        test_button.clicked.connect(self._test)
        buttons.addWidget(test_button)
        buttons.addStretch(1)
        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.setObjectName("primary")
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self._accept)
        buttons.addWidget(self.connect_button)
        layout.addLayout(buttons)

    def _current(self) -> ConnectionSettings:
        host = self.proxy_host_edit.text().strip()
        port_text = self.proxy_port_edit.text().strip()
        port = 9050
        try:
            if port_text:
                port = int(port_text)
        except ValueError:
            port = 9050
        proxy_host = host if self.proxy_box.isChecked() and host else ""
        return ConnectionSettings(
            self.url_edit.text().strip(),
            self.token_edit.text().strip(),
            proxy_host,
            port,
        )

    def _describe(self, settings: ConnectionSettings) -> str:
        """Try the node and return a human description of what happened."""
        if not settings.url:
            return "Enter the address of a node."
        try:
            info = settings.client(timeout=10.0).getinfo()
        except RpcClientError as exc:
            return f"No answer: {exc}"
        if info.get("network") != self._network:
            return (
                f"That node runs the {info.get('network')} network,"
                f" but this is a {self._network} wallet."
            )
        return f"Connected: {info['network']} at height {info['height']}, {info['peers']} peers."

    def _test(self) -> None:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self.status.setText(self._describe(self._current()))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _accept(self) -> None:
        candidate = self._current()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            message = self._describe(candidate)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.status.setText(message)
        if not message.startswith("Connected"):
            return
        self.settings = candidate
        self.accept()


def ask_for_node(
    parent: QtWidgets.QWidget | None,
    settings: ConnectionSettings,
    network: str,
    datadir: Path,
    *,
    reason: str = "",
) -> ConnectionSettings | None:
    """Show :class:`NodeDialog` and save the result if the user accepts."""
    dialog = NodeDialog(parent, settings, network, reason=reason)
    if dialog.exec_() != QtWidgets.QDialog.Accepted:
        return None
    dialog.settings.save(datadir, network)
    return dialog.settings


def is_loopback(url: str) -> bool:
    """Whether ``url`` points at this machine, and so at a node we may start."""
    host = urlparse(url).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0")


def start_node_with_progress(
    parent: QtWidgets.QWidget | None,
    *,
    network: str,
    datadir: Path,
    rpc_port: int | None = None,
    extra: tuple[str, ...] = (),
) -> LocalNode | None:
    """Start a node, showing progress and keeping the interface responsive.

    Args:
        parent: Dialog parent.
        network: Which network the node should join.
        datadir: Where its chain lives.
        rpc_port: Bind the RPC server here instead of the network default.
        extra: Further ``scarlet-node run`` options, such as ``--rpc-public`` or
            ``--prune``, as produced by :meth:`LocalNodeDialog.extra_arguments`.

    Returns:
        The running node, or ``None`` if the user cancelled or it failed (in
        which case the failure has already been reported).
    """
    dialog = QtWidgets.QProgressDialog(f"Starting a {network} node...", "Cancel", 0, 0, parent)
    dialog.setWindowTitle("ScarletCoin")
    dialog.setWindowModality(QtCore.Qt.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setValue(0)
    dialog.show()
    QtWidgets.QApplication.processEvents()

    node: LocalNode | None = None
    try:
        node = LocalNode.launch(
            network=network, datadir=datadir, rpc_port=rpc_port, extra=list(extra)
        )
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            QtWidgets.QApplication.processEvents()
            if dialog.wasCanceled():
                node.stop()
                return None
            if not node.running:
                raise LocalNodeError("the node stopped while starting up:\n\n" + node.tail_log())
            if node.is_ready():
                dialog.setLabelText("Node ready.")
                return node
            time.sleep(0.2)
        raise LocalNodeError(f"the node did not answer in time:\n\n{node.tail_log()}")
    except LocalNodeError as exc:
        if node is not None:
            node.stop()
        show_error(parent, "Could not start a node", str(exc))
        return None
    finally:
        dialog.close()


# ------------------------------------------------------------------- local nodes


def chain_summary(network: str, datadir: Path) -> dict:
    """Read what is known about the chain already stored on this machine."""
    return inspect_database(NodeConfig(network=network, datadir=Path(datadir)).chain_path)


class LocalNodeDialog(QtWidgets.QDialog):
    """Shown before a node is started here: how big the chain is, and what to do.

    Three decisions belong to the person starting the node and to nobody else:
    how much disk to spend on old blocks, whether strangers may use this node,
    and whether they may mine through it.  All three are on this one screen, with
    the size of the existing chain in plain sight, because that is the number
    that makes the pruning question worth asking.
    """

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        network: str,
        datadir: Path,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Start a node on this machine")
        self.setMinimumWidth(560)
        self._network = network
        self._datadir = Path(datadir)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        headline = QtWidgets.QLabel(
            f"A {network} node on this machine validates every block itself and needs "
            "no one's permission. It costs disk space and, the first time, a while to "
            "catch up with the network."
        )
        headline.setWordWrap(True)
        layout.addWidget(headline)

        self.size_label = QtWidgets.QLabel()
        self.size_label.setWordWrap(True)
        self.size_label.setTextFormat(QtCore.Qt.RichText)
        layout.addWidget(self.size_label)

        disk = QtWidgets.QGroupBox("Disk")
        disk_layout = QtWidgets.QVBoxLayout(disk)
        self.prune_box = QtWidgets.QCheckBox("Keep only recent blocks (prune)")
        self.prune_box.setToolTip(
            "Balances stay exact. What goes is the ability to show old blocks, "
            "serve them to a peer syncing from scratch, and reorganise past them."
        )
        disk_layout.addWidget(self.prune_box)
        keep_row = QtWidgets.QHBoxLayout()
        keep_row.addSpacing(22)
        keep_row.addWidget(QtWidgets.QLabel("keep the last"))
        self.keep_spin = QtWidgets.QSpinBox()
        self.keep_spin.setRange(MIN_PRUNE_KEEP, 100_000_000)
        self.keep_spin.setSingleStep(1000)
        self.keep_spin.setValue(MIN_PRUNE_KEEP)
        self.keep_spin.setSuffix(" blocks")
        self.keep_spin.setEnabled(False)
        keep_row.addWidget(self.keep_spin)
        self.prune_now_button = QtWidgets.QPushButton("Prune the chain now")
        self.prune_now_button.setEnabled(False)
        self.prune_now_button.clicked.connect(self._prune_now)
        keep_row.addWidget(self.prune_now_button)
        keep_row.addStretch(1)
        disk_layout.addLayout(keep_row)
        self.prune_box.toggled.connect(self.keep_spin.setEnabled)
        self.prune_box.toggled.connect(self._update_prune_button)
        layout.addWidget(disk)

        sharing = QtWidgets.QGroupBox("Sharing")
        sharing_layout = QtWidgets.QVBoxLayout(sharing)
        self.public_box = QtWidgets.QCheckBox("Let other people's wallets use this node")
        self.public_box.setToolTip(
            "--rpc-public: read-only and broadcast calls are answered without the "
            "token. Everything else still needs it."
        )
        sharing_layout.addWidget(self.public_box)
        mining_row = QtWidgets.QHBoxLayout()
        mining_row.addSpacing(22)
        self.public_mining_box = QtWidgets.QCheckBox("...and let them mine through it")
        self.public_mining_box.setToolTip("--rpc-public-mining: hands out block templates too.")
        self.public_mining_box.setEnabled(False)
        mining_row.addWidget(self.public_mining_box)
        mining_row.addStretch(1)
        sharing_layout.addLayout(mining_row)
        advertise_row = QtWidgets.QHBoxLayout()
        advertise_row.addSpacing(22)
        advertise_row.addWidget(QtWidgets.QLabel("public address"))
        self.advertise_edit = QtWidgets.QLineEdit()
        self.advertise_edit.setPlaceholderText("https://node.example.net  (optional)")
        self.advertise_edit.setEnabled(False)
        advertise_row.addWidget(self.advertise_edit, 1)
        sharing_layout.addLayout(advertise_row)
        self.public_box.toggled.connect(self.public_mining_box.setEnabled)
        self.public_box.toggled.connect(self.advertise_edit.setEnabled)
        layout.addWidget(sharing)

        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("hint")
        layout.addWidget(self.status)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        start = QtWidgets.QPushButton("Start node")
        start.setObjectName("primary")
        start.setDefault(True)
        start.clicked.connect(self.accept)
        buttons.addWidget(start)
        layout.addLayout(buttons)

        self._refresh_sizes()

    # --------------------------------------------------------------------- sizes

    def _refresh_sizes(self) -> None:
        summary = chain_summary(self._network, self._datadir)
        if not summary["exists"]:
            self.size_label.setText(
                f"<b>No {self._network} chain here yet.</b><br>"
                "The node will download it from the network, starting at the genesis "
                "block."
            )
            self._update_prune_button()
            return
        lines = [
            f"<b>Chain on this machine:</b> height {summary['height']}, "
            f"{format_bytes(summary['chain_bytes'] or 0)} of blocks, "
            f"<b>{format_bytes(summary['disk_bytes'])} on disk</b>."
        ]
        if summary["pruned_blocks"]:
            lines.append(
                f"Already pruned: {summary['pruned_blocks']} block bodies up to height "
                f"{summary['prune_height']} are gone."
            )
        lines.append(f"<span style='color:#97877f'>{summary['path']}</span>")
        self.size_label.setText("<br>".join(lines))
        self._update_prune_button()

    def _update_prune_button(self, *_ignored: object) -> None:
        summary = chain_summary(self._network, self._datadir)
        can_prune = bool(summary["exists"]) and self.prune_box.isChecked()
        self.prune_now_button.setEnabled(can_prune)

    def _prune_now(self) -> None:
        """Prune the stored chain before the node starts using it."""
        keep = self.keep_spin.value()
        if already_running(local_url(self._network)):
            show_error(
                self,
                "Prune",
                "A node is already running here and has the chain open. Stop it "
                "first, or prune from its own window.",
            )
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Prune the chain",
            f"Drop the bodies of every block except the last {keep:,}?\n\n"
            "Balances stay exact, but this node will no longer be able to show "
            "those blocks, serve them to a peer syncing from scratch, or "
            "reorganise past them.\n\nThis cannot be undone.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        progress = QtWidgets.QProgressDialog("Pruning...", "", 0, 0, self)
        progress.setCancelButton(None)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QtWidgets.QApplication.processEvents()
        try:
            result, disk = prune_database(
                NodeConfig(network=self._network, datadir=self._datadir).chain_path,
                get_params(self._network),
                keep,
            )
        except Exception as exc:  # pragma: no cover - disk or lock failures
            progress.close()
            show_error(self, "Prune failed", str(exc))
            return
        progress.close()
        self.status.setText(
            f"pruned {result.blocks} block(s) up to height {result.prune_height},"
            f" freeing {format_bytes(result.freed_bytes)};"
            f" the database is now {format_bytes(disk)}"
        )
        self._refresh_sizes()

    # ----------------------------------------------------------------- the answer

    def extra_arguments(self) -> tuple[str, ...]:
        """The ``scarlet-node run`` options this dialog's answers translate to."""
        extra: list[str] = []
        if self.prune_box.isChecked():
            extra += ["--prune", str(self.keep_spin.value())]
        if self.public_box.isChecked():
            extra.append("--rpc-public")
            if self.public_mining_box.isChecked():
                extra.append("--rpc-public-mining")
            advertise = self.advertise_edit.text().strip()
            if advertise:
                extra += ["--rpc-advertise", advertise]
        return tuple(extra)


# ------------------------------------------------------------------ public nodes


class PublicNodeDialog(QtWidgets.QDialog):
    """A list of the public nodes that are up, so picking one is a single click.

    The list comes from :mod:`scarletcoin.net.directory`: the addresses built
    into this release, the ones this machine has saved, and the ones those nodes
    say they know.  Every entry is probed, so what is on screen is what is
    actually answering right now, not a hopeful list of names.
    """

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        network: str,
        datadir: Path,
        *,
        for_mining: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Public {network} nodes")
        self.setMinimumSize(700, 380)
        self._network = network
        self._datadir = Path(datadir)
        self._for_mining = for_mining
        self._statuses: list[directory.NodeStatus] = []
        self.settings: ConnectionSettings | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)

        hint = QtWidgets.QLabel(
            "A public node lets you use ScarletCoin without downloading the chain. "
            "You are trusting its view of the network, so prefer one at the same "
            "height as the others — or run your own."
            + (
                "\n\nMining needs a node that hands out work; those are marked below."
                if for_mining
                else ""
            )
        )
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        layout.addWidget(hint)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Node", "Status", "Height", "Source"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self._accept)
        layout.addWidget(self.table, 1)

        self.status = QtWidgets.QLabel("looking for public nodes...")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_button)
        add_button = QtWidgets.QPushButton("Add a node...")
        add_button.clicked.connect(self._add)
        buttons.addWidget(add_button)
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.use_button = QtWidgets.QPushButton("Use this node")
        self.use_button.setObjectName("primary")
        self.use_button.setDefault(True)
        self.use_button.setEnabled(False)
        self.use_button.clicked.connect(self._accept)
        buttons.addWidget(self.use_button)
        layout.addLayout(buttons)

        self.refresh()

    def refresh(self) -> None:
        """Probe every known public node again, off the interface thread."""
        self.refresh_button.setEnabled(False)
        self.use_button.setEnabled(False)
        self.status.setText("looking for public nodes...")
        network, datadir = self._network, self._datadir
        run_in_thread(
            self,
            lambda: directory.discover(network, datadir),
            self._show,
            self._failed,
        )

    @QtCore.pyqtSlot(object)
    def _show(self, statuses: object) -> None:
        self._statuses = list(statuses)  # type: ignore[arg-type]
        self.refresh_button.setEnabled(True)
        font = monospace()
        self.table.setRowCount(len(self._statuses))
        for row, status in enumerate(self._statuses):
            host = QtWidgets.QTableWidgetItem(status.node.label)
            host.setFont(font)
            self.table.setItem(row, 0, host)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(status.describe()))
            height = "" if status.height is None else f"{status.height:,}"
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(height))
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(status.node.source))
            if not status.usable(self._network, for_mining=self._for_mining):
                for column in range(4):
                    item = self.table.item(row, column)
                    if item is not None:
                        item.setForeground(QtGui.QColor("#6b5e58"))
        self.table.resizeColumnsToContents()
        usable = [
            index
            for index, status in enumerate(self._statuses)
            if status.usable(self._network, for_mining=self._for_mining)
        ]
        if usable:
            self.table.selectRow(usable[0])
            self.use_button.setEnabled(True)
            self.status.setText(f"{len(usable)} node(s) answered on {self._network}")
        else:
            self.status.setText(
                f"no public {self._network} node answered. Add one with the button "
                "below, or run a node of your own."
            )

    @QtCore.pyqtSlot(str)
    def _failed(self, message: str) -> None:
        self.refresh_button.setEnabled(True)
        self.status.setText(f"could not look for public nodes: {message}")

    def _add(self) -> None:
        text, accepted = QtWidgets.QInputDialog.getText(
            self, "Add a public node", "Address of the node:", text="https://"
        )
        if not accepted:
            return
        url = directory.normalise_url(text)
        if not url:
            show_error(self, "Add a public node", f"{text!r} is not an address.")
            return
        directory.remember_node(self._datadir, self._network, url)
        self.refresh()

    def _selected(self) -> directory.NodeStatus | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._statuses):
            return self._statuses[row]
        return None

    def _accept(self) -> None:
        chosen = self._selected()
        if chosen is None:
            return
        if not chosen.usable(self._network, for_mining=self._for_mining):
            self.status.setText(f"that node cannot be used: {chosen.describe()}")
            return
        directory.remember_node(self._datadir, self._network, chosen.url)
        self.settings = ConnectionSettings(chosen.url, "")
        self.accept()


# ------------------------------------------------------------------- the question


class StartupDialog(QtWidgets.QDialog):
    """Asks the one question a newcomer cannot avoid: whose node?

    Running a node is the honest answer and the default; using a public one is
    the quick answer. Both are offered plainly, with what each costs, rather than
    one being hidden behind a menu.
    """

    LOCAL = "local"
    PUBLIC = "public"
    MANUAL = "manual"

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        network: str,
        datadir: Path,
        *,
        reason: str = "",
        for_mining: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("ScarletCoin")
        self.setMinimumWidth(560)
        self.answer: str | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(14)

        title = QtWidgets.QLabel(f"Which {network} node should this use?")
        title.setObjectName("title")
        layout.addWidget(title)

        if reason:
            note = QtWidgets.QLabel(reason)
            note.setWordWrap(True)
            note.setObjectName("hint")
            layout.addWidget(note)

        summary = chain_summary(network, datadir)
        if summary["exists"]:
            local_detail = (
                f"Validates everything itself. The chain here is at height "
                f"{summary['height']} and takes {format_bytes(summary['disk_bytes'])} "
                "on disk; you can prune it on the next screen."
            )
        else:
            local_detail = (
                "Validates everything itself, trusting nobody. It has to download "
                "the chain first, which takes time and disk space."
            )
        layout.addWidget(
            self._option(
                "Run a node on this machine",
                local_detail,
                self.LOCAL,
                primary=True,
            )
        )
        public_detail = (
            "Ready immediately and stores nothing, but you are trusting somebody "
            "else's view of the chain. Your keys never leave this machine either way."
        )
        if for_mining:
            public_detail += " Mining only works if that node hands out work."
        layout.addWidget(self._option("Connect to a public node", public_detail, self.PUBLIC))
        layout.addWidget(
            self._option(
                "Enter a node address",
                "For a node you run elsewhere, or somebody else's with a token.",
                self.MANUAL,
            )
        )

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        cancel = QtWidgets.QPushButton("Quit")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _option(self, title: str, detail: str, answer: str, *, primary: bool = False):
        box = QtWidgets.QGroupBox()
        row = QtWidgets.QHBoxLayout(box)
        text = QtWidgets.QVBoxLayout()
        heading = QtWidgets.QLabel(f"<b>{title}</b>")
        text.addWidget(heading)
        body = QtWidgets.QLabel(detail)
        body.setWordWrap(True)
        body.setObjectName("hint")
        text.addWidget(body)
        row.addLayout(text, 1)
        button = QtWidgets.QPushButton("Choose")
        if primary:
            button.setObjectName("primary")
            button.setDefault(True)
        button.clicked.connect(lambda: self._pick(answer))
        row.addWidget(button)
        return box

    def _pick(self, answer: str) -> None:
        self.answer = answer
        self.accept()


def choose_startup_node(
    parent: QtWidgets.QWidget | None,
    *,
    network: str,
    datadir: Path,
    settings: ConnectionSettings,
    reason: str = "",
    preference: str | None = None,
    for_mining: bool = False,
    allow_start: bool = True,
) -> tuple[ConnectionSettings | None, LocalNode | None]:
    """Ask which node to use and act on the answer.

    Args:
        parent: Dialog parent.
        network: Which network the application is on.
        datadir: Data directory, used for the chain, the token and the saved
            answer.
        settings: What the application would have used, offered as the default in
            the manual dialog.
        reason: Why the question is being asked, shown at the top.
        preference: Skip the question: ``"local"``, ``"public"`` or ``"ask"``.
        for_mining: Mark public nodes that cannot hand out mining work.
        allow_start: Whether starting a node here is permitted at all.

    Returns:
        The chosen connection and, if one was started here, the node — which the
        caller owns and must stop. ``(None, None)`` if the user gave up.
    """
    datadir = Path(datadir)
    while True:
        answer = preference
        if answer not in (StartupDialog.LOCAL, StartupDialog.PUBLIC, StartupDialog.MANUAL):
            dialog = StartupDialog(parent, network, datadir, reason=reason, for_mining=for_mining)
            if dialog.exec_() != QtWidgets.QDialog.Accepted or dialog.answer is None:
                return None, None
            answer = dialog.answer
        preference = None  # a second time round asks properly

        if answer == StartupDialog.LOCAL:
            if not allow_start:
                show_error(
                    parent,
                    "Node",
                    "Starting a node here was turned off with --no-start-node.",
                )
                continue
            running = ConnectionSettings(local_url(network), read_rpc_token(datadir, network) or "")
            if running.answers(timeout=4.0):
                running.save(datadir, network)
                return running, None
            options = LocalNodeDialog(parent, network, datadir)
            if options.exec_() != QtWidgets.QDialog.Accepted:
                continue
            node = start_node_with_progress(
                parent, network=network, datadir=datadir, extra=options.extra_arguments()
            )
            if node is None:
                continue
            chosen = ConnectionSettings(node.url, node.token)
            chosen.save(datadir, network)
            return chosen, node

        if answer == StartupDialog.PUBLIC:
            picker = PublicNodeDialog(parent, network, datadir, for_mining=for_mining)
            if picker.exec_() != QtWidgets.QDialog.Accepted or picker.settings is None:
                continue
            picker.settings.save(datadir, network)
            return picker.settings, None

        chosen = ask_for_node(parent, settings, network, datadir, reason=reason)
        if chosen is None:
            continue
        return chosen, None


def _why_not(settings: ConnectionSettings, network: str) -> str:
    """Try the configured node and explain, in a sentence, what went wrong."""
    try:
        info = settings.client(timeout=10.0).getinfo()
    except RpcClientError as exc:
        if exc.code == 401:
            return (
                f"A node is already running at {settings.url} but refused the RPC "
                "token, so it was probably started by another program. Stop it, or "
                "enter its token."
            )
        return f"No {network} node answered at {settings.url}."
    if info.get("network") != network:
        return (
            f"The node at {settings.url} is on the {info.get('network')} network,"
            f" but this is a {network} application."
        )
    return ""


def resolve_startup(
    args: argparse.Namespace,
    *,
    for_mining: bool = False,
) -> tuple[ConnectionSettings | None, LocalNode | None]:
    """Settle on a node before the main window opens.

    A node that already answers is used without a word. Otherwise the choice
    between running one here and using a public one is put to the user, because
    guessing either way would be wrong: silently downloading a chain surprises
    somebody who wanted a light wallet, and silently trusting a stranger's node
    surprises somebody who wanted their own.

    ``--node auto`` restores the older behaviour of starting a local node without
    asking, for launchers and scripts.

    Returns:
        The connection to use and, if one was started here, the node the caller
        must stop on exit. ``(None, None)`` when the user chose to quit.
    """
    network = args.network
    datadir = Path(args.datadir)
    settings = settings_from_args(args)
    requested = (getattr(args, "node", None) or "").strip()
    keyword = requested.lower() if requested.lower() in ("local", "public", "ask", "auto") else ""

    if requested and not keyword:
        url = directory.normalise_url(requested) or requested
        settings = ConnectionSettings(
            url,
            getattr(args, "rpc_token", None) or "",
            settings.proxy_host,
            settings.proxy_port,
        )
        if settings.url == local_url(network) and not settings.token:
            settings.token = read_rpc_token(datadir, network) or ""

    forced = keyword if keyword in ("local", "public", "ask") else None
    reason = "" if forced else _why_not(settings, network)
    if not forced and not reason:
        return settings, None

    allow_start = not getattr(args, "no_start_node", False)
    if keyword == "auto" and allow_start and is_loopback(settings.url):
        node = start_node_with_progress(None, network=network, datadir=datadir)
        if node is not None:
            chosen = ConnectionSettings(node.url, node.token)
            chosen.save(datadir, network)
            return chosen, node

    return choose_startup_node(
        None,
        network=network,
        datadir=datadir,
        settings=settings,
        reason=reason,
        preference=None if forced in (None, "ask") else forced,
        for_mining=for_mining,
        allow_start=allow_start,
    )
