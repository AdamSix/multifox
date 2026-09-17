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
  personas.py         generate_persona(ident, proxy, ordinal) -> CAMOU_* env for one identity
  core.py             identities on disk (create / add / delete one or all), proxy config, per-identity prefs, camoufox version checks + update
  controller.py       Playwright driver thread; launch / screenshot / focus / reload / close one / stop, per-identity network log
  app.py              App: shared state — controller, background jobs, freshness
  dashboard.py        stdlib http.server on 127.0.0.1:8787 + JSON API
  launcher.py         packaged app: browser install/update, then dashboard in a pywebview window
  static/index.html   the whole UI (one file: markup, CSS, vanilla JS polling /api/state)
packaging/            PyInstaller specs, build_app.sh / build_app.ps1, entitlements.plist, icons, screenshots
.github/workflows/    build.yml — tag v* builds all three platforms, signs + notarizes macOS, cuts a Release
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
- **`/api/state` must never wait on the driver thread.** A launch holds that
  thread for minutes, and a poll queued behind it froze the progress bar at
  `1/N` until the whole job finished. `controlled_idents()` and `health()`
  therefore read plain values straight out of `_contexts` without
  `_dispatch`. Anything `state()` calls must stay that cheap. `screenshot()`
  returns the cached frame without dispatching while a `LONG_COMMANDS`
  command runs: the browser allows few connections to one host, and shot
  requests queued behind a launch used them all up, so the state poll and
  the Stop POST hung too. Progress callbacks report the
  number of items *finished*, not the item being worked on, so the bar
  starts at 0 and only reaches 100 when the job does.
- **The dashboard window is pinned above other windows while a job runs.**
  Every Firefox window activates itself on launch and would cover the
  dashboard. `App.on_job_running(bool)` fires at job start and end; the
  launcher sets pywebview's `on_top` from it, which needs no Accessibility
  access. The pin drops `UNPIN_DELAY` seconds after the job, not with it: a
  Firefox window activates itself a moment after Playwright reports the
  launch, and the last window of a job landed in front of the dashboard when
  the pin dropped at once. The headless `dashboard.py` has no window and
  sets no hook. **The UI must not call `alert()` or `confirm()`:** pywebview
  shows those as a native dialog at normal window level, which opens behind
  the pinned dashboard while the page blocks on it, so the app looks frozen
  and cannot quit. Errors go through `notify()` inline. The one remaining
  `confirm()` is on a tile's remove button, which is disabled during a job.
- **The controller must outlive the sessions.** Closing it closes the browsers.
  So the dashboard process has to stay alive for the whole session.
- **Playwright event handlers run on the driver thread.** The `response` and
  `requestfailed` handlers in `_attach_netlog` swallow every exception for that
  reason: one raised there would surface inside the driver loop. Keep any new
  handler cheap and total.
- **`FIREFOX_PREFS` in `core.py` deliberately omits
  `privacy.resistFingerprinting`.** Camoufox spoofs at the C++ level from the
  persona config; RFP fights it. Do not add it.
- **A header pref and its spoofed JS counterpart must agree.** Camoufox
  spoofs `navigator.doNotTrack` and `navigator.globalPrivacyControl` in JS,
  while the DNT and Sec-GPC headers come from prefs. Left unset, every
  identity claimed the preference in JS and never sent it. Both are now
  pinned off: Firefox 135 removed Do Not Track, and GPC is only on in
  private windows, yet BrowserForge data predates the removal and handed
  every identity both. `FIREFOX_PREFS` never sends either header and
  `_patch` sets the JS values to match. `identity_prefs(env)` is the hook
  for any future pref that must follow a persona value.
- **Launch must pass `no_viewport=True`.** multifox calls Playwright's
  `launch_persistent_context` directly, bypassing camoufox's launcher, which
  sets this default. Without it Playwright emulates a 1280x720 viewport in
  every window: `innerWidth` is identical across identities and far below the
  persona's spoofed `outerWidth`, an impossible browser frame. With it,
  Juggler measures the real window, which Camoufox sizes to the persona's
  outer dimensions. The same bypass is why `FIREFOX_PREFS` carries the two
  `webgl.*` prefs camoufox's launcher would otherwise add.
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
- **The persona language is pinned, not random.** Camoufox picks the locale
  language at random from the CLDR speaker share of the GeoIP region. That
  share counts who speaks a language, not who browses in it, so 14% of UK
  identities loaded pages in French. `_patch_locale` overwrites
  `locale:language` / `locale:script` / `locale:all` with the region's
  most-spoken language, leaving region, timezone and geolocation as GeoIP set
  them. This is not a shared-value tell: it is the value nearly every real
  visitor from that region has, while a rare one stands out per identity.

