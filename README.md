# multifox

Run many isolated, fingerprint-spoofed browser sessions side by side. Each
identity gets its own disposable Firefox profile, its own SOCKS5 exit IP, and a
unique [Camoufox](https://github.com/daijro/camoufox) fingerprint whose
timezone/locale/geolocation are matched to that exit IP via a GeoIP lookup
through the proxy.

Identities are disposable: **Stop** kills every window and deletes all
profiles, so nothing (cookies, history, logins) survives between sessions.

## Requirements

- Python 3.10+ on PATH (`python3 --version`; on Windows, `py --version`)
- ~700MB disk for the Camoufox browser download
- macOS, Linux, or Windows (the `ffid.sh` CLI is macOS/Linux only; the
  dashboard works everywhere)
- One SOCKS5 proxy per identity — e.g. SSH tunnels to your VPSes:
  `ssh -N -D 127.0.0.1:1081 user@vps1`

## Install

```sh
git clone <this repo>        # or download and unzip it
cd ff-sessions
python3 install.py           # Windows: py install.py
```

That's it. The installer creates `.venv`, installs the camoufox package, and
downloads the browser. It takes a few minutes and is safe to re-run.

## Run

```sh
./ffid.sh dashboard          # Windows: .venv\Scripts\python dashboard.py
```

Then open http://127.0.0.1:8787 — create identities, launch them, and watch
live screenshots of every window from one page. Keep the dashboard running for
the whole session; quitting it closes the browser windows it launched.

## Configure proxies

Edit `proxies.conf`: one `host:port` per line, in identity order (id1, id2,
…). Each line must be a **different egress** — two identities sharing an exit
IP are linked. `DIRECT` means no proxy (your real IP — rarely what you want).

**The proxies must be UP when you click Create**: creation sends a GeoIP
lookup through each proxy so every identity's timezone and locale match its
exit IP.

## Updating

The desktop launcher updates Camoufox automatically on every startup: if the
update fails but a browser is already installed, it logs a warning and
continues with the installed one. CLI users can update manually with
`./ffid.sh update`. The dashboard shows an amber warning when the installed
Camoufox browser or python package is behind the latest release.

## Desktop app

A double-clickable desktop app (no command line needed) can be built with
PyInstaller:

```sh
./build_app.sh        # produces dist/ff-sessions.app and dist/ff-sessions.zip
```

The app shows a small launcher window: on first run it downloads the Camoufox
browser, then starts the dashboard and presents an **Open dashboard** button.
Quitting the window stops all sessions. Runtime data (proxies.conf, profiles/)
lives in `~/Library/Application Support/ff-sessions` (respectively
`%APPDATA%\ff-sessions` on Windows).

Architecture: the build matches the machine it runs on — build on an Apple
Silicon Mac for arm64, on an Intel Mac for x86_64. The app is unsigned, so on
another Mac: right-click → Open the first time (or
`xattr -dr com.apple.quarantine /path/to/ff-sessions.app`).

## CLI usage (macOS/Linux)

The dashboard covers the common workflow, but everything is also available as
detached CLI commands (browser windows survive the terminal exiting):

```sh
./ffid.sh setup              # same as python3 install.py
./ffid.sh create             # build profiles + personas (FFID_SESSIONS=4 to change the count)
./ffid.sh launch [url]       # open one window per identity
./ffid.sh stop               # kill everything and DELETE the profiles
./ffid.sh status             # what's running
./ffid.sh update             # upgrade camoufox package + browser
```

## Verifying a session

After launching, check each identity before use:

- IP: https://browserleaks.com/ip
- WebRTC/DNS leaks: https://browserleaks.com/webrtc
- Fingerprint: https://coveryourtracks.eff.org
