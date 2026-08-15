"""Shared pieces for the Qt applications: theme, worker threads and dialogs."""

from __future__ import annotations

import argparse
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from scarletcoin.cli_common import DEFAULT_DATADIR, read_rpc_token
from scarletcoin.core.params import get_params, network_names
from scarletcoin.net.client import RpcClient

__all__ = [
    "STYLESHEET",
    "PollWorker",
    "add_common_gui_arguments",
    "apply_theme",
    "client_from_args",
    "monospace",
    "show_error",
]

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


def client_from_args(args: argparse.Namespace) -> RpcClient:
    """Build an RPC client from parsed arguments."""
    url = args.rpc_url or f"http://127.0.0.1:{get_params(args.network).default_rpc_port}"
    token = args.rpc_token or read_rpc_token(args.datadir, args.network)
    return RpcClient(url, token=token, timeout=20.0)


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
