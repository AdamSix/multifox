"""
Playwright controller for multifox — launches identities as persistent
contexts and keeps them under automation control (screenshots now, goto/eval/
harvest later).

Playwright's sync API has thread affinity: every object must be used from the
thread that called sync_playwright(). So the controller owns a dedicated
driver thread; callers dispatch commands through a queue and wait on results.

The controller must outlive the sessions: closing it closes the browsers.
"""

import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time

import ffid_core

SHOT_CACHE_SECONDS = 2
COMMAND_TIMEOUT = 30  # per-command wait; launch uses its own longer budget


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

    def _cmd_launch(self, url, log, progress=None):
        idents = ffid_core.existing_idents()
        if not idents:
            raise RuntimeError("no profiles found — run create first")
        ff = ffid_core.browser_path()
        entries = ffid_core.effective_entries()
        launched = 0
        for n, ident in enumerate(idents, 1):
            if ident in self._contexts:
                log(f"{ident}: already controlled, skipping")
                continue
            profile_dir = ffid_core.PROFILES / ident
            if not (profile_dir / "persona.env").is_file():
                raise RuntimeError(f"{profile_dir} is incomplete — run create first")
            if progress:
                progress(f"Launching window {n}/{len(idents)}", n, len(idents))
            if ffid_core.LAUNCH_STAGGER > 0:
                time.sleep(random.randint(0, ffid_core.LAUNCH_STAGGER))
            entry = ffid_core.proxy_for(int(ident[2:]), entries)
            env = os.environ.copy()
            env.update(ffid_core.load_persona_env(profile_dir / "persona.env"))
            kwargs = {
                "user_data_dir": str(profile_dir),
                "executable_path": ff,
                "headless": False,
                "env": env,
                "firefox_user_prefs": dict(ffid_core.FIREFOX_PREFS),
            }
            if entry != "DIRECT":
                kwargs["proxy"] = {"server": f"socks5://{entry}"}
            try:
                ctx = self._pw.firefox.launch_persistent_context(**kwargs)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if url and url != "about:blank":
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                self._contexts[ident] = {"ctx": ctx, "page": page, "shot": None}
                launched += 1
                log(f"launched {ident} (controlled)")
            except Exception as exc:
                log(f"error: {ident} failed to launch: {exc}")
        log(f"All {launched}/{len(idents)} identities controlled.")
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
        except Exception:
            return None  # page closed or crashed; next poll retries
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
        return _raise_os_window(ffid_core.PROFILES / ident)

    def _cmd_reload(self, url, log, progress=None):
        if not self._contexts:
            raise RuntimeError("no controlled sessions to reload")
        contexts = sorted(self._contexts.items(), key=lambda kv: int(kv[0][2:]))
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

    def _cmd_stop(self, log):
        count = len(self._contexts)
        for ident, entry in list(self._contexts.items()):
            try:
                entry["ctx"].close()
            except Exception as exc:
                log(f"warning: closing {ident} failed: {exc}")
        self._contexts.clear()
        if count:
            log(f"closed {count} controlled contexts")
        return count

    def _cmd_controlled(self):
        return sorted(self._contexts, key=lambda n: int(n[2:]))

    # -- public API (any thread) ---------------------------------------------

    def launch(self, url, log, progress=None):
        # generous timeout: stagger + N context launches
        return self._dispatch("launch", url, log, progress, timeout=3600)

    def screenshot(self, ident):
        return self._dispatch("screenshot", ident)

    def focus(self, ident):
        return self._dispatch("focus", ident)

    def reload(self, url, log, progress=None):
        return self._dispatch("reload", url, log, progress, timeout=600)

    def stop(self, log):
        return self._dispatch("stop", log, timeout=120)

    def controlled_idents(self):
        return self._dispatch("controlled")


_instance = None
_instance_lock = threading.Lock()


def get_controller():
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Controller()
        return _instance
