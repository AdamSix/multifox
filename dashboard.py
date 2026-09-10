#!/usr/bin/env python3
"""
Local web dashboard for ff-sessions — manage Camoufox identity profiles
(create / launch / stop / status) from a browser UI.

Run with the project venv: .venv/bin/python dashboard.py
Then open http://127.0.0.1:8787

Binds localhost only. No dependencies beyond the stdlib plus ffid_core.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ffid_core

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
HOST = "127.0.0.1"
PORT = 8787

_jobs = []
_jobs_lock = threading.Lock()
_job_seq = 0


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


def _state():
    state = ffid_core.status()
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
            job, err = _start_job("launch", ffid_core.launch_profiles, url)
        elif self.path == "/api/stop":
            job, err = _start_job("stop", ffid_core.stop_profiles)
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ff-sessions dashboard: http://{HOST}:{PORT}  (Ctrl-C to quit)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
