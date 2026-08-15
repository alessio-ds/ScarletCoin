"""Starting a node as a child process.

The desktop applications use this so a user never has to open a terminal: if
nothing answers on the local RPC port, the wallet (or the miner) starts a node
itself, waits for it to come up, and stops it again on exit.

Nothing here is Qt-specific — :func:`start_local_node` is usable from a script or
a test just as well.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scarletcoin.cli_common import write_rpc_token
from scarletcoin.core.params import get_params
from scarletcoin.net.client import RpcClient, RpcClientError

__all__ = [
    "LocalNode",
    "LocalNodeError",
    "generate_token",
    "node_command",
    "start_local_node",
]

#: How long to wait for a freshly started node to answer, in seconds.
STARTUP_TIMEOUT = 60.0


def generate_token() -> str:
    """A fresh RPC token that is safe on a command line.

    ``secrets.token_urlsafe`` may begin with ``-``, which argparse would read as
    another option rather than as the value of ``--rpc-token``; a leading letter
    makes that impossible.
    """
    return "t" + secrets.token_urlsafe(32)


class LocalNodeError(RuntimeError):
    """Raised when a node could not be started."""


def _port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something already listens on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return True
        return False


def node_command(
    *,
    network: str,
    datadir: Path,
    rpc_port: int,
    rpc_token: str,
    p2p_port: int | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    """Build the command line that starts a node.

    The console script next to the running interpreter is preferred, because it
    shows up in process listings under its own name; ``python -m`` is the
    fallback for environments where scripts were not installed.
    """
    launcher: list[str]
    directory = Path(sys.executable).parent
    for name in ("scarlet-node", "scarlet-node.exe"):
        candidate = directory / name
        if candidate.exists():
            launcher = [str(candidate)]
            break
    else:
        launcher = [sys.executable, "-m", "scarletcoin.net.cli"]

    command = [
        *launcher,
        "run",
        "--network",
        network,
        "--datadir",
        str(datadir),
        "--rpc-host",
        "127.0.0.1",
        "--rpc-port",
        str(rpc_port),
        "--rpc-token",
        rpc_token,
    ]
    if p2p_port is not None:
        command += ["--p2p-port", str(p2p_port)]
    command += list(extra)
    return command


@dataclass
class LocalNode:
    """A node process owned by this application."""

    process: subprocess.Popen
    url: str
    token: str
    log_path: Path
    command: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ starting

    @classmethod
    def launch(
        cls,
        *,
        network: str,
        datadir: Path,
        rpc_port: int | None = None,
        p2p_port: int | None = None,
        extra: Sequence[str] = (),
    ) -> LocalNode:
        """Start a node and return immediately, before it is ready.

        Raises:
            LocalNodeError: if the process could not be spawned at all.
        """
        params = get_params(network)
        port = rpc_port or params.default_rpc_port
        if _port_busy(port):
            raise LocalNodeError(
                f"port {port} is already in use on this machine. A node is probably "
                "already running here (your wallet may have started one). Stop that "
                "node first, or connect to it instead of starting another."
            )
        token = generate_token()
        datadir = Path(datadir)
        log_path = datadir / network / "node.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        command = node_command(
            network=network,
            datadir=datadir,
            rpc_port=port,
            rpc_token=token,
            p2p_port=p2p_port,
            extra=extra,
        )
        try:
            handle = log_path.open("a", encoding="utf-8")
            handle.write(f"\n--- started by {Path(sys.argv[0]).name} ---\n")
            handle.flush()
            popen_kwargs: dict[str, object] = {}
            if os.name == "nt":
                # The node is a console program; without this flag Windows pops a
                # console window every time the wallet or the miner starts it.
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(
                command, stdout=handle, stderr=subprocess.STDOUT, **popen_kwargs
            )
        except OSError as exc:
            raise LocalNodeError(f"could not start a node: {exc}") from exc

        # Other tools on this machine read the token from here.
        write_rpc_token(datadir, network, token)
        return cls(process, f"http://127.0.0.1:{port}", token, log_path, command)

    # ------------------------------------------------------------------- status

    @property
    def running(self) -> bool:
        """``True`` while the process is alive."""
        return self.process.poll() is None

    def client(self, timeout: float = 5.0) -> RpcClient:
        """A client pointed at this node."""
        return RpcClient(self.url, token=self.token, timeout=timeout)

    def is_ready(self) -> bool:
        """``True`` once the node answers RPC calls."""
        try:
            self.client(timeout=3.0).getinfo()
        except RpcClientError:
            return False
        return True

    def tail_log(self, lines: int = 15) -> str:
        """Return the end of the node's log, for error messages."""
        try:
            text = self.log_path.read_text("utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable log
            return ""
        return "\n".join(text.strip().splitlines()[-lines:])

    def wait_until_ready(
        self,
        timeout: float = STARTUP_TIMEOUT,
        *,
        poll: float = 0.25,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bool:
        """Block until the node answers.

        Args:
            timeout: How long to wait.
            poll: Delay between attempts.
            should_cancel: Called between attempts; return ``True`` to give up.

        Returns:
            ``True`` if the node is ready.

        Raises:
            LocalNodeError: if the process died while starting.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                return False
            if not self.running:
                raise LocalNodeError("the node stopped while starting up:\n\n" + self.tail_log())
            if self.is_ready():
                return True
            time.sleep(poll)
        return False

    # ------------------------------------------------------------------ stopping

    def stop(self, timeout: float = 15.0) -> None:
        """Ask the node to shut down, then make sure it did."""
        if not self.running:
            return
        try:
            self.client(timeout=5.0).call("stop")
        except RpcClientError:
            self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn process
            self.process.kill()
            self.process.wait(timeout=5.0)


def already_running(url: str, token: str | None = None, *, timeout: float = 3.0) -> bool:
    """Return ``True`` if a node already answers at ``url``."""
    try:
        RpcClient(url, token=token, timeout=timeout).getinfo()
    except RpcClientError:
        return False
    return True


def start_local_node(
    *,
    network: str,
    datadir: Path,
    rpc_port: int | None = None,
    p2p_port: int | None = None,
    extra: Sequence[str] = (),
    timeout: float = STARTUP_TIMEOUT,
) -> LocalNode:
    """Start a node and wait for it to be usable.

    Raises:
        LocalNodeError: if it does not come up in time.
    """
    node = LocalNode.launch(
        network=network, datadir=datadir, rpc_port=rpc_port, p2p_port=p2p_port, extra=extra
    )
    if not node.wait_until_ready(timeout):
        node.stop()
        raise LocalNodeError(f"the node did not answer within {timeout:.0f}s:\n\n{node.tail_log()}")
    return node
