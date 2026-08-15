"""Shared pieces for the Qt applications: theme, worker threads and dialogs."""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from PyQt5 import QtCore, QtGui, QtWidgets

from scarletcoin.cli_common import DEFAULT_DATADIR, read_rpc_token
from scarletcoin.core.params import get_params, network_names
from scarletcoin.net.client import RpcClient, RpcClientError
from scarletcoin.net.launcher import LocalNode, LocalNodeError

__all__ = [
    "STYLESHEET",
    "ConnectionSettings",
    "NodeDialog",
    "PollWorker",
    "add_common_gui_arguments",
    "apply_theme",
    "ask_for_node",
    "client_from_args",
    "is_loopback",
    "monospace",
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
        "--no-start-node",
        action="store_true",
        help="do not start a node automatically when none is running locally",
    )


def default_url(network: str) -> str:
    """The node URL assumed when nothing else is configured."""
    return f"http://127.0.0.1:{get_params(network).default_rpc_port}"


def settings_from_args(args: argparse.Namespace) -> ConnectionSettings:
    """Work out which node to use: command line first, then saved, then localhost.

    Reading the local node's token file means a node running on this machine works
    with no configuration at all.
    """
    saved = ConnectionSettings.load(args.datadir, args.network)
    url = args.rpc_url or (saved.url if saved else default_url(args.network))
    token = args.rpc_token or (saved.token if saved else "")
    if not token and url == default_url(args.network):
        token = read_rpc_token(args.datadir, args.network) or ""
    return ConnectionSettings(url, token)


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

    Saved next to the wallet, in ``<datadir>/<network>/gui.json``, so a URL typed
    once is remembered. Command line options always win over the saved value.
    """

    url: str
    token: str = ""

    @staticmethod
    def path(datadir: Path, network: str) -> Path:
        """Location of the settings file."""
        return Path(datadir) / network / "gui.json"

    @classmethod
    def load(cls, datadir: Path, network: str) -> ConnectionSettings | None:
        """Read saved settings, or ``None`` if there are none."""
        try:
            data = json.loads(cls.path(datadir, network).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        url = str(data.get("rpc_url") or "").strip()
        if not url:
            return None
        return cls(url, str(data.get("rpc_token") or ""))

    def save(self, datadir: Path, network: str) -> None:
        """Store the settings, keeping the file private."""
        target = self.path(datadir, network)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"rpc_url": self.url, "rpc_token": self.token}, indent=1), "utf-8"
            )
            target.chmod(0o600)
        except OSError as exc:  # pragma: no cover - disk errors
            logger.warning("could not save the node settings: %s", exc)

    def client(self, timeout: float = 20.0) -> RpcClient:
        """Build a client from these settings."""
        return RpcClient(self.url, token=self.token or None, timeout=timeout)


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
        self.settings = ConnectionSettings(settings.url, settings.token)

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
        return ConnectionSettings(self.url_edit.text().strip(), self.token_edit.text().strip())

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
) -> LocalNode | None:
    """Start a node, showing progress and keeping the interface responsive.

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
        node = LocalNode.launch(network=network, datadir=datadir, rpc_port=rpc_port)
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
