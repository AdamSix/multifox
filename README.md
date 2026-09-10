# multifox

Run many isolated, fingerprint-spoofed browser sessions side by side. Each
identity gets its own disposable Firefox profile, its own SOCKS5 exit IP, and a
unique [Camoufox](https://github.com/daijro/camoufox) fingerprint whose
timezone/locale/geolocation are matched to that exit IP via a GeoIP lookup
through the proxy.

Identities are disposable: **Stop** kills every window and deletes all
profiles, so nothing (cookies, history, logins) survives between sessions.

## Download

Get the latest desktop app from
[GitHub Releases](https://github.com/AdamSix/multifox/releases/latest) — no
command line needed:

- [macOS — Apple Silicon](https://github.com/AdamSix/multifox/releases/latest/download/multifox-macos-arm64.zip)
- [macOS — Intel](https://github.com/AdamSix/multifox/releases/latest/download/multifox-macos-x86_64.zip)
- [Windows](https://github.com/AdamSix/multifox/releases/latest/download/multifox-windows-x86_64.zip)

Quick start:

1. Unzip and launch the app. On macOS the app is unsigned, so right-click →
   **Open** the first time (or run
   `xattr -dr com.apple.quarantine /path/to/multifox.app`).
2. On first run the launcher unpacks the bundled Camoufox browser, starts the
   dashboard, and presents an **Open dashboard** button
   (http://multifox.localhost:8787).
3. The launcher creates a `proxies.conf` in its data dir
   (`~/Library/Application Support/multifox` on macOS,
   `%APPDATA%\multifox` on Windows). Put one SOCKS5 `host:port` per line,
   in identity order (id1, id2, …). Each line must be a **different egress** —
   two identities sharing an exit IP are linked. SSH tunnels to your VPSes
   work well: `ssh -N -D 127.0.0.1:1081 user@vps1`.
4. **The proxies must be UP when you click Start**: creation sends a GeoIP
   lookup through each one so every identity's timezone and locale match its
   exit IP.
5. Keep the app running for the whole session; quitting it closes the browser
   windows it launched. Click **Stop** in the dashboard to kill every window
   and wipe all profiles.

## For developers

Everything below covers running and building from source.

### Requirements

- Python 3.10+ on PATH (`python3 --version`; on Windows, `py --version`)
- ~700MB disk for the Camoufox browser download
- macOS, Linux, or Windows (the `ffid.sh` CLI is macOS/Linux only; the
  dashboard works everywhere)
- One SOCKS5 proxy per identity (see Quick start above)

### Install

```sh
git clone https://github.com/AdamSix/multifox.git
cd multifox
python3 install.py           # Windows: py install.py
```

That's it. The installer creates `.venv`, installs the camoufox package, and
downloads the browser. It takes a few minutes and is safe to re-run.

### Run

```sh
./ffid.sh dashboard          # Windows: .venv\Scripts\python dashboard.py
```

Then open http://multifox.localhost:8787 (plain http://127.0.0.1:8787 works
too) — create identities, launch them, and watch live screenshots of every
window from one page. Keep the dashboard running for the whole session;
quitting it closes the browser windows it launched.

### Configure proxies

Edit `proxies.conf`: one `host:port` per line, in identity order (id1, id2,
…). Each line must be a **different egress** — two identities sharing an exit
IP are linked. `DIRECT` means no proxy (your real IP — rarely what you want).

**The proxies must be UP when you click Create**: creation sends a GeoIP
lookup through each proxy so every identity's timezone and locale match its
exit IP.

### Updating

The desktop launcher updates Camoufox automatically on every startup: if the
update fails but a browser is already installed, it logs a warning and
continues with the installed one. CLI users can update manually with
`./ffid.sh update`. The dashboard shows an amber warning when the installed
Camoufox browser or python package is behind the latest release.

### Desktop app builds

Local build:

```sh
./build_app.sh           # macOS:  dist/multifox.app + dist/multifox.zip
.\build_app.ps1          # Windows: dist\multifox\ + dist\multifox.zip
```

The build matches the machine it runs on — build on an Apple Silicon Mac for
arm64, on an Intel Mac for x86_64 (PyInstaller does not cross-compile). The
app is unsigned, so on another Mac: right-click → Open the first time (or
`xattr -dr com.apple.quarantine /path/to/multifox.app`).

CI builds (`.github/workflows/build.yml`): pushing a tag `v*` builds Apple
Silicon (macos-14), Intel (macos-13) and Windows (windows-latest) apps and
attaches the zips to a GitHub Release for that tag. The download links at the
top of this README always point at the latest release. To cut a release:

```sh
git tag v0.2.0 && git push origin v0.2.0
```

### CLI usage (macOS/Linux)

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

### Verifying a session

After launching, check each identity before use:

- IP: https://browserleaks.com/ip
- WebRTC/DNS leaks: https://browserleaks.com/webrtc
- Fingerprint: https://coveryourtracks.eff.org
