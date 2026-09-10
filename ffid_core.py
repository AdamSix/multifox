"""
Cross-platform core for managing isolated Camoufox identity profiles.

This is the Python-native equivalent of ffid.sh (which stays macOS/Linux-only):
create/launch/stop/status, usable from the dashboard or any other Python
driver. Browser discovery goes through the camoufox package, so it works on
macOS, Windows and Linux.

Instances launched here are tracked as Popen handles, so stop/status work
without pgrep/pkill. Instances launched externally (via ffid.sh) are visible
on disk but show as untracked.
"""

import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from gen_personas import generate_personas, proxy_entries

# Runtime data (proxies.conf, profiles/) lives here. Defaults to the project
# directory; the packaged app sets FFID_HOME to a per-user data dir.
ROOT = Path(os.environ.get("FFID_HOME") or Path(__file__).resolve().parent)
CONF = ROOT / "proxies.conf"
PROFILES = ROOT / "profiles"
MAX_SESSIONS = 100

# Seconds of random stagger between launches (avoids synchronized first connections).
LAUNCH_STAGGER = 5

# ident -> {"popen": Popen, "url": str, "started_at": float}
_tracked = {}


# Non-proxy preferences, single source of truth: write_user_js renders these
# into user.js for the CLI path, and the Playwright controller passes them as
# firefox_user_prefs. (Proxy prefs differ per path and stay in write_user_js.)
IPV6_PREF = {"network.dns.disableIPv6": True}

WEBRTC_PREFS = {
    "media.peerconnection.enabled": False,
    "media.peerconnection.ice.default_address_only": True,
    "media.peerconnection.ice.no_host": True,
    "media.peerconnection.ice.proxy_only_if_behind_proxy": True,
}

HYGIENE_PREFS = {
    "browser.shell.checkDefaultBrowser": False,
    "browser.shell.skipDefaultBrowserCheckOnFirstRun": True,
    "browser.aboutwelcome.enabled": False,
    "trailhead.firstrun.didSeeAboutWelcome": True,
    "datareporting.policy.dataSubmissionEnabled": False,
    "datareporting.healthreport.uploadEnabled": False,
    "app.shield.optoutstudies.enabled": False,
    "browser.discovery.enabled": False,
    "browser.crashReports.unsubmittedCheck.autoSubmit2": False,
    "signon.rememberSignons": False,
    "signon.autofillForms": False,
    "browser.formfill.enable": False,
    "browser.sessionstore.resume_from_crash": False,
    "toolkit.startup.max_resumed_crashes": -1,
    "browser.sessionstore.max_resumed_crashes": -1,
    "dom.security.https_only_mode": True,
}

FIREFOX_PREFS = {**IPV6_PREF, **WEBRTC_PREFS, **HYGIENE_PREFS}

# explanatory comments kept in the generated user.js
_TRAILING_COMMENTS = {
    "network.dns.disableIPv6": " // prevent IPv6 bypassing the proxy",
    "toolkit.startup.max_resumed_crashes": " // never offer Troubleshoot Mode after a hard kill",
    "browser.sessionstore.max_resumed_crashes": " // legacy name of the same pref",
}


def _render_pref(key, value):
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, str):
        rendered = f'"{value}"'
    else:
        rendered = str(value)
    return f'user_pref("{key}", {rendered});' + _TRAILING_COMMENTS.get(key, "")


def proxy_for(index, entries=None):
    """1-based identity index -> proxies.conf entry, cycled modulo entry count."""
    entries = proxy_entries() if entries is None else entries
    if not entries:
        raise RuntimeError(f"{CONF} has no proxy entries")
    return entries[(index - 1) % len(entries)]


def browser_path():
    override = os.environ.get("FIREFOX_BIN")
    if override:
        return override
    from camoufox.pkgman import launch_path

    return launch_path()


