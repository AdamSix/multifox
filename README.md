# multifox

Run many isolated, fingerprint-spoofed browser sessions side by side. Each
identity gets its own disposable Firefox profile and a unique
[Camoufox](https://github.com/daijro/camoufox) fingerprint (OS, fonts, WebGL,
screen, timezone, locale, …).

Identities are disposable: **Stop** kills every window and deletes all
profiles, so nothing (cookies, history, logins) survives between sessions.

By default every identity connects directly, with no proxy. Per-identity
SOCKS5 exit IPs (with GeoIP-matched timezone/locale) are an **experimental,
untested** feature — leave proxies off for now. See
[Proxies (experimental)](#proxies-experimental) below.

## Download

Get the latest desktop app from
[GitHub Releases](https://github.com/AdamSix/multifox/releases/latest) — no
command line needed:

- [macOS — Apple Silicon](https://github.com/AdamSix/multifox/releases/latest/download/multifox-macos-arm64.zip)
- [macOS — Intel](https://github.com/AdamSix/multifox/releases/latest/download/multifox-macos-x86_64.zip)
- [Windows](https://github.com/AdamSix/multifox/releases/latest/download/multifox-windows-x86_64.zip)

Quick start:

1. Unzip and launch. On macOS the app is unsigned, so right-click → **Open**
   the first time (or run
   `xattr -dr com.apple.quarantine /path/to/multifox.app`).
2. First run unpacks the bundled Camoufox browser, starts the dashboard, and
   shows an **Open dashboard** button (http://multifox.localhost:8787).
3. Keep the app running for the whole session; quitting it closes the browser
   windows it launched. Click **Stop** in the dashboard to kill every window
   and wipe all profiles.

## Run from source

Requirements: Python 3.10+ on PATH, ~700MB disk for the browser download.
macOS, Linux, or Windows.

```sh
git clone https://github.com/AdamSix/multifox.git
cd multifox
python3 install.py               # Windows: py install.py
.venv/bin/python dashboard.py    # Windows: .venv\Scripts\python dashboard.py
```

Then open http://multifox.localhost:8787 (plain http://127.0.0.1:8787 works
too) — create identities, launch them, and watch live screenshots of every
window from one page. Click a tile to bring that window to the front; on
macOS this needs Accessibility access (the app prompts for it, or enable it
in System Settings → Privacy & Security → Accessibility). Keep the dashboard
running for the whole session.

The desktop launcher updates Camoufox automatically on every startup,
continuing with the installed browser if the update fails. From a source
checkout, update with `.venv/bin/python -m multifox.core update`. The
dashboard warns when the installed Camoufox is behind the latest release.

## Desktop app builds

```sh
./packaging/build_app.sh           # macOS:  dist/multifox.app + dist/multifox.zip
.\packaging\build_app.ps1          # Windows: dist\multifox\ + dist\multifox.zip
```

Layout: the Python code lives in the `multifox/` package (`core`, `personas`,
`controller`, `dashboard`, `launcher`, plus `static/`), with thin
`launcher.py` / `dashboard.py` shims at the root so `python3 launcher.py` and
the PyInstaller specs keep working. Build scripts and specs are in
`packaging/`; `install.py` stays at the root.

Builds are per-machine (PyInstaller does not cross-compile): build on an
Apple Silicon Mac for arm64, on an Intel Mac for x86_64. The app is unsigned
— see the macOS note in Quick start.

CI builds (`.github/workflows/build.yml`): pushing a tag `v*` builds Apple
Silicon, Intel, and Windows apps and attaches the zips to a GitHub Release.
To cut a release:

```sh
git tag v0.3.0 && git push origin v0.3.0
```

## Proxies (experimental)

> **Experimental and untested.** Leave proxies off for now — run every
> identity direct (the default). Note that direct identities all share your
> real IP, and timezone/locale GeoIP matching is skipped.

When enabled, each identity gets its own SOCKS5 exit IP, and creation sends a
GeoIP lookup through each proxy so the identity's timezone, locale, and
geolocation match its exit IP.

- Dashboard: flip the proxies toggle on and edit `proxies.conf` in the data
  dir (`~/Library/Application Support/multifox` on macOS, `%APPDATA%\multifox`
  on Windows). From a source checkout: edit `proxies.conf` in the project
  directory.
- One SOCKS5 `host:port` per line, in identity order (id1, id2, …). Each line
  must be a **different egress** — two identities sharing an exit IP are
  linked. `DIRECT` means no proxy for that identity.
- SSH tunnels to your VPSes work well: `ssh -N -D 127.0.0.1:1081 user@vps1`.
- **The proxies must be UP when you create identities**, or the GeoIP lookups
  fail.

## Verifying a session

After launching, check each identity before use:

- IP: https://browserleaks.com/ip
- WebRTC/DNS leaks: https://browserleaks.com/webrtc
- Fingerprint: https://coveryourtracks.eff.org
