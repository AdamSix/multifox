"""
Chrome backend: identities that run in the installed, unmodified Google Chrome.

Driven through Patchright, a Playwright fork that removes the CDP leaks
(Runtime.enable, --enable-automation) a stock Playwright launch shows. The
browser itself is the real Chrome build, so nothing about the binary stands
out. The price is the fingerprint: GPU, canvas, audio, fonts, hardware and
screen are the host's, the same for every Chrome identity.

So only values that real Chrome users also vary are changed:

- timezone, language and geolocation, matched to the proxy exit IP;
- WebRTC, kept off the direct route when a proxy is set;
- the size of the real window.

The user agent, client hints and platform stay as Chrome reports them. An
override would contradict the TLS fingerprint and the engine underneath.

A DIRECT identity changes nothing but its window size: the host timezone and
language already match the host IP.
"""

import json
import os
import random
import sys
from pathlib import Path

from . import personas

CHANNEL = "chrome"

# Where Playwright looks for channel="chrome".
if sys.platform == "darwin":
    CHROME_PATHS = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
elif sys.platform == "win32":
    CHROME_PATHS = [
        Path(os.environ.get(var, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
        for var in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
        if os.environ.get(var)
    ]
else:
    CHROME_PATHS = [Path("/opt/google/chrome/chrome")]

# Smallest window drawn for an identity, clamped to the display.
MIN_WINDOW = (1280, 800)

# Share of identities whose window fills the work area, as a maximised one does.
MAXIMISED_SHARE = 0.4

# Playwright's --disable-features list, less the features whose absence a site
# can observe: ThirdPartyStoragePartitioning (on in real Chrome since 115),
# HttpsUpgrades (real Chrome upgrades http:// links) and PaintHolding. Chrome
# keeps the last --disable-features it is given, and user args come after
# Playwright's, so this replaces the default list. Compare it with
# chromiumSwitches.ts after a patchright upgrade: a feature Playwright adds
# there is dropped here until it is copied over.
DISABLED_FEATURES = (
    "AvoidUnnecessaryBeforeUnloadCheckSync",
    "DestroyProfileOnBrowserClose",
    "DialMediaRouteProvider",
    "GlobalMediaControls",
    "LensOverlay",
    "MediaRouter",
    "BlockOriginHeaderModificationOnRedirect",
    "Translate",
    "AutoDeElevate",
    "OptimizationHints",
)

# Playwright defaults a page can read. srgb hides a P3 display: on a MacBook,
# (color-gamut: p3) matched without it and did not match with it.
IGNORED_DEFAULT_ARGS = ["--force-color-profile=srgb"]

PROXIED_PREFS = {
    # Without this a page can read the real IP through WebRTC, past the proxy.
    "webrtc.ip_handling_policy": "disable_non_proxied_udp",
}

BASE_PREFS = {
    "credentials_enable_service": False,
    "profile.password_manager_enabled": False,
    # A killed browser otherwise opens with a "restore pages?" bubble.
    "profile.exit_type": "Normal",
    "profile.exited_cleanly": True,
}


def executable():
    """Path of the installed Google Chrome, or None."""
    return next((path for path in CHROME_PATHS if path.is_file()), None)


def problem():
    """Why Chrome identities cannot run on this machine, or None if they can."""
    if executable() is None:
        return "Google Chrome is not installed — install it from google.com/chrome"
    try:
        import patchright  # noqa: F401
    except ImportError:
        return "the patchright package is missing — re-run install.py"
    return None


def _work_area():
    """(left, top, width, height) of the host display's work area."""
    display = personas.host_display() or (1440, 900)
    left, top, reserved = personas.WORK_AREA[personas.HOST_OS]
    return left, top, display[0] - left, display[1] - reserved


def _window_size():
    """A window that fits the host work area: maximised, or a random smaller size."""
    _, _, width, height = _work_area()
    if random.random() < MAXIMISED_SHARE:
        return [width, height]
    return [
        random.randint(min(MIN_WINDOW[0], width), width),
        random.randint(min(MIN_WINDOW[1], height), height),
    ]


def _window_position(size):
    """A random top-left corner that keeps a window of `size` inside the work area.

    Without one every Chrome identity opened at the same screenX/screenY.
    """
    left, top, width, height = _work_area()
    return [
        random.randint(left, left + max(0, width - size[0])),
        random.randint(top, top + max(0, height - size[1])),
    ]


def _geolocate(proxy_config):
    """camoufox Geolocation of the exit IP behind proxy_config."""
    from camoufox.geolocation import get_geolocation
    from camoufox.ip import Proxy, public_ip

    return get_geolocation(public_ip(Proxy(**proxy_config).as_string()))


def _accept_languages(locale):
    """Chrome's language list for a locale: the locale, then its bare language."""
    tags = [locale.as_string, locale.language]
    return ",".join(dict.fromkeys(tags))


def generate_persona(ident, proxy):
    """Build the Chrome persona for identity `ident`; returns (persona, log_lines).

    A proxied identity takes timezone, language and geolocation from a GeoIP
    lookup of its exit IP, with the language pinned to the region's most-spoken
    one as for Camoufox (see personas.region_locale).
    """
    lines = []
    window = _window_size()
    persona = {"window": window, "position": _window_position(window)}
    size = "x".join(map(str, persona["window"]))
    proxy_config = personas.resolve_proxy(proxy, lines.append)
    if proxy_config is None:
        lines.append(f"{ident}: chrome  direct — host timezone and locale  window={size}")
        return persona, lines
    try:
        geo = _geolocate(proxy_config)
    except Exception as exc:  # proxy down, GeoIP DB missing, offline, etc.
        lines.append(
            f"warning: {ident}: GeoIP lookup through {personas.proxy_label(proxy)} "
            f"failed ({exc}); timezone and locale stay the host's, which may not "
            f"match the exit IP"
        )
        return persona, lines
    locale = personas.region_locale(geo.locale.region) or geo.locale
    persona.update(
        timezone=geo.timezone,
        locale=locale.as_string,
        languages=_accept_languages(locale),
        geolocation={
            "latitude": geo.latitude,
            "longitude": geo.longitude,
            "accuracy": geo.accuracy or 100,
        },
    )
    lines.append(
        f"{ident}: chrome  tz={geo.timezone}  loc={persona['languages']}  window={size}"
    )
    return persona, lines


def summary(persona):
    """Dashboard tile fields for a Chrome persona."""
    return {
        "os": personas.HOST_OS,
        "ua_tail": f"Google Chrome · {persona.get('locale') or 'host locale'}",
        "tz": persona.get("timezone") or "host",
    }


def _set_dotted(prefs, dotted, value):
    *parents, leaf = dotted.split(".")
    node = prefs
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value


def write_prefs(profile_dir, persona, proxied):
    """Merge the identity's prefs into profile_dir/Default/Preferences.

    Chrome rewrites the file on every run, so it is merged before each launch
    rather than written once at creation.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_file.read_text())
    except (OSError, ValueError):
        prefs = {}
    wanted = dict(BASE_PREFS)
    if proxied:
        wanted.update(PROXIED_PREFS)
    if persona.get("languages"):
        wanted["intl.accept_languages"] = persona["languages"]
        wanted["intl.selected_languages"] = persona["languages"]
    for dotted, value in wanted.items():
        _set_dotted(prefs, dotted, value)
    prefs_file.parent.mkdir(parents=True, exist_ok=True)
    prefs_file.write_text(json.dumps(prefs))


def launch_options(profile_dir, persona):
    """launch_persistent_context kwargs for one identity, proxy excluded.

    The language is set three ways because Chrome reads it from three places:
    the accept_languages pref gives navigator.languages and the Accept-Language
    header, while the default Intl locale comes from --lang on Windows and from
    LANG on Linux. macOS takes it from the system language, which neither
    reaches, so a proxied identity there can report an Intl locale that differs
    from navigator.language.

    The timezone goes through both TZ and the CDP override, so a worker that
    one of them misses still agrees with the page. TZ is left out on Windows,
    whose C runtime reads a different format.
    """
    size = persona.get("window") or _window_size()
    x, y = persona.get("position") or _window_position(size)
    args = [
        "--no-first-run",
        "--no-default-browser-check",
        # Hides the bar Chrome shows for Patchright's
        # --disable-blink-features=AutomationControlled. That flag must stay:
        # without it navigator.webdriver is true. The bar took 56px from the page.
        "--test-type",
        f"--window-size={size[0]},{size[1]}",
        f"--window-position={x},{y}",
        f"--disable-features={','.join(DISABLED_FEATURES)}",
    ]
    env = dict(os.environ)
    kwargs = {
        "user_data_dir": str(profile_dir),
        "channel": CHANNEL,
        "headless": False,
        # Patchright's advice, and the same reason as for Camoufox: an emulated
        # viewport would contradict the real window.
        "no_viewport": True,
        # Playwright adds --no-sandbox otherwise, and Chrome then shows an
        # info bar that also takes height from the page.
        "chromium_sandbox": True,
        "args": args,
        "ignore_default_args": IGNORED_DEFAULT_ARGS,
        "env": env,
    }
    locale = persona.get("locale")
    if locale:
        args.append(f"--lang={locale}")
        if sys.platform.startswith("linux"):
            env["LANG"] = env["LANGUAGE"] = f"{locale.replace('-', '_')}.UTF-8"
    timezone = persona.get("timezone")
    if timezone:
        kwargs["timezone_id"] = timezone
        if sys.platform != "win32":
            env["TZ"] = timezone
    if persona.get("geolocation"):
        # No permission is granted: a site that asks still gets Chrome's prompt.
        kwargs["geolocation"] = persona["geolocation"]
    return kwargs
