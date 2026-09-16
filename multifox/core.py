"""
Cross-platform core for managing isolated Camoufox identity profiles:
create/delete/status, driven by the dashboard. Browser discovery goes through
the camoufox package, so it works on macOS, Windows and Linux.

Each identity is a directory profiles/<id> holding the Firefox profile plus a
profile.json with the proxy it was created for and its Camoufox persona env.
The directory name is the identity id: a short random slug, generated once and
never reused, so netlogs/<id>.jsonl can only ever hold one persona.

Run '.venv/bin/python -m multifox.core update' to upgrade the camoufox
package + browser (the packaged app updates itself on startup).
"""

import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass

from . import paths
from .personas import camou_config, generate_persona, screen_ordinal

MAX_SESSIONS = 100

# Identity ids: digits and consonants only, so no slug reads as a word and
# none of 0/O/1/l can be misread off the screen.
IDENT_ALPHABET = "23456789bcdfghjkmnpqrstvwxyz"
IDENT_LENGTH = 4

# Seconds of random stagger between launches (avoids synchronized first
# connections). Applied per identity, so ten identities spread over ~25s.
LAUNCH_STAGGER = 5

PROFILE_FILE = "profile.json"

# Base prefs for every browser; identity_prefs() adds the per-persona ones.
# No privacy.resistFingerprinting: Camoufox does its own C++-level spoofing via
# the persona config; RFP would conflict with it.
FIREFOX_PREFS = {
    "network.dns.disableIPv6": True,  # prevent IPv6 bypassing the proxy
    # WebRTC: hard off + belt-and-suspenders
    "media.peerconnection.enabled": False,
    "media.peerconnection.ice.default_address_only": True,
    "media.peerconnection.ice.no_host": True,
    "media.peerconnection.ice.proxy_only_if_behind_proxy": True,
    # hygiene
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
    # never offer Troubleshoot Mode after a hard kill (second is the legacy name)
    "toolkit.startup.max_resumed_crashes": -1,
    "browser.sessionstore.max_resumed_crashes": -1,
    "dom.security.https_only_mode": True,
}


def identity_prefs(env):
    """Firefox prefs for one identity: the base set plus what its persona claims.

    Camoufox spoofs navigator.doNotTrack and navigator.globalPrivacyControl in
    JS, but the matching headers come from Firefox prefs. Left unset, a browser
    claims the preference in JS and never sends it — which is what Akamai
    reported back as 'dnt=unspecified'. BrowserForge randomises both per
    persona, so this cannot be a fixed pref.
    """
    prefs = dict(FIREFOX_PREFS)
    try:
        cfg = camou_config(env)
    except ValueError:
        return prefs
    prefs["privacy.donottrackheader.enabled"] = cfg.get("navigator.doNotTrack") == "1"
    prefs["privacy.globalprivacycontrol.enabled"] = (
        cfg.get("navigator.globalPrivacyControl") is True
    )
    return prefs


# -- proxy settings -----------------------------------------------------------


def proxies_enabled():
    """Global proxy toggle (dashboard UI). Off = every identity goes DIRECT."""
    try:
        return bool(json.loads(paths.SETTINGS.read_text()).get("proxies", False))
    except (OSError, ValueError):
        return False


def set_proxies_enabled(enabled):
    paths.SETTINGS.write_text(json.dumps({"proxies": bool(enabled)}) + "\n")


def proxy_conf_text():
    try:
        return paths.PROXY_CONF.read_text()
    except OSError:
        return ""


def write_proxy_conf(text):
    paths.PROXY_CONF.write_text(text if text.endswith("\n") else text + "\n")


