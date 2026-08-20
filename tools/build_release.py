"""Build the standalone desktop release for Windows, Linux and macOS.

Runs PyInstaller once per program (wallet GUI, miner GUI, node), drops the three
executables into ``release/bundle`` and packages that folder into an archive.
The bundle needs no Python to run: the wallet and the miner start the node in
the background, and the node sits next to them so
:func:`scarletcoin.net.launcher.node_command` finds it.

Usage::

    python tools/build_release.py

Writes ``release/ScarletCoin-<version>-<platform>.zip`` on Windows and
``release/ScarletCoin-<version>-<platform>.tar.gz`` on Linux and macOS.  The
Windows installer is compiled separately with Inno Setup (see
``packaging/windows``).
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
RELEASE = ROOT / "release"
BUNDLE = RELEASE / "bundle"
WORK = ROOT / "build" / "pyinstaller"
VENV = ROOT / ".venv-release"

#: (executable name, entry-point source file, keep the console window).
APPS = [
    ("scarlet-wallet-gui", "src/scarletcoin/gui/wallet_app.py", False),
    ("scarlet-miner-gui", "src/scarletcoin/gui/miner_app.py", False),
    ("scarlet-node", "src/scarletcoin/net/cli.py", True),
]

#: Package data files PyInstaller would otherwise not bundle.  Each entry is a
#: (source path, destination inside the frozen bundle).
DATA_FILES = [
    ("src/scarletcoin/crypto/wordlist", "scarletcoin/crypto/wordlist"),
    ("src/scarletcoin/miner/_scan_nonces.c", "scarletcoin/miner"),
]


def run(argv: list[str]) -> None:
    """Print and run a command, failing the build if it does."""
    print(f"+ {' '.join(argv)}")
    subprocess.run(argv, check=True)


def project_version() -> str:
    """The version from ``src/scarletcoin/__init__.py``."""
    text = (SRC / "scarletcoin" / "__init__.py").read_text("utf-8")
    match = re.search(r'__version__ = "([^"]+)"', text)
    if match is None:
        raise SystemExit("could not find __version__ in src/scarletcoin/__init__.py")
    return match.group(1)


def platform_tag() -> str:
    """A short tag naming the platform, used in the archive name."""
    if os.name == "nt":
        return "win64"
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine
    )
    system = "macos" if sys.platform == "darwin" else "linux"
    return f"{system}-{arch}"


def venv_python() -> Path:
    """The Python interpreter inside the build environment."""
    return VENV / "Scripts" / "python.exe" if os.name == "nt" else VENV / "bin" / "python"


def ensure_venv() -> Path:
    """Create a build environment and install the project plus PyInstaller."""
    if not (VENV / "pyvenv.cfg").exists():
        print(f"creating a build environment at {VENV}")
        run([sys.executable, "-m", "venv", str(VENV)])
    python = venv_python()
    run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(python), "-m", "pip", "install", "-e", ".[gui]", "pyinstaller"])
    return python


def _find_cryptography_dylibs(python: Path) -> list[tuple[str, str]]:
    """macOS only -- locate OpenSSL dylibs shipped inside the ``cryptography``
    wheel and return ``(absolute_source, bundle_relative_dest)`` pairs for
    ``--add-binary``.  This guarantees the built bundle carries the same
    ``libssl`` / ``libcrypto`` versions that the pre-compiled ``_rust.abi3.so``
    was linked against."""

    if sys.platform != "darwin":
        return []

    script = textwrap.dedent("""\
        import cryptography, os, site
        crypto_dir = os.path.dirname(cryptography.__file__)
        dylibs = []
        for root, _dirs, files in os.walk(crypto_dir):
            for name in sorted(files):
                if name.endswith('.dylib'):
                    dylibs.append(os.path.join(root, name))
        if dylibs:
            print('\\n'.join(sorted(dylibs)))
    """)
    try:
        proc = subprocess.run(
            [str(python), "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"warning: could not find cryptography dylibs ({exc})")
        return []

    sitelib = _venv_site_packages(python)
    libs: list[tuple[str, str]] = []
    for line in proc.stdout.strip().splitlines():
        lib_path = Path(line.strip())
        try:
            relative = lib_path.relative_to(sitelib)
        except ValueError:
            print(f"  warning: dylib {lib_path} is outside site-packages, skipping")
            continue
        libs.append((str(lib_path), str(relative)))
        print(f"  cryptography dylib: {relative}")
    return libs


def _venv_site_packages(python: Path) -> Path:
    """Return the absolute path of the venv's ``site-packages`` directory."""
    proc = subprocess.run(
        [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(proc.stdout.strip())


def build_apps(python: Path) -> None:
    """Freeze each program with PyInstaller into the shared bundle directory."""
    if BUNDLE.exists():
        shutil.rmtree(BUNDLE)
    if WORK.exists():
        shutil.rmtree(WORK)
    BUNDLE.mkdir(parents=True, exist_ok=True)

    crypto_libs = _find_cryptography_dylibs(python)

    for name, entry, console in APPS:
        command = [
            str(python),
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            name,
            "--distpath",
            str(BUNDLE),
            "--workpath",
            str(WORK),
            "--specpath",
            str(WORK),
            "--paths",
            str(SRC),
            "--collect-binaries",
            "cryptography",
        ]
        if not console:
            command.append("--noconsole")
        for source, destination in DATA_FILES:
            command.extend(["--add-data", f"{ROOT / source}{os.pathsep}{destination}"])
        for lib_path, bundle_dest in crypto_libs:
            command.extend(["--add-binary", f"{lib_path}{os.pathsep}{bundle_dest}"])
            print(f"  bundling cryptography library: {lib_path}")
        command.append(str(ROOT / entry))
        run(command)


def package(version: str, tag: str) -> Path:
    """Zip or tar the executables into the archive users download."""
    files = sorted(path for path in BUNDLE.iterdir() if path.is_file())
    if not files:
        raise SystemExit("nothing to package: the bundle is empty")
    if os.name == "nt":
        target = RELEASE / f"ScarletCoin-{version}-{tag}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
    else:
        target = RELEASE / f"ScarletCoin-{version}-{tag}.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            for path in files:
                archive.add(path, arcname=path.name)
    size = target.stat().st_size
    print(f"wrote {target} ({size / (1024 * 1024):.1f} MiB)")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="do not reinstall dependencies into the build environment",
    )
    args = parser.parse_args()

    python = venv_python()
    if not (VENV / "pyvenv.cfg").exists() or not python.exists() or not args.skip_install:
        python = ensure_venv()

    version = project_version()
    tag = platform_tag()
    print(f"building ScarletCoin {version} for {tag}")
    build_apps(python)
    package(version, tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
