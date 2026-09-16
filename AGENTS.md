# AGENTS.md — orientation for agents working in this repo

**Keep this file current.** After any change that alters the architecture, the
module layout, an HTTP endpoint, an on-disk file format, a build step, or the
CI flow, update the matching section here in the same commit. Do not let it
drift. If a change makes a statement below wrong, fix the statement. This file
describes intent and structure, not a changelog — do not add a history
section. `README.md` is the user-facing doc (download, install, proxies,
verification); update it too when user-visible behavior changes.

## What the app is

multifox runs many isolated, fingerprint-spoofed Firefox sessions side by side
on one machine, and shows them all in one local dashboard.

Press Start in the dashboard and multifox:

1. Generates N personas — a realistic OS / hardware / locale mix from
   BrowserForge data, via the `camoufox` python package.
2. Gives each persona its own disposable Firefox profile plus a unique
   [Camoufox](https://github.com/daijro/camoufox) fingerprint (OS, fonts,
   WebGL, screen, timezone, locale).
3. Launches N headed Camoufox windows as Playwright persistent contexts, so
   every window stays under automation control.
4. Serves a dashboard with a live screenshot per window. A tile click raises
   that OS window.

Use case: testing how a site behaves for many distinct visitors at once —
anti-bot / fingerprint checks, geo and A/B QA, several logins that must not
share cookies or a fingerprint.

Per-identity SOCKS5 exit IPs are implemented but **experimental and untested**.
The default is direct (no proxy) for every identity.

## Architecture

```
install.py            venv + camoufox package + browser download (source checkout only)
dashboard.py          shim -> multifox.dashboard:main   (headless server, dev use)
launcher.py           shim -> multifox.launcher:main    (desktop app entry point)
multifox/
  paths.py            all filesystem locations; HOME (writable data) vs BUNDLE (read-only assets)
  personas.py         generate_persona(index, proxy) -> CAMOU_* env for one identity
  core.py             identities on disk, proxy config, per-identity prefs, camoufox version checks + update
  controller.py       Playwright driver thread; launch / screenshot / focus / reload / stop, per-identity network log
  app.py              App: shared state — controller, background jobs, freshness
  dashboard.py        stdlib http.server on 127.0.0.1:8787 + JSON API
  launcher.py         packaged app: browser install/update, then dashboard in a pywebview window
  static/index.html   the whole UI (one file: markup, CSS, vanilla JS polling /api/state)
packaging/            PyInstaller specs, build_app.sh / build_app.ps1, icons, screenshots
.github/workflows/    build.yml — tag v* builds all three platforms and cuts a Release
```

Layer rule: `paths` <- `personas` <- `core` <- `controller` <- `app` <-
(`dashboard`, `launcher`). Keep it acyclic. The shims at the repo root exist so
`python3 launcher.py` and the PyInstaller specs keep working; put no logic in
them.

### Things that are easy to break

- **Playwright thread affinity.** The sync API demands that every object is
  used on the thread that called `sync_playwright()`. `Controller` owns one
  driver thread; callers push commands onto a queue and wait for a result. Any
  new browser operation must be a `_cmd_*` method plus a public wrapper that
  calls `_dispatch`. Never touch a context or page from an HTTP handler.
- **The controller must outlive the sessions.** Closing it closes the browsers.
  So the dashboard process has to stay alive for the whole session.
- **Playwright event handlers run on the driver thread.** The `response` and
  `requestfailed` handlers in `_attach_netlog` swallow every exception for that
  reason: one raised there would surface inside the driver loop. Keep any new
  handler cheap and total.
- **`FIREFOX_PREFS` in `core.py` deliberately omits
  `privacy.resistFingerprinting`.** Camoufox spoofs at the C++ level from the
  persona config; RFP fights it. Do not add it.
- **Prefs that have a JS counterpart must be set per identity.** Camoufox
  spoofs `navigator.doNotTrack` and `navigator.globalPrivacyControl` in JS,
  while the DNT and Sec-GPC headers come from prefs. BrowserForge randomises
  both per persona, so `identity_prefs(env)` derives them from the persona
  instead of fixing them in `FIREFOX_PREFS`. Left unset, every identity
  claimed the preference in JS and never sent it.
- **`OS_PERSONAS` in `personas.py` is restricted to the host's own OS
  (`HOST_OS`), not a fixed cross-platform mix.** WebGL always renders through
  this process's real backend regardless of what a persona claims, so a
  persona claiming a different OS implies a backend (e.g. Direct3D on
  Windows) it can never produce. Measured directly: on a macOS host,
  identities claiming a Windows GPU and identities claiming an Apple GPU
  produced byte-identical WebGL render hashes, proving neither touched the
  claimed backend. Linux hosts are the unresolved exception — no `"linux"`
  persona exists (its font set ships no base Latin family), so they still get
  the old cross-OS mix and keep the host-mismatch tell open.
- **Screen constraints in `personas.py` keep a persona self-consistent.**
  Without them the generator clamps to the host screen, so a spoofed-Windows
  identity would claim a MacBook resolution. The constraint asks for an exact
  size, because a range lets the generator invent sizes no device ships. Not
  every size has a fingerprint, so `_generate` walks `SCREENS` until one works.
- **Never pass `screen.*` or `navigator.*` through `launch_options(config=)`.**
  Camoufox records which domains the caller set and then skips its own
  corrections for them: `clamp_screen_to_display`, `fix_screen_no_taskbar`,
  `clamp_window_dimensions`, `fix_navigator_arch`. One override key disables
  the lot, which silently produced screens ignoring the size constraint.
  Repairs belong in `_patch`, after generation, where the emitted values are
  known and can be clamped against each other.