def camoufox_freshness(timeout=10):
    """Compare the installed Camoufox browser + package against the latest releases.

    Returns a dict of version strings; values are None when a check couldn't run
    (offline, package not installed). Never raises — callers decide whether
    staleness is fatal.
    """
    info = {
        "browser_installed": None,
        "browser_latest": None,
        "package_installed": None,
        "package_latest": None,
    }
    try:
        from importlib.metadata import version as pkg_version

        info["package_installed"] = pkg_version("camoufox")
    except Exception:
        pass
    try:
        from camoufox.pkgman import installed_verstr

        info["browser_installed"] = installed_verstr()
    except Exception:
        pass
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/camoufox/json", timeout=timeout) as resp:
            info["package_latest"] = json.load(resp)["info"]["version"]
    except Exception:
        pass
    try:
        from camoufox.pkgman import list_available_versions

        latest = max(list_available_versions(include_prerelease=False), key=lambda v: v.version)
        info["browser_latest"] = latest.version.full_string
    except Exception:
        pass
    return info


def log_camoufox_freshness(log=print):
    """Warn when the Camoufox browser or python package is behind the latest release."""
    info = camoufox_freshness()
    if info["browser_latest"] and info["browser_installed"] and info["browser_installed"] != info["browser_latest"]:
        log(
            f"warning: Camoufox browser {info['browser_installed']} is behind latest "
            f"{info['browser_latest']} — run './ffid.sh update'"
        )
    if info["package_latest"] and info["package_installed"] and info["package_installed"] != info["package_latest"]:
        log(
            f"warning: camoufox package {info['package_installed']} is behind latest "
            f"{info['package_latest']} — run './ffid.sh update'"
        )
    return info


def update_camoufox(log=print):
    """Upgrade the camoufox package and fetch the latest browser (official/stable channel)."""
    log_camoufox_freshness(log)
    for cmd in (
        [sys.executable, "-m", "pip", "install", "--upgrade", "camoufox[geoip]"],
        [sys.executable, "-m", "camoufox", "set", "official/stable"],
        [sys.executable, "-m", "camoufox", "fetch"],
    ):
        log(f"$ python {' '.join(cmd[1:])}")
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout.replace("\r", "\n").splitlines():
            if line.strip():
                log(line)
        if proc.returncode:
            raise RuntimeError(f"command failed (exit {proc.returncode}): {' '.join(cmd)}")
    log("camoufox up to date")


def write_user_js(profile_dir, index, entry):
    """index is 0-based; entry is a proxies.conf line ('host:port' or 'DIRECT')."""
    lines = [f"// identity {index + 1} — generated by ffid.sh, edit via the script", ""]
    if entry == "DIRECT":
        lines.append('user_pref("network.proxy.type", 0); // WARNING: direct connection, no proxy')
    else:
        host, _, port = entry.rpartition(":")
        lines += [
            'user_pref("network.proxy.type", 1);',
            f'user_pref("network.proxy.socks", "{host}");',
            f'user_pref("network.proxy.socks_port", {port});',
            'user_pref("network.proxy.socks_version", 5);',
            'user_pref("network.proxy.socks_remote_dns", true);',
            'user_pref("network.proxy.failover_direct", false); // never leak direct if proxy dies',
        ]
    lines += [
        _render_pref("network.dns.disableIPv6", True),
        "",
        "// WebRTC: hard off + belt-and-suspenders",
        *(_render_pref(k, v) for k, v in WEBRTC_PREFS.items()),
        "",
        "// NOTE: no privacy.resistFingerprinting here — Camoufox does its own",
        "// C++-level spoofing via the persona config; RFP would conflict with it.",
        "",
        "// hygiene",
        *(_render_pref(k, v) for k, v in HYGIENE_PREFS.items()),
    ]
    (profile_dir / "user.js").write_text("\n".join(lines) + "\n")


def create_profiles(count, log=print):
    if not 1 <= count <= MAX_SESSIONS:
        raise ValueError(f"identity count must be 1-{MAX_SESSIONS} (got {count})")
    entries = proxy_entries()
    if not entries:
        raise RuntimeError(f"{CONF} has no proxy entries")
    if count > len(entries):
        log(f"warning: {len(entries)} proxies for {count} identities — egresses repeat (shared IPs link identities)")
    # clear any previous set so a smaller count doesn't leave stale profiles behind
    if PROFILES.is_dir() and PROFILES == ROOT / "profiles":
        shutil.rmtree(PROFILES)
    PROFILES.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        ident = f"id{i + 1}"
        entry = entries[i % len(entries)]
        if entry == "DIRECT":
            log(f"warning: {ident} will connect DIRECTLY (your real IP)")
        profile_dir = PROFILES / ident
        profile_dir.mkdir(parents=True, exist_ok=True)
        write_user_js(profile_dir, i, entry)
    # one Camoufox persona per identity (does GeoIP lookups through the proxies)
    for line in generate_personas(count):
        log(line)
    log(f"Done. {count} profiles created.")


