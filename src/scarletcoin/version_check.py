"""Check PyPI for a newer release, with a daily cache.

Every tool that talks to the outside world calls :func:`check_version` once at
start-up.  The answer is cached per datadir so a node that runs for months does
not hammer PyPI on every restart; the check expires after 24 hours.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from scarletcoin import __version__

logger = logging.getLogger(__name__)

#: How long a cached answer is reused before PyPI is asked again.
_CACHE_SECONDS = 86400  # 24 hours

#: PyPI JSON endpoint for the scarletcoin package.
_PYPI_URL = "https://pypi.org/pypi/scarletcoin/json"

#: How long we wait for PyPI to answer (seconds).
_TIMEOUT = 5.0


def _cache_path(datadir: str | Path) -> Path:
    return Path(datadir) / "version_check.json"


def _load_cache(datadir: str | Path) -> tuple[str | None, float]:
    """Return ``(latest_version, checked_at)`` from the cache, or ``(None, 0)``."""
    path = _cache_path(datadir)
    try:
        data = json.loads(path.read_text("utf-8"))
        return str(data.get("version") or ""), float(data.get("checked_at") or 0)
    except (OSError, ValueError, KeyError):
        return None, 0.0


def _save_cache(datadir: str | Path, version: str) -> None:
    path = _cache_path(datadir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": version, "checked_at": time.time()}, indent=1),
            "utf-8",
        )
    except OSError:
        return


def _parse_version(raw: str) -> tuple[int, ...]:
    """Parse a PEP 440 version into a comparable tuple."""
    # Strip any local or pre-release suffix for comparison purposes.
    cleaned = raw.split("+")[0].split("-")[0]
    try:
        return tuple(int(part) for part in cleaned.split("."))
    except ValueError:
        return ()


def _fetch_latest() -> str | None:
    """Return the latest version from PyPI, or ``None`` on any failure."""
    try:
        import urllib.request

        req = urllib.request.Request(_PYPI_URL, headers={"User-Agent": "scarletcoin-version-check"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return str(data.get("info", {}).get("version") or "")
    except Exception:
        logger.debug("cannot reach PyPI to check for a newer version", exc_info=True)
        return None


def check_version(datadir: str | Path = ".") -> str | None:
    """Return the latest available version if it is newer than the running one.

    The answer is cached for 24 hours, so this is cheap enough to call on every
    start-up.  Returns ``None`` when the running version is the latest, the check
    is suppressed by the environment, or PyPI cannot be reached.

    Set the environment variable ``SCARLETCOIN_NO_VERSION_CHECK=1`` to skip the
    check entirely (useful in air-gapped environments).
    """
    if os.environ.get("SCARLETCOIN_NO_VERSION_CHECK"):
        return None
    cached_version, cached_at = _load_cache(datadir)
    if cached_version and time.time() - cached_at < _CACHE_SECONDS:
        latest = cached_version
    else:
        latest = _fetch_latest()
        if latest is None:
            # PyPI is unreachable; re-use the cached version if we have one.
            latest = cached_version
            if not latest:
                return None
        else:
            _save_cache(datadir, latest)
    running = _parse_version(__version__)
    available = _parse_version(latest)
    if available > running:
        return latest
    return None