- **Identity ids carry no order, so two things must supply it.** Tiles sort by
  `created` from `profile.json`. Live contexts sort by insertion order in
  `Controller._contexts`, which is launch order. Nothing may parse a number out
  of an id.
- **Screen constraints in `personas.py` keep a persona self-consistent.**
  The constraint asks for an exact size, because a range lets the generator
  invent sizes no device ships. Not every size has a fingerprint, so
  `_generate` walks `SCREENS` until one works. Only presets that fit the host
  display are offered (`_fitting_screens`, via `camoufox.display`): the real
  window is sized to the persona's `outerWidth`/`outerHeight` and the OS
  clamps it to the display, so a persona claiming 2560x1440 on a 1512x982
  MacBook measured `innerWidth` 1512 against a spoofed `outerWidth` 2560. A
  laptop therefore gets fewer distinct presets; an external monitor widens
  the set. `SCREENS_NOT_ON` also drops sizes no device of the persona OS
  ships (1366x768 and 1536x864 on macOS). If nothing fits, every size for
  the OS is used rather than failing. `generate_persona` no longer passes
  `i_know_what_im_doing`, so camoufox's `LeakWarning`s are captured and
  written to the job log instead of being silenced.
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
- **macOS codesign must run on every nested Mach-O binary, not just the
  `.app` bundle — including binaries shipped as opaque data.** `codesign
  --deep` is unreliable on a PyInstaller tree this size, so `build_app.sh`'s
  `sign_tree` walks a bundle and signs bottom-up (loose Mach-O files, then
  nested `.app`/`.framework` bundles innermost-first, then the outer bundle)
  before signing the outer `.app`. Apple's notarizer unpacks and checks the
  bundled Camoufox browser payload zip too, even though PyInstaller treats it
  as opaque data — `build_app.sh` runs `sign_tree` on the staged payload
  *before* it gets zipped into `bundle_payload.zip`, not just on the final
  `.app`. Every signed binary needs the hardened runtime (`--options
  runtime`) plus `packaging/entitlements.plist`, since Python's frozen
  interpreter and the bundled native binaries (Camoufox/Playwright/Node)
  aren't signed by Apple. Within one directory, dylibs must be signed before
  any executable that links against them by a same-directory relative
  path — Camoufox's own `camoufox` binary links `libmozavcodec.dylib` this
  way, and codesign refuses to sign an executable whose linked dylib isn't
  signed yet. `find`'s enumeration order isn't guaranteed to put dylibs
  first, so `sign_tree` signs all `*.dylib`/`*.so` in a scope before any
  other executable there. This only reproduced on one CI runner
  (`macos-15-intel`), not locally or on `macos-14` — don't assume a clean
  local run means the signing order is safe.

## On-disk state

Under `paths.HOME` (`~/Library/Application Support/multifox` on macOS,
`%APPDATA%\multifox` on Windows, the project dir from a source checkout):

- `profiles/<id>/` — the Firefox profile directory. `<id>` is the identity id:
  4 characters from `IDENT_ALPHABET` (digits and consonants, so no slug reads
  as a word), drawn at random and never reused.
- `profiles/<id>/profile.json` — `{id, proxy, created, env}`. `env` is the
  CAMOU_* persona. This is the identity format; a change here invalidates
  existing profiles, and `Identity.load` silently skips anything it cannot
  parse. The id on disk is the directory name, not the `id` field, so a
  profile written before ids became slugs still loads. `created` orders the
  dashboard tiles; without it they would reshuffle on every poll, and it falls
  back to the mtime of `profile.json`.
- `proxies.conf` — one SOCKS5 `host:port` or `DIRECT` per line. Comments start
  with `#`. A new identity takes the least-used line, not the line at its
  position: identities have no position once one can be removed.
