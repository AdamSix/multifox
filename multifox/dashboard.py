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

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import controller
from . import core as ffid_core

# When frozen by PyInstaller, static assets live in the bundle (_MEIPASS).
STATIC = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / (
    "multifox/static" if getattr(sys, "frozen", False) else "static"
)
HOST = "127.0.0.1"
PORT = 8787

# Full job logs (the UI shows only progress + warnings) for the "full log" button.
LOG_FILE = ffid_core.ROOT / "dashboard.log"

_jobs = []
_jobs_lock = threading.Lock()
_job_seq = 0

_freshness = {}
_ax_prompted = False


def _request_accessibility_once():
    """macOS: show the Accessibility grant prompt at most once per process."""
    global _ax_prompted
    if _ax_prompted:
        return
    _ax_prompted = True
    controller.accessibility_trusted(prompt=True)


def _freshness_worker():
    global _freshness
    try:
        _freshness = ffid_core.log_camoufox_freshness()
    except Exception:
        pass


def _start_job(kind, fn, *args):
    """Run fn(*args, log=..., progress=...) in a background thread; returns (job, error)."""
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
            "progress": {"message": "starting…", "current": None, "total": None},
            "started_at": time.time(),
        }
        _jobs.append(job)
        del _jobs[:-10]  # keep the last 10

    try:
        with LOG_FILE.open("a") as fh:
            fh.write(f"\n=== {kind} job {_job_seq} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    except OSError:
        pass

    def log(line):
        with _jobs_lock:
            job["log"].append(str(line))
        try:
            with LOG_FILE.open("a") as fh:
                fh.write(str(line) + "\n")
        except OSError:
            pass

    def progress(message, current=None, total=None):
        with _jobs_lock:
            job["progress"] = {"message": str(message), "current": current, "total": total}

    def run():
        try:
            fn(*args, log=log, progress=progress)
            with _jobs_lock:
                if job["log"]:
                    job["progress"]["message"] = job["log"][-1]
            job["status"] = "done"
        except Exception as exc:
            log(f"error: {exc}")
            job["status"] = "error"

    threading.Thread(target=run, daemon=True).start()
    return job, None


def _stop_all(log, progress=None):
    if progress:
        progress("Closing browser windows…")
    try:
        controller.get_controller().stop(log)
    except RuntimeError as exc:
        log(f"controller: {exc}")
    if progress:
        progress("Deleting profiles…")
    ffid_core.stop_profiles(log)


def _start_sessions(count, url, log, progress=None):
    ffid_core.create_profiles(count, log, progress=progress)
    controller.get_controller().launch(url, log, progress=progress)


def _sessions_running():
    if controller._instance is None:
        return False
    try:
        return bool(controller.get_controller().controlled_idents())
    except Exception:
        return False


def _open_log_file():
    LOG_FILE.touch(exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-t", str(LOG_FILE)])
    elif sys.platform == "win32":
        os.startfile(str(LOG_FILE))  # noqa: S606 - local user action
    else:
        subprocess.Popen(["xdg-open", str(LOG_FILE)])


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
        elif self.path == "/api/proxies/conf":
            self._send_json({"text": ffid_core.proxy_conf_text()})
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
        if self.path == "/api/start":
            try:
                count = int(body.get("count", 10))
            except (TypeError, ValueError):
                self._send_json({"error": "count must be an integer"}, 400)
                return
            url = body.get("url") or "about:blank"
            if _sessions_running():
                job, err = _start_job(
                    "reload",
                    lambda u, log, progress: controller.get_controller().reload(u, log, progress),
                    url,
                )
            else:
                job, err = _start_job("start", _start_sessions, count, url)
        elif self.path == "/api/create":
            try:
                count = int(body.get("count", 10))
            except (TypeError, ValueError):
                self._send_json({"error": "count must be an integer"}, 400)
                return
            job, err = _start_job("create", ffid_core.create_profiles, count)
        elif self.path == "/api/launch":
            url = body.get("url") or "about:blank"
            job, err = _start_job(
                "launch",
                lambda u, log, progress: controller.get_controller().launch(u, log, progress),
                url,
            )
        elif self.path == "/api/stop":
            job, err = _start_job("stop", _stop_all)
        elif self.path == "/api/focus":
            ident = body.get("id")
            if controller._instance is None:
                self._send_json({"error": "no running sessions"}, 404)
                return
            try:
                detail = controller.get_controller().focus(ident)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, 404)
                return
            if sys.platform == "darwin" and not controller.accessibility_trusted():
                _request_accessibility_once()
                detail = (
                    "warning: window focus needs Accessibility access — approve the "
                    "system prompt, or enable it in System Settings → Privacy & "
                    "Security → Accessibility, then click the tile again"
                )
            self._send_json({"ok": True, "detail": detail})
            return
        elif self.path == "/api/proxies":
            ffid_core.set_proxies_enabled(bool(body.get("enabled", True)))
            self._send_json({"proxies_enabled": ffid_core.proxies_enabled()})
            return
        elif self.path == "/api/proxies/conf":
            text = body.get("text")
            if not isinstance(text, str):
                self._send_json({"error": "text must be a string"}, 400)
                return
            ffid_core.write_proxy_conf(text)
            self._send_json({"ok": True})
            return
        elif self.path == "/api/open-log":
            try:
                _open_log_file()
            except OSError as exc:
                self._send_json({"error": f"could not open log: {exc}"}, 500)
                return
            self._send_json({"ok": True})
            return
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
    print(f"multifox dashboard: http://multifox.localhost:{PORT}  (Ctrl-C to quit)")
    print("note: quitting the dashboard closes all browser windows it launched")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
