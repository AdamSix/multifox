"""
Playwright controller for multifox — launches identities as persistent
contexts and keeps them under automation control (screenshots now, goto/eval/
harvest later).

Every context also writes a diagnostic network log to netlogs/<ident>.jsonl,
so a block or a failed request can be attributed after the fact.

Playwright's sync API has thread affinity: every object must be used from the
thread that called sync_playwright(). So the controller owns a dedicated
driver thread; callers dispatch commands through a queue and wait on results.

The controller must outlive the sessions: closing it closes the browsers.
"""

import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time

from . import core, paths

SHOT_CACHE_SECONDS = 2
COMMAND_TIMEOUT = 30  # per-command wait; launch uses its own longer budget

# Consecutive screenshot failures before an identity is reported as dead. A
# crashed or closed page otherwise looks identical to an idle one in the UI.
SHOT_FAILURES_BEFORE_DEAD = 3

# Successful images/fonts/css say nothing about why a session was blocked.
NETLOG_SKIP_TYPES = {"image", "font", "media", "stylesheet"}

# Header values are truncated to this many characters. Akamai's _abck cookie
# alone runs to 1.2KB, and a full Cookie header to 6KB; the prefix is enough to
# identify a header, and the profile holds the real cookie values.
NETLOG_MAX_HEADER = 200

# Entries written per (status, url) before further ones are suppressed. A page
# stuck reloading a block page would otherwise log the same failure for hours.
NETLOG_MAX_PER_KEY = 20

# Hard ceiling per identity. Reaching it writes one notice and stops.
NETLOG_MAX_BYTES = 16 * 1024 * 1024

# Headers kept for successful responses. Failures record every header instead,
# because the reason for a block usually only appears there.
NETLOG_HEADERS = frozenset({
    "akamai-grn",
    "content-type",
    "retry-after",
    "server",
    "server-timing",
    "set-cookie",
    "x-akamai-request-id",
    "x-akamai-transformed",
    "x-cache",
    "x-reference-error",
})


def _clip_headers(headers, keep=None):
    """Header dict with long values truncated, optionally limited to `keep` names."""
    clipped = {}
    for name, value in headers.items():
        if keep is not None and name not in keep:
            continue
        clipped[name] = value if len(value) <= NETLOG_MAX_HEADER else (
            value[:NETLOG_MAX_HEADER] + f"…(+{len(value) - NETLOG_MAX_HEADER}B)"
        )
    return clipped


