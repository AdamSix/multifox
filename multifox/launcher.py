#!/usr/bin/env python3
"""
Desktop launcher for multifox — the packaged app's entry point.

Starts the dashboard server in-process and shows it in a native webview
window (pywebview) — no separate browser needed. A splash page shows startup
progress until the dashboard responds, then the window loads it.

Closing the window stops the server and any controlled browser sessions.

Runs frozen (PyInstaller) or plain (python3 launcher.py). Runtime data
(proxies.conf, profiles/) lives in FFID_HOME: a per-user data dir when frozen,
the project directory otherwise.
"""

import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP_NAME = "multifox"
DISPLAY_NAME = "multifox"


def runtime_dir():
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    return Path.home() / f".{APP_NAME}"


HOME = runtime_dir()
os.environ["FFID_HOME"] = str(HOME)

# Static assets and the proxies.conf template live in the bundle when frozen.
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))

DASHBOARD_URL = "http://multifox.localhost:8787"
LOCAL_URL = "http://127.0.0.1:8787"  # used for the webview itself (no DNS dependency)

SPLASH_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  body { background: #1c1c1e; color: #e5e5ea; margin: 0; padding: 20px;
         font: 13px -apple-system, Helvetica, sans-serif; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  #status { color: #9a9aa0; margin-bottom: 12px; }
  #log { white-space: pre-wrap; color: #8e8e93; font: 11px Menlo, monospace; }
</style></head><body>
<h1>multifox</h1>
<div id="status">starting…</div>
<pre id="log"></pre>
<script>
  function mfoxStatus(t) { document.getElementById("status").textContent = t; }
  function mfoxLog(t) {
    var l = document.getElementById("log");
    l.textContent += t + "\n";
    window.scrollTo(0, document.body.scrollHeight);
  }
</script>
</body></html>"""


class Launcher:
    def __init__(self):
        import webview

        self._webview = webview
        self.window = webview.create_window(
            DISPLAY_NAME, html=SPLASH_HTML, width=1200, height=850, min_size=(720, 500)
        )
        self.server = None

    # -- ui plumbing (worker thread -> webview) --------------------------------

    def _js(self, code):
        try:
            self.window.evaluate_js(code)
        except Exception:
            pass  # window not ready yet, or already closed

    def log_line(self, text):
        try:
            with (HOME / "launcher.log").open("a") as fh:
                fh.write(text + "\n")
        except OSError:
            pass
        self._js(f"mfoxLog({json.dumps(str(text))})")

    def set_status(self, text):
        self._js(f"mfoxStatus({json.dumps(str(text))})")

    # -- startup sequence (worker thread) --------------------------------------

    def _worker(self):
        try:
            self._setup_runtime_dir()
            self._ensure_browser()
            self._update_browser()
            self._start_dashboard()
            self.window.load_url(LOCAL_URL)
            # after load_url so the system prompt lands on top of the window
            # instead of being clobbered by it appearing
            self._request_accessibility()
        except Exception:
            import traceback

            self.log_line(traceback.format_exc().rstrip())
            self.set_status("startup failed — see log")

    def _request_accessibility(self):
        """macOS: window focusing needs Accessibility access; prompt at startup."""
        if sys.platform != "darwin":
            return
        from . import controller

        if controller.accessibility_trusted():
            return
        controller.accessibility_trusted(prompt=True)
        self.log_line(
            "tile-click window focusing needs Accessibility access — approve the "
            "system prompt (or later: System Settings → Privacy & Security → Accessibility)"
        )

    def _setup_runtime_dir(self):
        HOME.mkdir(parents=True, exist_ok=True)
        conf = HOME / "proxies.conf"
        if not conf.exists():
            template = BUNDLE / "proxies.conf"
            if template.exists():
                shutil.copy(template, conf)
            else:
                conf.write_text("DIRECT\n")
            self.log_line(f"created {conf} — edit it to add your proxies")

    def _ensure_browser(self):
        from camoufox.pkgman import CamoufoxNotInstalled, installed_verstr

        try:
            self.log_line(f"camoufox browser {installed_verstr()} installed")
            self._ensure_mmdb()
            return
        except CamoufoxNotInstalled:
            pass

        # preferred path: unpack the browser bundled inside the app (offline)
        payload_zip = BUNDLE / "bundle_payload" / "bundle_payload.zip"
        if payload_zip.is_file():
            import zipfile

            from camoufox.pkgman import INSTALL_DIR

            self.set_status("installing bundled camoufox browser…")
            self.log_line("installing bundled camoufox browser (one-time unpack)…")
            try:
                INSTALL_DIR.mkdir(parents=True, exist_ok=True)
                if shutil.which("ditto"):
                    # preserves permissions exactly (zipfile drops the +x bit)
                    subprocess.run(
                        ["ditto", "-x", "-k", str(payload_zip), str(INSTALL_DIR)], check=True
                    )
                else:
                    with zipfile.ZipFile(payload_zip) as zf:
                        for info in zf.infolist():
                            dest = zf.extract(info, INSTALL_DIR)
                            mode = info.external_attr >> 16
                            if mode:
                                os.chmod(dest, mode)
                self.log_line(f"camoufox browser {installed_verstr()} installed")
                self._ensure_mmdb()
                return
            except Exception as exc:
                self.log_line(f"warning: bundled install failed ({exc}); downloading instead")

        self.set_status("downloading camoufox browser (one-time, ~300MB)…")
        self.log_line("downloading camoufox browser — this only happens once…")
        from camoufox.pkgman import CamoufoxFetcher

        fetcher = CamoufoxFetcher()
        fetcher.fetch_latest()
        fetcher.install()
        self.log_line("browser installed")
        self._ensure_mmdb()

    def _update_browser(self):
        """Unskippable at startup: try to update camoufox; fall back to what's installed."""
        from . import core as ffid_core

        self.set_status("updating camoufox…")
        try:
            ffid_core.update_camoufox(self.log_line)
        except Exception as exc:
            from camoufox.pkgman import CamoufoxNotInstalled, installed_verstr

            try:
                ver = installed_verstr()
            except CamoufoxNotInstalled:
                raise  # nothing installed to fall back on
            self.log_line(
                f"warning: camoufox update failed ({exc}); "
                f"continuing with installed browser {ver}"
            )

    def _ensure_mmdb(self):
        try:
            from camoufox.geolocation import GEOIP_DIR, download_mmdb

            if GEOIP_DIR.exists() and any(GEOIP_DIR.iterdir()):
                return
            download_mmdb()
            self.log_line("GeoIP database installed")
        except Exception as exc:
            self.log_line(f"warning: GeoIP database setup failed: {exc}")

    def _start_dashboard(self):
        from . import dashboard

        self.set_status("starting dashboard…")
        if self._dashboard_responding():
            self.log_line("dashboard already running in another instance")
            return
        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer((dashboard.HOST, dashboard.PORT), dashboard.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=dashboard._freshness_worker, daemon=True).start()
        for _ in range(50):
            if self._dashboard_responding():
                self.log_line(f"dashboard running at {DASHBOARD_URL}")
                return
            time.sleep(0.1)
        raise RuntimeError("dashboard did not start")

    @staticmethod
    def _dashboard_responding():
        try:
            with urllib.request.urlopen(f"{DASHBOARD_URL}/api/state", timeout=1):
                return True
        except Exception:
            return False

    # -- run / shutdown ---------------------------------------------------------

    def run(self):
        threading.Thread(target=self._worker, daemon=True).start()
        self._webview.start()  # blocks until the window is closed
        # window closed — stop sessions and the server
        try:
            from . import controller

            if controller._instance is not None:
                controller._instance.stop(lambda line: None)
        except Exception:
            pass
        if self.server is not None:
            self.server.shutdown()


def main():
    # Frozen only: camoufox's addons use multiprocessing.Lock, whose resource
    # tracker re-executes the app binary; freeze_support() diverts that re-exec
    # to the helper code instead of launching a second full app instance.
    multiprocessing.freeze_support()
    Launcher().run()


if __name__ == "__main__":
    main()
