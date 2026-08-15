"""Optional Qt desktop applications for the wallet and the miner.

PyQt5 is an optional dependency; install it with::

    uv sync --extra gui      # or: pip install "scarletcoin[gui]"
"""

__all__ = ["require_qt"]


def require_qt():
    """Import and return the PyQt5 modules, with a helpful error if they are missing.

    Returns:
        The ``(QtCore, QtGui, QtWidgets)`` modules.

    Raises:
        SystemExit: if PyQt5 is not installed.
    """
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SystemExit(
            "the graphical interface needs PyQt5.\n"
            "Install it with:  uv sync --extra gui\n"
            'or:               pip install "scarletcoin[gui]"'
        ) from exc
    return QtCore, QtGui, QtWidgets
