"""
Cross-platform core for managing isolated Camoufox identity profiles:
create/delete/status, driven by the dashboard. Browser discovery goes through
the camoufox package, so it works on macOS, Windows and Linux.

Each identity is a directory profiles/idN holding the Firefox profile plus a
profile.json with the proxy it was created for and its Camoufox persona env.

Run '.venv/bin/python -m multifox.core update' to upgrade the camoufox
package + browser (the packaged app updates itself on startup).
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

from . import paths
from .personas import camou_config, generate_persona

MAX_SESSIONS = 100

# Seconds of random stagger between launches (avoids synchronized first connections).
LAUNCH_STAGGER = 5

PROFILE_FILE = "profile.json"

# Passed to every browser as firefox_user_prefs. No privacy.resistFingerprinting:
# Camoufox does its own C++-level spoofing via the persona config; RFP would
# conflict with it.
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


@dataclass
class Identity:
    index: int  # 1-based
    proxy: str  # proxies.conf entry this identity was created for
    env: dict  # CAMOU_* persona env for the browser process

    @property
    def id(self):
        return f"id{self.index}"

    @property
    def dir(self):
        return paths.PROFILES / self.id

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        data = {"index": self.index, "proxy": self.proxy, "env": self.env}
        (self.dir / PROFILE_FILE).write_text(json.dumps(data, indent=2) + "\n")

    @classmethod
    def load(cls, profile_file):
        data = json.loads(profile_file.read_text())
        return cls(index=int(data["index"]), proxy=data["proxy"], env=dict(data["env"]))

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


def load_identities():
    """All complete identities on disk, ordered by index."""
    if not paths.PROFILES.is_dir():
        return []
    identities = []
    for profile_file in paths.PROFILES.glob(f"id*/{PROFILE_FILE}"):
        try:
            identities.append(Identity.load(profile_file))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(identities, key=lambda ident: ident.index)


def create_profiles(count, log=print, progress=None):
    if not 1 <= count <= MAX_SESSIONS:
        raise ValueError(f"identity count must be 1-{MAX_SESSIONS} (got {count})")
    entries = effective_entries()
    if not entries:
        raise RuntimeError(f"{paths.PROXY_CONF} has no proxy entries")
    if proxies_enabled():
        if count > len(entries):
            log(f"warning: {len(entries)} proxies for {count} identities — egresses repeat (shared IPs link identities)")
    else:
        log("proxies disabled — every identity connects DIRECTLY (your real IP)")
    # clear any previous set so a smaller count doesn't leave stale profiles behind
    delete_profiles()
    for i in range(count):
        if progress:
            progress(f"Creating identity {i + 1}/{count}", i + 1, count)
        proxy = entries[i % len(entries)]
        if proxy == "DIRECT" and proxies_enabled():
            log(f"warning: id{i + 1} will connect DIRECTLY (your real IP)")
        env, lines = generate_persona(i, proxy)
        for line in lines:
            log(line)
        Identity(index=i + 1, proxy=proxy, env=env).save()
    log(f"Done. {count} profiles created.")


def delete_profiles(log=None):
    if paths.PROFILES.is_dir():
        shutil.rmtree(paths.PROFILES)
        if log:
            log("profiles deleted — click Start to begin a fresh set")


def status():
    raw = proxy_entries()
    return {
        "proxy_entries": len(raw),
        "direct_entries": sum(1 for e in raw if e == "DIRECT"),
        "proxies_enabled": proxies_enabled(),
        "identities": [
            {"id": ident.id, "proxy": ident.proxy, **ident.summary()} for ident in load_identities()
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