def load_persona_env(path):
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        key, _, value = line[len("export "):].partition("=")
        parsed = shlex.split(value)
        env[key] = parsed[0] if parsed else ""
    return env


def existing_idents():
    if not PROFILES.is_dir():
        return []
    idents = [p.name for p in PROFILES.iterdir() if p.is_dir() and re.fullmatch(r"id\d+", p.name)]
    return sorted(idents, key=lambda name: int(name[2:]))


def launch_profiles(url="about:blank", log=print):
    ff = browser_path()
    idents = existing_idents()
    if not idents:
        raise RuntimeError("no profiles found — run create first")
    for ident in idents:
        profile_dir = PROFILES / ident
        if not (profile_dir / "user.js").is_file() or not (profile_dir / "persona.env").is_file():
            raise RuntimeError(f"{profile_dir} is incomplete — run create first")
        if LAUNCH_STAGGER > 0:
            time.sleep(random.randint(0, LAUNCH_STAGGER))
        env = os.environ.copy()
        env.update(load_persona_env(profile_dir / "persona.env"))
        proc = subprocess.Popen(
            [ff, "-no-remote", "-profile", str(profile_dir), "--new-window", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        _tracked[ident] = {"popen": proc, "url": url, "started_at": time.time()}
        log(f"launched {ident} (pid {proc.pid})")
    log(f"All {len(idents)} identities running.")


def stop_profiles(log=print):
    running = {ident: t for ident, t in _tracked.items() if t["popen"].poll() is None}
    if running:
        for t in running.values():
            t["popen"].terminate()
        log(f"terminated {len(running)} instances, waiting for exit…")
        deadline = time.time() + 60
        while time.time() < deadline:
            if all(t["popen"].poll() is not None for t in running.values()):
                break
            time.sleep(0.5)
        for ident, t in running.items():
            if t["popen"].poll() is None:
                log(f"force-killing {ident} (pid {t['popen'].pid})")
                t["popen"].kill()
        time.sleep(1)
    else:
        log("no tracked instances running")
    _tracked.clear()
    # safety: only ever delete the profiles dir inside this project's folder
    if PROFILES.is_dir() and PROFILES == ROOT / "profiles":
        shutil.rmtree(PROFILES)
        log("profiles deleted — run create to start a fresh set")


def _persona_summary(profile_dir):
    persona = profile_dir / "persona.env"
    if not persona.is_file():
        return {}
    try:
        env = load_persona_env(persona)
        cfg = json.loads("".join(v for k, v in sorted(env.items()) if k.startswith("CAMOU_CONFIG_")))
    except (ValueError, OSError):
        return {}
    ua = cfg.get("navigator.userAgent", "")
    if "Windows" in ua:
        os_name = "windows"
    elif "Macintosh" in ua:
        os_name = "macos"
    elif "Linux" in ua:
        os_name = "linux"
    else:
        os_name = "?"
    return {
        "os": os_name,
        "ua_tail": ua[-40:],
        "tz": cfg.get("timezone", cfg.get("int:timezone", "?")),
    }


def status():
    entries = proxy_entries() if CONF.is_file() else []
    identities = []
    for ident in existing_idents():
        index = int(ident[2:])
        tracked = _tracked.get(ident)
        if tracked is not None:
            alive = tracked["popen"].poll() is None
            state = {"tracked": True, "running": alive, "pid": tracked["popen"].pid if alive else None}
        else:
            state = {"tracked": False, "running": None, "pid": None}
        identities.append(
            {
                "id": ident,
                "proxy": proxy_for(index, entries) if entries else "?",
                **_persona_summary(PROFILES / ident),
                **state,
            }
        )
    return {
        "proxy_entries": len(entries),
        "direct_entries": sum(1 for e in entries if e == "DIRECT"),
        "identities": identities,
    }
