#!/usr/bin/env python3
"""
Local web dashboard for ff-sessions — manage Camoufox identity profiles
(create / launch / stop / status) from a browser UI.

Run with the project venv: .venv/bin/python dashboard.py
Then open http://127.0.0.1:8787

Binds localhost only. Launch uses the Playwright controller (controller.py):
headed windows under automation control, with live screenshots in the UI.
The dashboard must stay alive for the whole session — quitting it closes
the browser windows. (For detached fire-and-forget windows, use ffid.sh.)
"""

import atexit
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import controller
import ffid_core

# When frozen by PyInstaller, static assets live in the bundle (_MEIPASS).
STATIC = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "static"
HOST = "127.0.0.1"
PORT = 8787

_jobs = []
_jobs_lock = threading.Lock()
_job_seq = 0

_freshness = {}


def _freshness_worker():
    global _freshness
    try:
        _freshness = ffid_core.log_camoufox_freshness()
    except Exception:
        pass


def _start_job(kind, fn, *args):
    """Run fn(*args, log=...) in a background thread; returns (job, error)."""
    global _job_seq
    with _jobs_lock:
        if any(j["status"] == "running" for j in _jobs):
            return None, "another job is already running"
        _job_seq += 1
        job = {
            "id": _job_seq,
            "kind": kind,
            "status": "running",
            "log": [],
            "started_at": time.time(),
        }
        _jobs.append(job)
        del _jobs[:-10]  # keep the last 10

    def log(line):
        with _jobs_lock:
            job["log"].append(str(line))

    def run():
        try:
            fn(*args, log=log)
            job["status"] = "done"
        except Exception as exc:
            log(f"error: {exc}")
            job["status"] = "error"

    threading.Thread(target=run, daemon=True).start()
    return job, None


def _stop_all(log):
    try:
        controller.get_controller().stop(log)
    except RuntimeError as exc:
        log(f"controller: {exc}")
    ffid_core.stop_profiles(log)


def _update_all(log):
    if controller._instance is not None and controller.get_controller().controlled_idents():
        raise RuntimeError("stop all identities before updating camoufox")
    ffid_core.update_camoufox(log)
    global _freshness
    _freshness = ffid_core.log_camoufox_freshness(log)


def _state():
    state = ffid_core.status()
    controlled = set()
    if controller._instance is not None:
        try:
            controlled = set(controller.get_controller().controlled_idents())
        except Exception:
            pass
    for ident in state["identities"]:
        ident["controlled"] = ident["id"] in controlled
        if ident["controlled"]:
            ident["running"] = True
    state["mode"] = "controller" if controlled else "none"
    state["freshness"] = _freshness
    with _jobs_lock:
        state["jobs"] = [
            {k: v for k, v in j.items()} for j in _jobs[-5:]
        ]
    return state


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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

    def do_GET(self):
        if self.path == "/":
            page = STATIC / "index.html"
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._send_json(_state())
        elif self.path.startswith("/api/shot/"):
            ident = self.path[len("/api/shot/"):].split("?", 1)[0]
            try:
                data = controller.get_controller().screenshot(ident)
            except RuntimeError:
                data = None
            if data is None:
                self._send_json({"error": "no screenshot available"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_json()
        if self.path == "/api/create":
            try:
                count = int(body.get("count", 10))
            except (TypeError, ValueError):
                self._send_json({"error": "count must be an integer"}, 400)
                return
            job, err = _start_job("create", ffid_core.create_profiles, count)
        elif self.path == "/api/launch":
            url = body.get("url") or "about:blank"
            job, err = _start_job(
                "launch", lambda u, log: controller.get_controller().launch(u, log), url
            )
        elif self.path == "/api/stop":
            job, err = _start_job("stop", _stop_all)
        elif self.path == "/api/proxies":
            ffid_core.set_proxies_enabled(bool(body.get("enabled", True)))
            self._send_json({"proxies_enabled": ffid_core.proxies_enabled()})
            return
        elif self.path == "/api/update":
            job, err = _start_job("update", _update_all)
        else:
            self._send_json({"error": "not found"}, 404)
            return
        if err:
            self._send_json({"error": err}, 409)
        else:
            self._send_json({"job": job["id"], "kind": job["kind"]})

    def log_message(self, fmt, *args):
        pass  # quiet


def main():
    @atexit.register
    def _cleanup():
        if controller._instance is not None:
            try:
                controller._instance.stop(lambda line: None)
            except Exception:
                pass

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=_freshness_worker, daemon=True).start()
    print(f"ff-sessions dashboard: http://{HOST}:{PORT}  (Ctrl-C to quit)")
    print("note: quitting the dashboard closes all browser windows it launched")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
