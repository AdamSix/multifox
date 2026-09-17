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
PAGE_LOAD_TIMEOUT = 60000  # ms, per goto/reload
# Longest one window can hold the driver thread: stagger + launch + page load.
# Stop waits at most this long for a cancelled launch to finish its window.
LAUNCH_STEP_TIMEOUT = 120
LONG_COMMANDS = frozenset({"launch", "reload", "stop", "close"})

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
# because the reason for a block usually only appears there. Set-Cookie is
# handled separately, one entry per cookie (see _set_cookies).
NETLOG_HEADERS = frozenset({
    "akamai-grn",
    "content-type",
    "retry-after",
    "server",
    "server-timing",
    "x-akamai-request-id",
    "x-akamai-transformed",
    "x-cache",
    "x-reference-error",
})

# Bot-vendor cookies whose value carries the verdict. Recorded from the Cookie
# header of every request, since a block often arrives as a 200 challenge page.
# Akamai's _abck reads hash~valid~sensor~-1~-1: a second field of 0 means the
# sensor validated, -1 means it did not.
BOT_COOKIES = frozenset({
    "_abck", "bm_sz", "ak_bmsc", "bm_sv", "bm_mi",  # Akamai
    "datadome",  # DataDome
    "_px3", "_pxvid", "_pxhd",  # HUMAN / PerimeterX
})

# The verdict sits in the first 100 characters of _abck; the rest is sensor blob.
NETLOG_MAX_COOKIE = 100


def _clip(value, limit):
    return value if len(value) <= limit else value[:limit] + f"…(+{len(value) - limit}B)"


def _clip_headers(headers, keep=None):
    """Header dict with long values truncated, optionally limited to `keep` names."""
    clipped = {}
    for name, value in headers.items():
        if name == "set-cookie" or (keep is not None and name not in keep):
            continue
        clipped[name] = _clip(value, NETLOG_MAX_HEADER)
    return clipped


def _bot_cookies(cookie_header):
    """{name: clipped value} of the BOT_COOKIES present in a Cookie header."""
    found = {}
    for part in cookie_header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name in BOT_COOKIES:
            found[name] = _clip(value, NETLOG_MAX_COOKIE)
    return found