- `settings.json` — `{"proxies": bool}` only.
- `dashboard.log`, `launcher.log` — full job logs; the UI shows progress plus
  lines matching warning/error/fail.

Under `paths.NETLOGS` (`netlogs/` beside `profiles/`):

- `netlogs/<id>.jsonl` — one JSON object per response, written by the Playwright
  context. A `{"event": "launch"}` line marks each run, since the file is
  appended across runs. Successful image/font/media/stylesheet responses are
  skipped. A successful response keeps only the headers in `NETLOG_HEADERS`; a
  failed one keeps every response header plus the request headers, because that
  is where a block explains itself. Network failures are recorded with
  `"status": null` and a `failure` string. `Set-Cookie` is logged as
  `set_cookie`, a list with one clipped entry per cookie: Playwright merges
  repeated `Set-Cookie` headers into one string, and one clip of that dropped
  every cookie after the first, which is where Akamai's `_abck` usually sat.
  Every request whose `Cookie` header carries a bot-vendor cookie
  (`BOT_COOKIES`: Akamai, DataDome, HUMAN) gets a `cookies` map of those
  values, clipped to `NETLOG_MAX_COOKIE`, on success too — a block often
  arrives as a 200 challenge page. The verdict is in the first 100 characters
  of `_abck` (`hash~valid~…`, `0` validated, `-1` not). When an identity turns
  dead, one `{"event": "dead", "cookies": {...}}` line dumps the same cookies
  from the context's jar, since the final verdict may have arrived in a
  skipped response.

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
count never leaves stale identities behind. Add appends to the set and Remove
deletes one profile, both while the other sessions keep running — which is why
ids are random rather than an index: an index would have to be reused, and the
netlog of the old holder would then gain a second persona.

## HTTP API (127.0.0.1:8787, localhost only)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | `static/index.html`, sent `no-store`: WebKit's disk cache outlives the app and served a previous release's page |
| GET | `/api/state` | identities, proxy counts, camoufox freshness, last 5 jobs |
| GET | `/api/shot/<id>` | JPEG screenshot, cached 2s per identity |
| GET/POST | `/api/proxies/conf` | read / write `proxies.conf` text |
| POST | `/api/start` | create N profiles then launch; reloads instead if sessions run |
| POST | `/api/create` | profiles only, no launch |
| POST | `/api/add` | create N more profiles (default 1) and launch only those |
| POST | `/api/remove` | close one identity and delete its profile |
| POST | `/api/launch` | launch existing profiles |
| POST | `/api/stop` | close every context and delete all profiles |
| POST | `/api/focus` | raise one identity's OS window |
| POST | `/api/proxies` | set the global proxy toggle |
| POST | `/api/open-log` | open `dashboard.log` in the OS text editor |

`/api/state` reports `dead: true` for an identity whose screenshots have failed
`SHOT_FAILURES_BEFORE_DEAD` times running, with the last error in `error`. The
UI paints that tile red and tells the user to check the window, because a
crashed page is otherwise indistinguishable from an idle one.

`/api/state` also carries `unloadable`: the `profiles/<id>` directories that
produced no identity. `scan_profiles` returns both lists, and `load_identities`
is a wrapper over it. Skipping a broken profile silently is not acceptable —
the identity disappears from the dashboard while its ~80MB directory stays on
disk — so the UI shows a persistent banner and `_cmd_launch` logs a warning.
The banner's delete buttons post the directory name to `/api/remove`, which
accepts a name that no identity could be loaded from for that reason.

Long operations run as background jobs: one at a time, `409` when another is
already running. A POST returns a job id immediately; the UI polls
`/api/state`. Any new long operation must go through `App.start_job` and accept
`log=` and `progress=` keyword arguments.

Stop is the one exception: it starts with `preempt=True` while another job
runs. That sets `App.cancel`, an Event the create and launch loops check
before each identity (the launch stagger sleeps on it too), so they exit
early. Stop then calls `wait_for_other_jobs` before closing and wiping,
because the window being opened or the profile being written cannot be
interrupted; it waits at most `LAUNCH_STEP_TIMEOUT`. Any new loop that runs
for more than a few seconds must take `cancel=` and check it the same way.

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
