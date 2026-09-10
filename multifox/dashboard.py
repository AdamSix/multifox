#!/usr/bin/env python3
"""
Local web dashboard for multifox — manage Camoufox identity profiles
(create / launch / stop / status) from a browser UI.

Run with the project venv: .venv/bin/python dashboard.py
Then open http://multifox.localhost:8787

Binds localhost only. Launch uses the Playwright controller (controller.py):
headed windows under automation control, with live screenshots in the UI.
The dashboard must stay alive for the whole session — quitting it closes
the browser windows.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core, paths
from .app import App

HOST = "127.0.0.1"
PORT = 8787
LOCAL_URL = f"http://{HOST}:{PORT}"
DASHBOARD_URL = f"http://multifox.localhost:{PORT}"


def open_log_file():
    paths.DASHBOARD_LOG.touch(exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-t", str(paths.DASHBOARD_LOG)])
    elif sys.platform == "win32":
        os.startfile(str(paths.DASHBOARD_LOG))  # noqa: S606 - local user action
    else:
        subprocess.Popen(["xdg-open", str(paths.DASHBOARD_LOG)])


class Handler(BaseHTTPRequestHandler):
    @property
    def app(self):
        return self.server.app

    def _send_json(self, obj, code=200):
        self._send_bytes(json.dumps(obj).encode(), "application/json", code)

    def _send_bytes(self, body, content_type, code=200, cache=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def _read_count(self, body):
        try:
            return int(body.get("count", 10))
        except (TypeError, ValueError):
            return None

    def do_GET(self):
        if self.path == "/":
            self._send_bytes((paths.STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send_json(self.app.state())
        elif self.path == "/api/proxies/conf":
            self._send_json({"text": core.proxy_conf_text()})
        elif self.path.startswith("/api/shot/"):
            ident = self.path[len("/api/shot/"):].split("?", 1)[0]
            data = None
            if ident in self.app.controlled_idents():
                try:
                    data = self.app.controller().screenshot(ident)
                except RuntimeError:
                    data = None
            if data is None:
                self._send_json({"error": "no screenshot available"}, 404)
            else:
                self._send_bytes(data, "image/jpeg", cache="no-store")
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_json()
        app = self.app
        if self.path == "/api/start":
            count = self._read_count(body)
            if count is None:
                self._send_json({"error": "count must be an integer"}, 400)
                return
            url = body.get("url") or "about:blank"
            if app.controlled_idents():
                self._send_job(app.start_job("reload", app.reload_sessions, url))
            else:
                self._send_job(app.start_job("start", app.start_sessions, count, url))
        elif self.path == "/api/create":
            count = self._read_count(body)
            if count is None:
                self._send_json({"error": "count must be an integer"}, 400)
                return
            self._send_job(app.start_job("create", core.create_profiles, count))
        elif self.path == "/api/launch":
            url = body.get("url") or "about:blank"
            self._send_job(app.start_job("launch", app.launch_sessions, url))
        elif self.path == "/api/stop":
            self._send_job(app.start_job("stop", app.stop_all))
        elif self.path == "/api/focus":
            ident = body.get("id")
            if ident not in app.controlled_idents():
                self._send_json({"error": f"{ident} is not running — no window to focus"}, 404)
                return
            try:
                detail = app.focus(ident)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, 404)
                return
            self._send_json({"ok": True, "detail": detail})
        elif self.path == "/api/proxies":
            core.set_proxies_enabled(bool(body.get("enabled", True)))
            self._send_json({"proxies_enabled": core.proxies_enabled()})
        elif self.path == "/api/proxies/conf":
            text = body.get("text")
            if not isinstance(text, str):
                self._send_json({"error": "text must be a string"}, 400)
                return
            core.write_proxy_conf(text)
            self._send_json({"ok": True})
        elif self.path == "/api/open-log":
            try:
                open_log_file()
            except OSError as exc:
                self._send_json({"error": f"could not open log: {exc}"}, 500)
                return
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def _send_job(self, started):
        job, err = started
        if err:
            self._send_json({"error": err}, 409)
        else:
            self._send_json({"job": job["id"], "kind": job["kind"]})

    def log_message(self, fmt, *args):
        pass  # quiet


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, app):
        super().__init__((HOST, PORT), Handler)
        self.app = app


def start_server(app):
    """Bind the dashboard, serve it on a daemon thread, and kick off the freshness check."""
    server = DashboardServer(app)
    threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True).start()
    threading.Thread(target=app.refresh_freshness, name="camoufox-freshness", daemon=True).start()
    return server


def main():
    app = App()
    server = start_server(app)
    print(f"multifox dashboard: {DASHBOARD_URL}  (Ctrl-C to quit)")
    print("note: quitting the dashboard closes all browser windows it launched")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
