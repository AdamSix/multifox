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
        self._threads = {}  # job id -> thread, so Stop can wait for a job it cancelled
        self.freshness = {}
        self._ax_prompted = False
        self.on_job_running = None  # callable(bool), set by the launcher
        self.cancel = threading.Event()  # set by Stop; long loops exit at their next check

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

    def health(self):
        """Per-identity liveness, without starting the driver."""
        if self._controller is None:
            return {}
        try:
            return self._controller.health()
        except Exception:
            return {}

    def shutdown(self):
        self.cancel.set()  # a launch in progress winds down instead of holding the quit
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

    def start_job(self, kind, fn, *args, preempt=False):
        """Run fn(*args, log=..., progress=...) in a background thread; returns (job, error).

        One job at a time, except a preempting one (Stop): it may start while
        another runs, sets `cancel` so that job winds down, and is expected to
        call `wait_for_other_jobs` before touching what the other job builds.
        """
        with self._jobs_lock:
            running = [j for j in self._jobs if j["status"] == "running"]
            if running and (not preempt or any(j["preempt"] for j in running)):
                return None, "another job is already running"
            if preempt:
                self.cancel.set()
            else:
                self.cancel.clear()
            self._job_seq += 1
            job = {
                "id": self._job_seq,
                "kind": kind,
                "status": "running",
                "log": [],
                "progress": {"message": "starting…", "current": None, "total": None},
                "started_at": time.time(),
                "preempt": preempt,
            }
            self._jobs.append(job)
            del self._jobs[:-10]  # keep the last 10
            kept = {j["id"] for j in self._jobs}
            self._threads = {i: t for i, t in self._threads.items() if i in kept}

        self._append_log(f"\n=== {kind} job {job['id']} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

        def log(line):
            with self._jobs_lock:
                job["log"].append(str(line))
            self._append_log(str(line))

        def progress(message, current=None, total=None):
            with self._jobs_lock:
                job["progress"] = {"message": str(message), "current": current, "total": total}

        def run():
            self._notify_job_running(True)
            try:
                fn(*args, log=log, progress=progress)
                with self._jobs_lock:
                    if job["log"]:
                        job["progress"]["message"] = job["log"][-1]
                job["status"] = "done"
            except Exception as exc:
                log(f"error: {exc}")
                job["status"] = "error"
            finally:
                self._notify_job_running(False)

        thread = threading.Thread(target=run, daemon=True)
        self._threads[job["id"]] = thread
        thread.start()
        return job, None

    def wait_for_other_jobs(self, timeout):
        """Block until every running job but the caller's has finished."""
        me = threading.current_thread()
        deadline = time.time() + timeout
        with self._jobs_lock:
            others = [
                self._threads[j["id"]] for j in self._jobs
                if j["status"] == "running" and self._threads.get(j["id"]) is not me
            ]
        for thread in others:
            thread.join(max(0, deadline - time.time()))

    def _notify_job_running(self, running):
        if self.on_job_running is None:
            return
        try:
            self.on_job_running(running)
        except Exception:
            pass  # a UI nicety must never fail a job

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
        core.create_profiles(count, log, progress=progress, cancel=self.cancel)
        if self.cancel.is_set():
            log("cancelled by Stop")
            return
        self.controller().launch(url, log, progress=progress, cancel=self.cancel)

    def reload_sessions(self, url, log, progress=None):
        self.controller().reload(url, log, progress=progress)

    def launch_sessions(self, url, log, progress=None):
        self.controller().launch(url, log, progress=progress, cancel=self.cancel)

    def add_sessions(self, count, url, log, progress=None):
        """Create `count` more identities and launch only those."""
        created = core.add_profiles(count, log, progress=progress, cancel=self.cancel)
        if self.cancel.is_set():
            log("cancelled by Stop")
            return
        self.controller().launch(
            url, log, progress=progress, only=[ident.id for ident in created],
            cancel=self.cancel,
        )

    def remove_session(self, ident, log, progress=None):
        """Close one identity's window, if it has one, and delete its profile."""
        if progress:
            progress(f"Closing {ident}…")
        if self._controller is not None:
            try:
                self._controller.close(ident, log)
            except RuntimeError as exc:
                log(f"controller: {exc}")
        if progress:
            progress(f"Deleting {ident}…")
        core.delete_profile(ident, log)

    def stop_all(self, log, progress=None):
        if progress:
            progress("Waiting for the running job to wind down…")
        # A cancelled launch still has to finish the window it is opening, and
        # a cancelled create must not be mid-write when the wipe starts.
        self.wait_for_other_jobs(timeout=controller.LAUNCH_STEP_TIMEOUT)
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
        health = self.health()
        for ident in state["identities"]:
            ident["controlled"] = ident["id"] in controlled
            info = health.get(ident["id"], {})
            ident["dead"] = bool(info.get("dead"))
            ident["error"] = info.get("error")
        state["freshness"] = self.freshness
        with self._jobs_lock:
            state["jobs"] = [dict(j) for j in self._jobs[-5:]]
        return state
