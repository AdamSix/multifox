#!/usr/bin/env python3
"""
Desktop launcher for multifox — the packaged app's entry point.

Starts the dashboard server in-process and shows it in a native webview
window (pywebview) — no separate browser needed. A splash page shows startup
progress until the dashboard responds, then the window loads it.

Closing the window stops the server and any controlled browser sessions.

Runs frozen (PyInstaller) or plain (python3 launcher.py). Runtime data lives
in paths.HOME: a per-user data dir when frozen, the project directory otherwise.
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

from . import controller, core, dashboard, paths
from .app import App

DISPLAY_NAME = "multifox"

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
        self.app = App()
        self.server = None

    # -- ui plumbing (worker thread -> webview) --------------------------------

    def _js(self, code):
        try:
            self.window.evaluate_js(code)
        except Exception:
            pass  # window not ready yet, or already closed

    def log_line(self, text):
        try:
            with paths.LAUNCHER_LOG.open("a") as fh:
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
            self.window.load_url(dashboard.LOCAL_URL)
            # after load_url so the system prompt lands on top of the window
            # instead of being clobbered by it appearing
            self._request_accessibility()
        except Exception:
            import traceback

            self.log_line(traceback.format_exc().rstrip())
            self.set_status("startup failed — see log")

    def _request_accessibility(self):
        """macOS: window focusing needs Accessibility access; prompt at startup."""
        if sys.platform != "darwin" or controller.accessibility_trusted():
            return
        self.app.request_accessibility_once()
        self.log_line(
            "tile-click window focusing needs Accessibility access — approve the "
            "system prompt (or later: System Settings → Privacy & Security → Accessibility)"
        )

    def _setup_runtime_dir(self):
        paths.HOME.mkdir(parents=True, exist_ok=True)
        conf = paths.PROXY_CONF
        if not conf.exists():
            if paths.PROXY_CONF_TEMPLATE.exists():
                shutil.copy(paths.PROXY_CONF_TEMPLATE, conf)
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
        if paths.BROWSER_PAYLOAD.is_file():
            self.set_status("installing bundled camoufox browser…")
            self.log_line("installing bundled camoufox browser (one-time unpack)…")
            try:
                self._unpack_bundled_browser()
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

    @staticmethod
    def _unpack_bundled_browser():
        import zipfile

        from camoufox.pkgman import INSTALL_DIR

        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        if shutil.which("ditto"):
            # preserves permissions exactly (zipfile drops the +x bit)
            subprocess.run(
                ["ditto", "-x", "-k", str(paths.BROWSER_PAYLOAD), str(INSTALL_DIR)], check=True
            )
            return
        with zipfile.ZipFile(paths.BROWSER_PAYLOAD) as zf:
            for info in zf.infolist():
                dest = zf.extract(info, INSTALL_DIR)
                mode = info.external_attr >> 16
                if mode:
                    os.chmod(dest, mode)

    def _update_browser(self):
        """Unskippable at startup: try to update camoufox; fall back to what's installed."""
        self.set_status("updating camoufox…")
        try:
            core.update_camoufox(self.log_line)
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
        self.set_status("starting dashboard…")
        if self._dashboard_responding():
            self.log_line("dashboard already running in another instance")
            return
        self.server = dashboard.start_server(self.app)
        for _ in range(50):
            if self._dashboard_responding():
                self.log_line(f"dashboard running at {dashboard.DASHBOARD_URL}")
                return
            time.sleep(0.1)
        raise RuntimeError("dashboard did not start")

    @staticmethod
    def _dashboard_responding():
        try:
            with urllib.request.urlopen(f"{dashboard.LOCAL_URL}/api/state", timeout=1):
                return True
        except Exception:
            return False

    # -- run / shutdown ---------------------------------------------------------

    def run(self):
        threading.Thread(target=self._worker, daemon=True).start()
        self._webview.start()  # blocks until the window is closed
        self.app.shutdown()
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
