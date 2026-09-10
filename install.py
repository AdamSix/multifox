#!/usr/bin/env python3
"""
One-time installer for ff-sessions — works on macOS, Linux and Windows.

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
    run([str(py), "-m", "pip", "install", "--upgrade", "camoufox[geoip]"])
    run([str(py), "-m", "camoufox", "set", "official/stable"])
    run([str(py), "-m", "camoufox", "fetch"])
    print()
    print("install complete. Next steps:")
    print("  1. edit proxies.conf — one SOCKS5 host:port per line (see the comments in the file)")
    if sys.platform == "win32":
        print(f"  2. start the dashboard: {py} dashboard.py")
    else:
        print("  2. start the dashboard: ./ffid.sh dashboard")
    print("  3. open http://127.0.0.1:8787")


if __name__ == "__main__":
    main()