def _set_cookies(response):
    """Each Set-Cookie header of a response, clipped per cookie.

    response.headers merges repeated Set-Cookie values into one string, so a
    single clip would drop every cookie after the first; headers_array keeps
    them apart. Only called when the merged view shows a Set-Cookie at all,
    since headers_array is a round trip to the browser.
    """
    return [
        _clip(h["value"], NETLOG_MAX_HEADER)
        for h in response.headers_array()
        if h["name"].lower() == "set-cookie"
    ]


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
        self._running = None  # name of the command the driver thread is executing
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
        self._netlog_emit = {}  # ident -> emit(entry) into that netlog, with its caps
        self._ready.set()
        while True:
            name, args, result_q = self._commands.get()
            self._running = name
            try:
                handler = getattr(self, f"_cmd_{name}")
                result_q.put((True, handler(*args)))
            except Exception as exc:
                result_q.put((False, exc))
            finally:
                self._running = None

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
        last_cookies = {}

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

        self._netlog_emit[ident] = emit
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
                headers = response.headers
                if response.ok:
                    entry["headers"] = _clip_headers(headers, NETLOG_HEADERS)
                else:
                    entry["headers"] = _clip_headers(headers)
                    entry["request_headers"] = _clip_headers(request.headers)
                if "set-cookie" in headers:
                    entry["set_cookie"] = _set_cookies(response)
                # Only when the values change: the same map on every request
                # of a page load buries the one where the verdict flipped.
                cookies = _bot_cookies(request.headers.get("cookie", ""))
                if cookies and cookies != last_cookies:
                    last_cookies.clear()
                    last_cookies.update(cookies)
                    entry["cookies"] = cookies
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
        self._netlog_emit.clear()

    def _log_dead_cookies(self, ident, entry):
        """Write the bot-vendor cookies from the jar once an identity turns dead.

        The final verdict may have arrived in a response the netlog skipped or
        clipped; the jar holds whatever the browser ended up with.
        """
        emit = self._netlog_emit.get(ident)
        if emit is None:
            return
        try:
            jar = entry["ctx"].cookies()
        except Exception as exc:
            emit({"event": "dead", "error": str(exc).splitlines()[0][:200]})
            return
        emit({
            "event": "dead",
            "cookies": {
                c["name"]: _clip(c["value"], NETLOG_MAX_COOKIE)
                for c in jar
                if c["name"] in BOT_COOKIES
            },
        })

    def _cmd_launch(self, url, log, progress=None, only=None, cancel=None):
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
        total = len(identities)
        launched = 0
        cancelled = cancel.is_set if cancel is not None else lambda: False
        for n, ident in enumerate(identities, 1):
            if cancelled():
                log("cancelled by Stop")
                break
            if ident.id in self._contexts:
                log(f"{ident.id}: already controlled, skipping")
                continue
            if progress:
                progress(f"Launching window {n}/{total}", n - 1, total)
            if core.LAUNCH_STAGGER > 0:
                delay = random.randint(0, core.LAUNCH_STAGGER)
                if cancel is not None:
                    if cancel.wait(delay):
                        log("cancelled by Stop")
                        break
                else:
                    time.sleep(delay)
            kwargs = {
                "user_data_dir": str(ident.dir),
                "executable_path": ff,
                "headless": False,
                # Without this Playwright emulates a 1280x720 viewport in every
                # window, so innerWidth contradicts the persona's outerWidth
                # and is identical across identities.
                "no_viewport": True,
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
                    if progress:
                        progress(f"Loading page in window {n}/{total}", n - 1, total)
                    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                self._contexts[ident.id] = {
                    "ctx": ctx, "page": page, "shot": None, "fails": 0, "error": None,
                }
                launched += 1
                log(f"launched {ident.id} (controlled)")
            except Exception as exc:
                log(f"error: {ident.id} failed to launch: {exc}")
        if progress:
            progress(f"Launched {launched}/{total} windows", total, total)
        log(f"All {launched}/{total} identities controlled.")
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
            if entry["fails"] == SHOT_FAILURES_BEFORE_DEAD:
                self._log_dead_cookies(ident, entry)
            return None  # page closed or crashed; next poll retries
        entry["fails"] = 0
        entry["error"] = None
        entry["shot"] = (time.time(), data)
        return data

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
                progress(f"Reloading window {n}/{len(contexts)}", n - 1, len(contexts))
            try:
                if url and url != "about:blank":
                    entry["page"].goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                else:
                    entry["page"].reload(wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                entry["shot"] = None
                log(f"reloaded {ident}")
            except Exception as exc:
                log(f"error: {ident} failed to reload: {exc}")
        if progress:
            progress(f"Reloaded {len(contexts)} windows", len(contexts), len(contexts))
        return len(self._contexts)

    def _cmd_close(self, ident, log):
        """Close one identity's context and its netlog. False if it wasn't controlled."""
        entry = self._contexts.pop(ident, None)
        handle = self._netlogs.pop(ident, None)
        self._netlog_emit.pop(ident, None)
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

    # -- public API (any thread) ---------------------------------------------

    def launch(self, url, log, progress=None, only=None, cancel=None):
        # generous timeout: stagger + N context launches
        return self._dispatch("launch", url, log, progress, only, cancel, timeout=3600)

    def screenshot(self, ident):
        # While a long command holds the driver thread, a dispatched shot would
        # wait behind it and tie up one of the browser's few connections to the
        # dashboard; enough of those and the state poll and Stop POST queue too.
        if self._running in LONG_COMMANDS:
            entry = self._contexts.get(ident)
            return entry["shot"][1] if entry and entry["shot"] else None
        return self._dispatch("screenshot", ident)

    def focus(self, ident):
        return self._dispatch("focus", ident)

    def reload(self, url, log, progress=None):
        return self._dispatch("reload", url, log, progress, timeout=600)

    def close(self, ident, log):
        return self._dispatch("close", ident, log, timeout=120)

    def stop(self, log):
        return self._dispatch("stop", log, timeout=120)

    # The two readers below do not go through _dispatch: a launch occupies the
    # driver thread for minutes and every /api/state poll would wait behind it,
    # freezing the progress bar. They only read plain values from _contexts,
    # never a Playwright object, so the thread rule is not broken.

    def controlled_idents(self):
        return list(self._contexts)

    def health(self):
        return {
            ident: {
                "dead": entry["fails"] >= SHOT_FAILURES_BEFORE_DEAD,
                "error": entry["error"],
            }
            for ident, entry in list(self._contexts.items())
        }

