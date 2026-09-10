#!/usr/bin/env python3
"""
Desktop launcher for multifox — the packaged app's entry point.

Shows a small native window that:
  1. on first run, downloads the Camoufox browser + GeoIP database
  2. starts the dashboard server (in-process, on http://multifox.localhost:8787)
  3. presents a button to open the dashboard in the default browser

Quitting the window stops the server and any controlled browser sessions.

Runs frozen (PyInstaller) or plain (python3 launcher.py). Runtime data
(proxies.conf, profiles/) lives in FFID_HOME: a per-user data dir when frozen,
the project directory otherwise.
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "multifox"
DISPLAY_NAME = "multifox"


def runtime_dir():
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    return Path.home() / f".{APP_NAME}"


HOME = runtime_dir()
os.environ["FFID_HOME"] = str(HOME)

# Static assets and the proxies.conf template live in the bundle when frozen.
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

DASHBOARD_URL = "http://multifox.localhost:8787"


class Launcher:
    def __init__(self):
        import tkinter as tk

        self._tk = tk
        self.root = tk.Tk()
        self.root.title(DISPLAY_NAME)
        self.root.resizable(False, False)

        self.status = tk.StringVar(value="starting…")
        self._ui = queue.Queue()  # ("log"|"status"|"ready", text)

        frame = tk.Frame(self.root, padx=16, pady=16)
        frame.pack()

        tk.Label(frame, text=DISPLAY_NAME, font=("", 16, "bold")).pack(anchor="w")
        tk.Label(frame, textvariable=self.status).pack(anchor="w", pady=(2, 8))

        self.log = tk.Text(frame, width=72, height=14, state="disabled")
        self.log.pack()

        buttons = tk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        self.open_btn = tk.Button(
            buttons, text="Open dashboard", state="disabled",
            command=lambda: webbrowser.open(DASHBOARD_URL),
        )
        self.open_btn.pack(side="left")
        tk.Button(buttons, text="Quit", command=self.on_quit).pack(side="right")

        self.server = None
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.root.after(100, self._poll_ui)
        threading.Thread(target=self._worker, daemon=True).start()

    # -- ui plumbing (worker thread -> tk thread) -----------------------------

    def _poll_ui(self):
        try:
            while True:
                kind, text = self._ui.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", text + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self.status.set(text)
                elif kind == "ready":
                    self.status.set(f"dashboard running at {DASHBOARD_URL}")
                    self.open_btn.configure(state="normal")
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_ui)

    def log_line(self, text):
        try:
            with (HOME / "launcher.log").open("a") as fh:
                fh.write(text + "\n")
        except OSError:
            pass
        self._ui.put(("log", text))

    def set_status(self, text):
        self._ui.put(("status", text))

    # -- startup sequence (worker thread) --------------------------------------

    def _worker(self):
        try:
            self._setup_runtime_dir()
            self._ensure_browser()
            self._update_browser()
            self._start_dashboard()
            self._ui.put(("ready", ""))
        except Exception:
            import traceback

            self.log_line(traceback.format_exc().rstrip())
            self.set_status("startup failed — see log")

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
        import ffid_core

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
        import dashboard

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

    # -- shutdown ---------------------------------------------------------------

    def on_quit(self):
        self.set_status("shutting down…")
        try:
            import controller

            if controller._instance is not None:
                controller._instance.stop(lambda line: None)
        except Exception:
            pass
        if self.server is not None:
            self.server.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    Launcher().run()


if __name__ == "__main__":
    main()
