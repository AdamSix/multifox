"""
Filesystem locations for multifox.

Runtime data (proxies.conf, settings.json, profiles/, logs) lives in HOME: a
per-user data dir when frozen by PyInstaller, the project directory otherwise.
Bundled read-only assets (static/, the proxies.conf template, the browser
payload) live in BUNDLE.
"""

import os
import sys
from pathlib import Path

APP_NAME = "multifox"

FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_DIR = Path(__file__).resolve().parent.parent


def _default_home():
    if not FROZEN:
        return PROJECT_DIR
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    return Path.home() / f".{APP_NAME}"


HOME = _default_home()
SETTINGS = HOME / "settings.json"
PROXY_CONF = HOME / "proxies.conf"
PROFILES = HOME / "profiles"
DASHBOARD_LOG = HOME / "dashboard.log"
LAUNCHER_LOG = HOME / "launcher.log"

BUNDLE = Path(getattr(sys, "_MEIPASS", PROJECT_DIR))
STATIC = BUNDLE / "multifox" / "static"
PROXY_CONF_TEMPLATE = BUNDLE / "proxies.conf"
BROWSER_PAYLOAD = BUNDLE / "bundle_payload" / "bundle_payload.zip"