- **`camou_config` must sort chunks numerically.** Camoufox splits the config
  at 2047 characters on Windows, so a persona reaches `CAMOU_CONFIG_10`, and a
  name sort puts it before `_2` and yields invalid JSON. `Identity.summary`
  swallows that as a `ValueError`, so the only symptom is a dashboard showing
  `?` for every field. `_chunk` re-splits at 2047 on every platform.
- **Frozen vs source paths.** `paths.FROZEN` splits writable data (per-user
  data dir when frozen, the project dir otherwise) from bundled assets
  (`sys._MEIPASS`). New files must be classified as one or the other in
  `paths.py`, never hardcoded elsewhere.
- **Frozen updates only fetch the browser.** pip is not shipped and
  re-executing `sys.executable` would relaunch the app, so package updates
  reach packaged users only as a new multifox release.
- **`multiprocessing.freeze_support()` in `launcher.main`** stops camoufox's
  addon locks from re-launching a second copy of the whole app.

## On-disk state

Under `paths.HOME` (`~/Library/Application Support/multifox` on macOS,
`%APPDATA%\multifox` on Windows, the project dir from a source checkout):

- `profiles/idN/` — the Firefox profile directory.
- `profiles/idN/profile.json` — `{index, proxy, env}`. `env` is the CAMOU_*
  persona. This is the identity format; a change here invalidates existing
  profiles, and `Identity.load` silently skips anything it cannot parse.
- `proxies.conf` — one SOCKS5 `host:port` or `DIRECT` per line, in identity
  order. Comments start with `#`.
- `settings.json` — `{"proxies": bool}` only.
- `dashboard.log`, `launcher.log` — full job logs; the UI shows progress plus
  lines matching warning/error/fail.

Under `paths.NETLOGS` (`netlogs/` beside `profiles/`):

- `netlogs/idN.jsonl` — one JSON object per response, written by the Playwright
  context. A `{"event": "launch"}` line marks each run, since the file is
  appended across runs. Successful image/font/media/stylesheet responses are
  skipped. A successful response keeps only the headers in `NETLOG_HEADERS`; a
  failed one keeps every response header plus the request headers, because that
  is where a block explains itself. Network failures are recorded with
  `"status": null` and a `failure` string.

These logs live outside `profiles/` on purpose, so Stop does not delete the
evidence of why a session was blocked.

Three caps keep them bounded, all in `controller.py`:

- `NETLOG_MAX_HEADER` truncates each header value. Akamai's `_abck` cookie runs
  to 1.2KB and a full `Cookie` header to 6KB, which made an unclipped failure
  entry 9.5KB.
- `NETLOG_MAX_PER_KEY` logs at most N entries per `(status, url)`, then writes
  one `{"event": "suppressed"}` line and tallies silently. This is what stops a
  page looping on a block page from logging for hours. Suppression is announced
  when it starts rather than summarised at close, so a hard kill loses nothing.
- `NETLOG_MAX_BYTES` is the per-identity ceiling. On reaching it the file gets
  one `{"event": "truncated"}` line and nothing more.

Nothing rotates the files across runs — delete them by hand.

Stop deletes every profile. Start deletes the previous set first, so a smaller
count never leaves stale identities behind.

## HTTP API (127.0.0.1:8787, localhost only)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | `static/index.html` |
| GET | `/api/state` | identities, proxy counts, camoufox freshness, last 5 jobs |
| GET | `/api/shot/<id>` | JPEG screenshot, cached 2s per identity |
| GET/POST | `/api/proxies/conf` | read / write `proxies.conf` text |
| POST | `/api/start` | create N profiles then launch; reloads instead if sessions run |
| POST | `/api/create` | profiles only, no launch |
| POST | `/api/launch` | launch existing profiles |
| POST | `/api/stop` | close every context and delete all profiles |
| POST | `/api/focus` | raise one identity's OS window |
| POST | `/api/proxies` | set the global proxy toggle |
| POST | `/api/open-log` | open `dashboard.log` in the OS text editor |

`/api/state` reports `dead: true` for an identity whose screenshots have failed
`SHOT_FAILURES_BEFORE_DEAD` times running, with the last error in `error`. The
UI paints that tile red and tells the user to check the window, because a
crashed page is otherwise indistinguishable from an idle one.

`/api/state` also carries `unloadable`: the `profiles/idN` directories that
produced no identity. `scan_profiles` returns both lists, and `load_identities`
is a wrapper over it. Skipping a broken profile silently is not acceptable —
the identity disappears from the dashboard while its ~80MB directory stays on
disk — so the UI shows a persistent banner and `_cmd_launch` logs a warning.

Long operations run as background jobs: one at a time, `409` when another is
already running. A POST returns a job id immediately; the UI polls
`/api/state`. Any new long operation must go through `App.start_job` and accept
`log=` and `progress=` keyword arguments.

Window raising is per-OS and best effort: AppleScript via System Events on
macOS (needs Accessibility access), `AppActivate` through PowerShell on
Windows, `xdotool` on Linux. A returned string that starts with `warning:`
means the raise failed.

## Working in this repo

- Python 3.10+. Stdlib only at runtime plus `camoufox[geoip]` (which brings
  Playwright and BrowserForge) and `pywebview`. There is no requirements file:
  `install.py` is the dependency list. Keep the runtime dependency set this
  small.
- No test suite and no linter config. Verify by running the dashboard from a
  source checkout: `.venv/bin/python dashboard.py`, then
  http://multifox.localhost:8787.
- Do not run builds or installs unless the task asks for it. Builds are slow
  and per-machine; PyInstaller does not cross-compile.
- The UI is one hand-written HTML file with no build step and no framework.
  Keep it that way.
- Release: `git tag vX.Y.Z && git push origin vX.Y.Z`.