def accessibility_trusted(prompt=False):
    """macOS: is this process Accessibility-trusted? Always True on other OSes.

    prompt=True asks macOS to show the system grant dialog (shown only once per
    app; after that the user must toggle it manually in System Settings).
    """
    if sys.platform != "darwin":
        return True
    import ctypes

    his = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework"
        "/Frameworks/HIServices.framework/HIServices"
    )
    if not prompt:
        return bool(his.AXIsProcessTrusted())
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    key = ctypes.c_void_p.in_dll(his, "kAXTrustedCheckOptionPrompt").value
    val = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
    keys = (ctypes.c_void_p * 1)(key)
    values = (ctypes.c_void_p * 1)(val)
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    options = cf.CFDictionaryCreate(
        None, keys, values, 1,
        ctypes.byref(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
        ctypes.byref(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")),
    )
    his.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    his.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    return bool(his.AXIsProcessTrustedWithOptions(options))


def _pids_for_profile(profile_dir):
    """PIDs of processes whose command line mentions this profile dir, oldest first.

    The path is followed by whitespace or end-of-line so id1 doesn't match id10.
    (POSIX character class: pgrep regexes don't support \\s.)
    """
    pattern = re.escape(str(profile_dir)) + r"([[:space:]]|$)"
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return sorted(int(p) for p in out.split() if p.strip().isdigit())


def _raise_os_window(profile_dir):
    """Best effort: bring the OS window owning this profile to the front.

    Returns a detail string; a leading "warning:" means the raise failed.
    """
    if sys.platform == "darwin":
        for pid in _pids_for_profile(profile_dir):
            r = subprocess.run(
                [
                    "osascript", "-e",
                    'tell application "System Events" to set frontmost of '
                    f"first process whose unix id is {pid} to true",
                ],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                return f"raised pid {pid}"
        return (
            "warning: could not raise the window — grant Accessibility access to "
            "the app running the dashboard (System Settings → Privacy & Security "
            "→ Accessibility)"
        )
    if sys.platform == "win32":
        ps = (
            "$p = Get-CimInstance Win32_Process -Filter \"Name='camoufox.exe'\" |"
            f" Where-Object {{ $_.CommandLine -match [regex]::Escape('{profile_dir}') + '[\" ]' }} |"
            " Sort-Object ProcessId | Select-Object -First 1;"
            " if ($p) { (New-Object -ComObject WScript.Shell).AppActivate($p.ProcessId) | Out-Null }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        return "focus requested"
    if shutil.which("xdotool"):
        for pid in _pids_for_profile(profile_dir):
            out = subprocess.run(
                ["xdotool", "search", "--pid", str(pid)], capture_output=True, text=True
            ).stdout.split()
            if out:
                subprocess.run(["xdotool", "windowactivate", out[-1]], capture_output=True)
                return f"raised pid {pid}"
    return "warning: no window-raise tool available (install xdotool)"


class Controller:
    def __init__(self):
        self._commands = queue.Queue()
        self._ready = threading.Event()
        self._start_error = None
        self._thread = threading.Thread(target=self._run, name="pw-driver", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError(f"playwright driver failed to start: {self._start_error}")
        if self._start_error:
            raise RuntimeError(f"playwright driver failed to start: {self._start_error}")

    # -- driver thread ------------------------------------------------------

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
        except Exception as exc:
            self._start_error = exc
            self._ready.set()
            return
        self._contexts = {}  # ident -> {"ctx", "page", "proxy", "shot": (ts, bytes)}
        self._netlogs = {}  # ident -> open netlogs/<ident>.jsonl handle
        self._ready.set()
        while True:
            name, args, result_q = self._commands.get()
            try:
                handler = getattr(self, f"_cmd_{name}")
                result_q.put((True, handler(*args)))
            except Exception as exc:
                result_q.put((False, exc))

    def _dispatch(self, name, *args, timeout=COMMAND_TIMEOUT):
        result_q = queue.Queue()
        self._commands.put((name, args, result_q))
        ok, value = result_q.get(timeout=timeout)
        if not ok:
            raise value
        return value

    # -- commands (driver thread only) --------------------------------------

    def _attach_netlog(self, ident, ctx):
        """Record this context's responses and network failures to netlogs/<ident>.jsonl.

        One JSON object per line. Kept outside profiles/ so a Stop, which wipes
        every profile, does not take the evidence with it.
        """
        try:
            paths.NETLOGS.mkdir(parents=True, exist_ok=True)
            handle = (paths.NETLOGS / f"{ident}.jsonl").open("a", buffering=1)
        except OSError:
            return  # diagnostics are never worth failing a launch over
        self._netlogs[ident] = handle
        written = {"bytes": 0, "stopped": False}
        seen = {}

        def emit(entry):
            entry["t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            try:
                line = json.dumps(entry) + "\n"
            except (ValueError, TypeError):
                return
            if written["bytes"] + len(line) > NETLOG_MAX_BYTES:
                if written["stopped"]:
                    return
                written["stopped"] = True
                line = json.dumps({"event": "truncated", "limit": NETLOG_MAX_BYTES}) + "\n"
            written["bytes"] += len(line)
            try:
                handle.write(line)
            except OSError:
                pass

        def record(entry):
            """Emit entry unless this (status, url) has already been logged enough."""
            key = (entry.get("status"), entry.get("url"))
            count = seen.get(key, 0) + 1
            seen[key] = count
            if count > NETLOG_MAX_PER_KEY:
                if count == NETLOG_MAX_PER_KEY + 1:
                    emit({
                        "event": "suppressed",
                        "status": key[0],
                        "url": key[1],
                        "after": NETLOG_MAX_PER_KEY,
                    })
                return
            emit(entry)

        emit({"event": "launch", "ident": ident})

        def on_response(response):
            try:
                request = response.request
                if response.ok and request.resource_type in NETLOG_SKIP_TYPES:
                    return
                entry = {
                    "status": response.status,
                    "method": request.method,
                    "url": response.url,
                    "type": request.resource_type,
                }
                if response.ok:
                    entry["headers"] = _clip_headers(response.headers, NETLOG_HEADERS)
                else:
                    entry["headers"] = _clip_headers(response.headers)
                    entry["request_headers"] = _clip_headers(request.headers)
            except Exception:
                return  # an exception here would surface on the driver thread
            record(entry)

        def on_requestfailed(request):
            try:
                entry = {
                    "status": None,
                    "method": request.method,
                    "url": request.url,
                    "type": request.resource_type,
                    "failure": request.failure,
                }
            except Exception:
                return
            record(entry)

        ctx.on("response", on_response)
        ctx.on("requestfailed", on_requestfailed)

    def _close_netlogs(self):
        for handle in self._netlogs.values():
            try:
                handle.close()
            except OSError:
                pass
        self._netlogs.clear()

    def _cmd_launch(self, url, log, progress=None, only=None):
        identities, unloadable = core.scan_profiles()
        if unloadable:
            log(
                f"warning: ignoring {len(unloadable)} unreadable profile(s) "
                f"({', '.join(unloadable)}) — delete them, or press Stop to clear all"
            )
        if only is not None:
            identities = [ident for ident in identities if ident.id in set(only)]
        if not identities:
            raise RuntimeError("no profiles found — run create first")
        ff = core.browser_path()
        launched = 0
        for n, ident in enumerate(identities, 1):
            if ident.id in self._contexts:
                log(f"{ident.id}: already controlled, skipping")
                continue
            if progress:
                progress(f"Launching window {n}/{len(identities)}", n, len(identities))
            if core.LAUNCH_STAGGER > 0:
                time.sleep(random.randint(0, core.LAUNCH_STAGGER))
            kwargs = {
                "user_data_dir": str(ident.dir),
                "executable_path": ff,
                "headless": False,
                "env": {**os.environ, **ident.env},
                "firefox_user_prefs": core.identity_prefs(ident.env),
            }
            if ident.proxy != "DIRECT":
                kwargs["proxy"] = {"server": f"socks5://{ident.proxy}"}
            try:
                ctx = self._pw.firefox.launch_persistent_context(**kwargs)
                self._attach_netlog(ident.id, ctx)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if url and url != "about:blank":
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                self._contexts[ident.id] = {
                    "ctx": ctx, "page": page, "shot": None, "fails": 0, "error": None,
                }
                launched += 1
                log(f"launched {ident.id} (controlled)")
            except Exception as exc:
                log(f"error: {ident.id} failed to launch: {exc}")
        log(f"All {launched}/{len(identities)} identities controlled.")
        return launched

    def _cmd_screenshot(self, ident):
        entry = self._contexts.get(ident)
        if entry is None:
            return None
        cached = entry["shot"]
        if cached and time.time() - cached[0] < SHOT_CACHE_SECONDS:
            return cached[1]
        try:
            data = entry["page"].screenshot(type="jpeg", quality=50)
        except Exception as exc:
            entry["fails"] += 1
            entry["error"] = str(exc).splitlines()[0][:200]
            return None  # page closed or crashed; next poll retries
        entry["fails"] = 0
        entry["error"] = None
        entry["shot"] = (time.time(), data)
        return data

    def _cmd_health(self):
        return {
            ident: {
                "dead": entry["fails"] >= SHOT_FAILURES_BEFORE_DEAD,
                "error": entry["error"],
            }
            for ident, entry in self._contexts.items()
        }

    def _cmd_focus(self, ident):
        entry = self._contexts.get(ident)
        if entry is None:
            raise RuntimeError(f"{ident} is not controlled — no window to focus")
        try:
            entry["page"].bring_to_front()
        except Exception:
            pass  # page may be closed; the OS raise below is the important part
        return _raise_os_window(paths.PROFILES / ident)

    def _cmd_reload(self, url, log, progress=None):
        if not self._contexts:
            raise RuntimeError("no controlled sessions to reload")
        contexts = list(self._contexts.items())
        for n, (ident, entry) in enumerate(contexts, 1):
            if progress:
                progress(f"Reloading window {n}/{len(contexts)}", n, len(contexts))
            try:
                if url and url != "about:blank":
                    entry["page"].goto(url, wait_until="domcontentloaded", timeout=60000)
                else:
                    entry["page"].reload(wait_until="domcontentloaded", timeout=60000)
                entry["shot"] = None
                log(f"reloaded {ident}")
            except Exception as exc:
                log(f"error: {ident} failed to reload: {exc}")
        return len(self._contexts)

    def _cmd_close(self, ident, log):
        """Close one identity's context and its netlog. False if it wasn't controlled."""
        entry = self._contexts.pop(ident, None)
        handle = self._netlogs.pop(ident, None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if entry is None:
            return False
        try:
            entry["ctx"].close()
        except Exception as exc:
            log(f"warning: closing {ident} failed: {exc}")
        log(f"closed {ident}")
        return True

    def _cmd_stop(self, log):
        count = len(self._contexts)
        for ident, entry in list(self._contexts.items()):
            try:
                entry["ctx"].close()
            except Exception as exc:
                log(f"warning: closing {ident} failed: {exc}")
        self._contexts.clear()
        self._close_netlogs()
        if count:
            log(f"closed {count} controlled contexts")
        return count

    def _cmd_controlled(self):
        return list(self._contexts)

    # -- public API (any thread) ---------------------------------------------

    def launch(self, url, log, progress=None, only=None):
        # generous timeout: stagger + N context launches
        return self._dispatch("launch", url, log, progress, only, timeout=3600)

    def screenshot(self, ident):
        return self._dispatch("screenshot", ident)

    def focus(self, ident):
        return self._dispatch("focus", ident)

    def reload(self, url, log, progress=None):
        return self._dispatch("reload", url, log, progress, timeout=600)

    def close(self, ident, log):
        return self._dispatch("close", ident, log, timeout=120)

    def stop(self, log):
        return self._dispatch("stop", log, timeout=120)

    def controlled_idents(self):
        return self._dispatch("controlled")

    def health(self):
        return self._dispatch("health")

