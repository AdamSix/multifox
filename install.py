#!/usr/bin/env python3
"""
One-time installer for multifox — works on macOS, Linux and Windows.

  python3 install.py      (Windows: py install.py)

Creates .venv, installs the camoufox python package (with GeoIP support),
and downloads the Camoufox browser (~600MB). Safe to re-run: it just upgrades.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def venv_python():
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(cmd):
    print(f"$ {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def main():
    if sys.version_info < (3, 10):
        sys.exit(f"error: Python 3.10+ required (you have {sys.version.split()[0]})")
    py = venv_python()
    if not py.exists():
        print("creating virtualenv in .venv ...")
        run([sys.executable, "-m", "venv", str(VENV)])
    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(py), "-m", "pip", "install", "--upgrade", "camoufox[geoip]", "pywebview"])
    run([str(py), "-m", "camoufox", "set", "official/stable"])
    run([str(py), "-m", "camoufox", "fetch"])
    # camoufox fetch exits 0 even when it synced nothing (e.g. GitHub API rate
    # limit) — verify the browser actually landed
    check = subprocess.run(
        [str(py), "-c", "from camoufox.pkgman import installed_verstr; installed_verstr()"],
        capture_output=True,
    )
    if check.returncode:
        sys.exit("error: camoufox browser did not install (see fetch output above)")
    print()
    print("install complete. Next steps:")
    print("  1. edit proxies.conf — one SOCKS5 host:port per line (see the comments in the file)")
    print(f"  2. start the dashboard: {py} dashboard.py")
    print("  3. open http://multifox.localhost:8787")


if __name__ == "__main__":
    main()
