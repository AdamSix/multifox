"""
Application state shared by the dashboard HTTP handlers and the desktop
launcher: the Playwright controller (created on first use), the background
job runner, and the Camoufox freshness check.

One App per process. The HTTP server holds it; handlers reach it via
self.server.app.
"""

import sys
import threading
import time

from . import controller, core, paths


class App:
    def __init__(self):
        self._controller = None
        self._controller_lock = threading.Lock()
        self._jobs = []
        self._jobs_lock = threading.Lock()
        self._job_seq = 0
        self.freshness = {}
        self._ax_prompted = False

    # -- controller -----------------------------------------------------------

    def controller(self):
        with self._controller_lock:
            if self._controller is None:
                self._controller = controller.Controller()
            return self._controller

    def controlled_idents(self):
        """Identities under Playwright control, without starting the driver."""
        if self._controller is None:
            return []
        try:
            return self._controller.controlled_idents()
        except Exception:
            return []

    def shutdown(self):
        if self._controller is not None:
            try:
                self._controller.stop(lambda line: None)
            except Exception:
                pass

    def request_accessibility_once(self):
        """macOS: show the Accessibility grant prompt at most once per process."""
        if self._ax_prompted:
            return
        self._ax_prompted = True
        controller.accessibility_trusted(prompt=True)

    # -- background jobs ------------------------------------------------------

    def start_job(self, kind, fn, *args):
        """Run fn(*args, log=..., progress=...) in a background thread; returns (job, error)."""
        with self._jobs_lock:
            if any(j["status"] == "running" for j in self._jobs):
                return None, "another job is already running"
            self._job_seq += 1
            job = {
                "id": self._job_seq,
                "kind": kind,
                "status": "running",
                "log": [],
                "progress": {"message": "starting…", "current": None, "total": None},
                "started_at": time.time(),
            }
            self._jobs.append(job)
            del self._jobs[:-10]  # keep the last 10

        self._append_log(f"\n=== {kind} job {job['id']} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

        def log(line):
            with self._jobs_lock:
                job["log"].append(str(line))
            self._append_log(str(line))

        def progress(message, current=None, total=None):
            with self._jobs_lock:
                job["progress"] = {"message": str(message), "current": current, "total": total}

        def run():
            try:
                fn(*args, log=log, progress=progress)
                with self._jobs_lock:
                    if job["log"]:
                        job["progress"]["message"] = job["log"][-1]
                job["status"] = "done"
            except Exception as exc:
                log(f"error: {exc}")
                job["status"] = "error"

        threading.Thread(target=run, daemon=True).start()
        return job, None

    @staticmethod
    def _append_log(line):
        # Full job logs (the UI shows only progress + warnings) for the "full log" button.
        try:
            with paths.DASHBOARD_LOG.open("a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    # -- operations -----------------------------------------------------------

    def start_sessions(self, count, url, log, progress=None):
        core.create_profiles(count, log, progress=progress)
        self.controller().launch(url, log, progress=progress)

    def reload_sessions(self, url, log, progress=None):
        self.controller().reload(url, log, progress=progress)

    def launch_sessions(self, url, log, progress=None):
        self.controller().launch(url, log, progress=progress)

    def stop_all(self, log, progress=None):
        if progress:
            progress("Closing browser windows…")
        if self._controller is not None:
            try:
                self._controller.stop(log)
            except RuntimeError as exc:
                log(f"controller: {exc}")
        if progress:
            progress("Deleting profiles…")
        core.delete_profiles(log)

    def focus(self, ident):
        """Bring an identity's window to the front; returns a detail string."""
        detail = self.controller().focus(ident)
        if sys.platform == "darwin" and not controller.accessibility_trusted():
            self.request_accessibility_once()
            detail = (
                "warning: window focus needs Accessibility access — approve the "
                "system prompt, or enable it in System Settings → Privacy & "
                "Security → Accessibility, then click the tile again"
            )
        return detail

    def refresh_freshness(self):
        try:
            self.freshness = core.log_camoufox_freshness()
        except Exception:
            pass

    def state(self):
        state = core.status()
        controlled = set(self.controlled_idents())
        for ident in state["identities"]:
            ident["controlled"] = ident["id"] in controlled
        state["freshness"] = self.freshness
        with self._jobs_lock:
            state["jobs"] = [dict(j) for j in self._jobs[-5:]]
        return state