def proxy_entries():
    """Non-comment lines of proxies.conf: 'host:port' or 'DIRECT'."""
    lines = (line.strip() for line in proxy_conf_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def effective_entries():
    """Proxy entries actually in effect: ['DIRECT'] for all when toggled off."""
    if not proxies_enabled():
        return ["DIRECT"]
    return proxy_entries()


# -- identities ---------------------------------------------------------------


def new_ident():
    """An unused identity id. Random, never derived from a count, never reused."""
    while True:
        ident = "".join(random.choices(IDENT_ALPHABET, k=IDENT_LENGTH))
        if not (paths.PROFILES / ident).exists():
            return ident


def _least_used_index(count, taken):
    """Index in range(count) that appears least often in taken; ties go to the lowest."""
    tally = Counter(taken)
    return min(range(count), key=lambda i: (tally[i], i))


@dataclass
class Identity:
    id: str  # also the profile directory name
    proxy: str  # proxies.conf entry this identity was created for
    env: dict  # CAMOU_* persona env for the browser process
    created: float  # epoch seconds; the dashboard orders tiles by it

    @property
    def dir(self):
        return paths.PROFILES / self.id

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {
            "id": self.id,
            "proxy": self.proxy,
            "created": self.created,
            "env": self.env,
        }
        (self.dir / PROFILE_FILE).write_text(json.dumps(data, indent=2) + "\n")

    @classmethod
    def load(cls, profile_dir):
        """Read profiles/<id>/profile.json. The id comes from the directory name.

        created is missing from profiles written before ids became slugs; the
        mtime of profile.json stands in, since it is written once at creation.
        """
        profile_file = profile_dir / PROFILE_FILE
        data = json.loads(profile_file.read_text())
        return cls(
            id=profile_dir.name,
            proxy=data["proxy"],
            env=dict(data["env"]),
            created=float(data.get("created") or profile_file.stat().st_mtime),
        )

    def summary(self):
        try:
            cfg = camou_config(self.env)
        except ValueError:
            return {"os": "?", "ua_tail": "?", "tz": "?"}
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


def scan_profiles():
    """(identities, names of profile dirs that produced none), oldest first.

    A profile whose profile.json is missing or unreadable — an interrupted
    creation, or a directory written by an older version — is skipped. Reporting
    the skips matters: otherwise the dashboard shows fewer identities than there
    are directories on disk and says nothing about the gigabytes still there.
    """
    if not paths.PROFILES.is_dir():
        return [], []
    identities, unloadable = [], []
    for entry in sorted(paths.PROFILES.iterdir()):
        if not entry.is_dir():
            continue
        try:
            identities.append(Identity.load(entry))
        except (OSError, ValueError, KeyError):
            unloadable.append(entry.name)
    return sorted(identities, key=lambda ident: (ident.created, ident.id)), unloadable


def load_identities():
    """All complete identities on disk, oldest first."""
    return scan_profiles()[0]


def create_profiles(count, log=print, progress=None):
    """Replace every profile on disk with a fresh set of `count` identities."""
    if not 1 <= count <= MAX_SESSIONS:
        raise ValueError(f"identity count must be 1-{MAX_SESSIONS} (got {count})")
    delete_profiles()  # a smaller count must not leave stale profiles behind
    return add_profiles(count, log, progress=progress)


def add_profiles(count, log=print, progress=None):
    """Create `count` more identities alongside the existing ones; returns them.

    Proxy and screen preset are the least-used ones among the identities
    already on disk, so both stay spread after identities are removed.
    """
    existing, _ = scan_profiles()
    total = len(existing) + count
    if count < 1 or total > MAX_SESSIONS:
        raise ValueError(f"identity count must be 1-{MAX_SESSIONS} (got {total})")
    entries = effective_entries()
    if not entries:
        raise RuntimeError(f"{paths.PROXY_CONF} has no proxy entries")
    if proxies_enabled():
        if total > len(entries):
            log(f"warning: {len(entries)} proxies for {total} identities — egresses repeat (shared IPs link identities)")
    else:
        log("proxies disabled — every identity connects DIRECTLY (your real IP)")
    identities, created = list(existing), []
    for n in range(1, count + 1):
        if progress:
            progress(f"Creating identity {n}/{count}", n, count)
        proxy = entries[_least_used_index(
            len(entries), [entries.index(i.proxy) for i in identities if i.proxy in entries]
        )]
        ident = new_ident()
        if proxy == "DIRECT" and proxies_enabled():
            log(f"warning: {ident} will connect DIRECTLY (your real IP)")
        env, lines = generate_persona(
            ident, proxy, [screen_ordinal(i.env) for i in identities]
        )
        for line in lines:
            log(line)
        identity = Identity(id=ident, proxy=proxy, env=env, created=time.time())
        identity.save()
        identities.append(identity)
        created.append(identity)
    log(f"Done. {count} profile(s) created.")
    return created


def _rmtree(path, attempts=5):
    """Delete a tree, retrying: Windows can hold a profile file open briefly
    after the browser process exits."""
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def delete_profiles(log=None):
    if paths.PROFILES.is_dir():
        _rmtree(paths.PROFILES)
        if log:
            log("profiles deleted — click Start to begin a fresh set")


def delete_profile(ident, log=None):
    """Delete one profiles/<ident> directory. Also removes an unreadable one."""
    path = (paths.PROFILES / ident).resolve()
    if path.parent != paths.PROFILES.resolve() or not path.is_dir():
        raise FileNotFoundError(f"{ident}: no such profile")
    _rmtree(path)
    if log:
        log(f"{ident}: profile deleted")


def status():
    raw = proxy_entries()
    identities, unloadable = scan_profiles()
    return {
        "proxy_entries": len(raw),
        "direct_entries": sum(1 for e in raw if e == "DIRECT"),
        "proxies_enabled": proxies_enabled(),
        "unloadable": unloadable,
        "identities": [
            {"id": ident.id, "proxy": ident.proxy, **ident.summary()} for ident in identities
        ],
    }


# -- camoufox browser ---------------------------------------------------------


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
        hint = "it will update automatically now" if paths.FROZEN else "run 'python -m multifox.core update'"
        log(
            f"warning: Camoufox browser {info['browser_installed']} is behind latest "
            f"{info['browser_latest']} — {hint}"
        )
    if info["package_latest"] and info["package_installed"] and info["package_installed"] != info["package_latest"]:
        hint = "download the latest multifox release" if paths.FROZEN else "run 'python -m multifox.core update'"
        log(
            f"warning: camoufox package {info['package_installed']} is behind latest "
            f"{info['package_latest']} — {hint}"
        )
    return info


def _update_browser_frozen(log):
    """In-process browser update for the packaged app (see update_camoufox)."""
    from camoufox.pkgman import (
        CamoufoxFetcher,
        CamoufoxNotInstalled,
        installed_verstr,
        list_available_versions,
    )

    try:
        installed = installed_verstr()
    except CamoufoxNotInstalled:
        return  # first install is the launcher's job (_ensure_browser)
    latest = next(iter(list_available_versions(include_prerelease=False)), None)
    if latest is None:
        log("warning: no supported camoufox browser release found — keeping installed version")
        return
    if installed == latest.version.full_string:
        log(f"camoufox browser {installed} is up to date")
        return
    log(f"updating camoufox browser {installed} -> {latest.version.full_string}…")
    CamoufoxFetcher(selected_version=latest).install(replace=True)
    log(f"camoufox browser {installed_verstr()} installed")


def update_camoufox(log=print):
    """Bring camoufox up to date.

    Source checkout: upgrade the pip package, then fetch the latest browser via
    the camoufox CLI. Frozen (PyInstaller) app: the package is baked into the
    bundle and pip isn't shipped, and re-executing sys.executable would just
    relaunch the app — so only the browser is updated, in-process via
    CamoufoxFetcher (which only offers releases the bundled package supports).
    Package updates reach frozen users as new multifox releases.
    """
    log_camoufox_freshness(log)
    if paths.FROZEN:
        _update_browser_frozen(log)
        return
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


if __name__ == "__main__":
    if sys.argv[1:] != ["update"]:
        sys.exit("usage: python -m multifox.core update")
    update_camoufox()
