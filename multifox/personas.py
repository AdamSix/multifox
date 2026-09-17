"""
Generate one Camoufox persona per identity (called by core.create_profiles).

Asks the camoufox python package to build a full fingerprint config
(BrowserForge-generated, matching real-world device distributions). A GeoIP
lookup runs through the identity's proxy, or over the direct connection when it
has none, so timezone / locale / geolocation match the exit IP. The language of
that locale is then pinned to the region's most-spoken one (see
_patch_locale). The resulting CAMOU_* environment variables are stored in the
identity's profile.json and set on the browser process at launch.

The caller picks the least-used screen preset for each new identity, so the
presets stay spread as identities are added and removed. Every identity still
gets a unique randomly-generated BrowserForge fingerprint.
"""

import json
import random
import sys
import warnings
from collections import Counter

# The OS this copy of multifox is actually running on. Camoufox spoofs what
# JavaScript reads, but WebGL still renders through this process's real
# backend (Direct3D-ANGLE on Windows, native GL/Metal on macOS). A persona
# claiming a different OS is claiming a rendering backend it can never
# produce: measured on a macOS host, identities claiming a Windows GPU and
# identities claiming an Apple GPU rendered byte-identical WebGL output,
# proving neither actually went through the claimed backend. Restricting
# personas to the host's own OS removes that tell.
if sys.platform == "darwin":
    HOST_OS = "macos"
elif sys.platform == "win32":
    HOST_OS = "windows"
else:
    HOST_OS = "linux"

# OS mix per identity, restricted to the host OS so no persona claims a
# rendering backend this process cannot produce (see HOST_OS above).
#
# Linux hosts are the exception: a "linux" persona is not offered because its
# font set ships no base Latin family, so the page cannot render text. Falling
# back to the old cross-OS mix here still leaves the host-mismatch tell open
# on Linux specifically -- unresolved, not fixed.
if HOST_OS == "linux":
    OS_PERSONAS = ["windows"] * 8 + ["macos"] * 2
else:
    OS_PERSONAS = [HOST_OS]

# Real display resolutions, one per identity (core picks the least-used one).
# The generator is asked for an exact size, because given a range it picks
# sizes no device ships (1376x774), and a resolution that does not exist is
# trivial for a page to check. Not every size has a matching fingerprint, so
# generation walks the list until one works. Only sizes that fit the host
# display are offered (see _fitting_screens): the real window is sized to the
# persona's outer dimensions and the OS clamps it to the display, so a larger
# claim leaves innerWidth far below the spoofed outerWidth.
SCREENS = [
    (1920, 1080), (1680, 1050), (1536, 864), (1440, 900), (2560, 1440),
    (1920, 1200), (1600, 900), (1366, 768), (1280, 800), (2560, 1600),
    (1512, 982), (1728, 1117),
]

# Sizes no device of that OS ships. 1366x768 is a budget Windows panel and
# 1536x864 is 1920x1080 at 125% Windows scaling; a Mac reports neither, and
# at devicePixelRatio 2 they would imply panels that do not exist. 1512x982
# and 1728x1117 are the 14" and 16" MacBook Pro logical sizes, which no
# Windows laptop reports.
SCREENS_NOT_ON = {
    "macos": {(1366, 768), (1536, 864)},
    "windows": {(1512, 982), (1728, 1117)},
}

# Desktop chrome reserved per OS, as (availLeft, availTop, reserved height).
# Camoufox has its own work-area correction, but it still emits an availLeft
# equal to the screen width on some draws, which cannot happen: a work area
# cannot start at the right edge of the screen it fills.
WORK_AREA = {
    "windows": (0, 0, 40),
    "macos": (0, 25, 25),
    "linux": (0, 27, 27),
}

# Firefox reports a fixed oscpu per platform. The generator sometimes omits it
# altogether, and a missing property is a plainer signal than a wrong one.
OSCPU = {
    "windows": "Windows NT 10.0; Win64; x64",
    "macos": "Intel Mac OS X 10.15",
    "linux": "Linux x86_64",
}


# Camoufox splits the config across CAMOU_CONFIG_<n> at 2047 characters on
# Windows and 32767 elsewhere. Re-chunking at the smaller size is valid on
# every platform, so the patched config uses it unconditionally.
CONFIG_CHUNK = 2047


class NoUsableScreen(RuntimeError):
    """No size in SCREENS has a generatable fingerprint for this OS."""


def camou_config(env):
    """Decode the CAMOU_CONFIG_* chunks of a persona env back into one dict.

    Sorted by chunk number, not by name: camoufox splits the config at 2047
    characters on Windows, so a persona reaches CAMOU_CONFIG_10 and a plain
    sort would put it before CAMOU_CONFIG_2 and produce invalid JSON.
    """
    chunks = [
        (int(k.rsplit("_", 1)[1]), v)
        for k, v in env.items()
        if k.startswith("CAMOU_CONFIG_")
    ]
    return json.loads("".join(v for _, v in sorted(chunks))) if chunks else {}


def screen_ordinal(env):
    """Index in SCREENS of the screen a persona actually uses, or None if it is not one."""
    try:
        cfg = camou_config(env)
    except ValueError:
        return None
    size = (cfg.get("screen.width"), cfg.get("screen.height"))
    return SCREENS.index(size) if size in SCREENS else None


def _host_display():
    """(width, height) of the largest attached display in CSS pixels, or None."""
    try:
        from camoufox.display import largest_display

        display = largest_display()
    except Exception:  # older camoufox, no screen, enumeration failed
        return None
    return (display.width, display.height) if display else None


def _fitting_screens(os_persona):
    """SCREENS indices that exist on os_persona and fit the host display.

    Falls back to every size for the OS if none fits the display.
    """
    excluded = SCREENS_NOT_ON.get(os_persona, set())
    for_os = [i for i, size in enumerate(SCREENS) if size not in excluded]
    display = _host_display()
    if display is None:
        return for_os
    width, height = display
    fitting = [i for i in for_os if SCREENS[i][0] <= width and SCREENS[i][1] <= height]
    return fitting or for_os


def _screen_order(in_use, os_persona):
    """Fitting SCREENS indices, least-used first, ties by index.

    The whole order matters, not just the first choice: not every size has a
    fingerprint for every OS, so generation falls through to the next entry.
    Falling through to the next *least-used* one is what keeps the presets
    spread; walking the list in order landed two identities on the same size.
    """
    tally = Counter(o for o in in_use if o is not None)
    return sorted(_fitting_screens(os_persona), key=lambda i: (tally[i], i))


def _generate(order, os_persona, kwargs):
    """launch_options with an exact screen size, trying `order` until one works."""
    from browserforge.fingerprints import Screen
    from camoufox.utils import launch_options

    for index in order:
        width, height = SCREENS[index]
        attempt = dict(
            kwargs,
            screen=Screen(
                min_width=width, max_width=width, min_height=height, max_height=height
            ),
        )
        try:
            return launch_options(**attempt)
        except ValueError:
            continue  # no fingerprint for this size; try the next one
    raise NoUsableScreen(f"no usable screen size for os={os_persona}")


def _patch_locale(cfg):
    """Replace the persona language with the region's most-spoken one, in place.

    Camoufox draws the language at random from the CLDR speaker share of the
    GeoIP region, but that share counts who *speaks* a language, not who runs
    a browser in it: it gave 14% of UK identities fr-GB, which no real UK
    Firefox sends. Taking the top language instead is not a shared-value tell,
    because it is the value almost every real visitor from that region has.

    Region, script, timezone and geolocation still come from GeoIP, so an
    identity behind a proxy stays consistent with its exit IP.
    """
    from camoufox.locales import SELECTOR, normalize_locale

    region = cfg.get("locale:region")
    if not region:
        return
    try:
        # Private, but the public entry point picks at random by design.
        languages, weights = SELECTOR._load_territory_data(region)
        locale = normalize_locale(
            f"{str(languages[int(weights.argmax())]).replace('_', '-')}-{region}"
        )
    except Exception:  # unknown territory, no language data, camoufox change
        return
    cfg.update(locale.as_config())
    cfg["locale:all"] = f"{locale.as_string}, {locale.language}"


def _patch(cfg, os_persona):
    """Repair the work area, the locale, DNT and a missing oscpu, in place.

    Applied after generation rather than through launch_options(config=...):
    camoufox reads which domains the caller set and skips its own corrections
    for those, so passing one screen.* key would disable its screen clamping
    and taskbar fix wholesale.

    BrowserForge data predates Firefox 135, which removed Do Not Track, so it
    still hands out doNotTrack "1", and it gave every identity GPC, which
    Firefox only enables in private windows. The browser sends neither header
    (see core.FIREFOX_PREFS), so the JS values must say so too.
    """
    _patch_locale(cfg)
    cfg["navigator.doNotTrack"] = "unspecified"
    cfg["navigator.globalPrivacyControl"] = False
    cfg.setdefault("navigator.oscpu", OSCPU[os_persona])
    width, height = cfg.get("screen.width"), cfg.get("screen.height")
    if not width or not height:
        return
    left, top, reserved = WORK_AREA[os_persona]
    cfg["screen.availLeft"] = left
    cfg["screen.availTop"] = top
    cfg["screen.availWidth"] = width - left
    cfg["screen.availHeight"] = height - reserved
    for key, limit in (
        ("window.outerWidth", cfg["screen.availWidth"]),
        ("window.outerHeight", cfg["screen.availHeight"]),
    ):
        if cfg.get(key) and cfg[key] > limit:
            cfg[key] = limit
    for key, floor in (("window.screenX", left), ("window.screenY", top)):
        if cfg.get(key) is not None and cfg[key] < floor:
            cfg[key] = floor


def _chunk(cfg, env):
    """env with its CAMOU_CONFIG_* chunks replaced by a re-serialised cfg."""
    rebuilt = {k: v for k, v in env.items() if not k.startswith("CAMOU_CONFIG_")}
    blob = json.dumps(cfg)
    for start in range(0, len(blob), CONFIG_CHUNK):
        rebuilt[f"CAMOU_CONFIG_{start // CONFIG_CHUNK + 1}"] = blob[start : start + CONFIG_CHUNK]
    return rebuilt


def generate_persona(ident, proxy, screens_in_use=()):
    """Build the CAMOU_* env for identity `ident`; returns (env, log_lines).

    proxy is a proxies.conf entry ('host:port' or 'DIRECT'). screens_in_use is
    the screen_ordinal of every identity that already exists, so the new one
    takes a preset the others do not use.
    """
    lines = []
    os_persona = random.choice(OS_PERSONAS)
    order = _screen_order(screens_in_use, os_persona)
    kwargs = {
        "os": os_persona,
        "block_webrtc": True,
        # Looked up through the proxy when there is one, over the direct
        # connection otherwise: a DIRECT identity that reports the host
        # timezone while claiming another region contradicts its own exit IP.
        "geoip": True,
    }
    if proxy != "DIRECT":
        kwargs["proxy"] = {"server": f"socks5://{proxy}"}

    # Camoufox warns (LeakWarning) when a kwarg can make the persona
    # detectable. Those go to stderr by default, where nobody sees them; the
    # job log is where they belong.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            opts = _generate(order, os_persona, kwargs)
        except NoUsableScreen:
            raise
        except Exception as exc:  # proxy down, GeoIP DB missing, offline, etc.
            lines.append(f"{ident}: GeoIP lookup failed ({exc}); falling back to random locale")
            del kwargs["geoip"]
            opts = _generate(order, os_persona, kwargs)
    lines.extend(f"warning: {ident}: camoufox: {w.message}" for w in caught)

    env = {k: str(v) for k, v in opts["env"].items() if k.startswith("CAMOU_")}
    if not env:
        raise RuntimeError(f"no CAMOU_CONFIG generated for {ident}")

    cfg = camou_config(env)
    _patch(cfg, os_persona)
    env = _chunk(cfg, env)
    ua = cfg.get("navigator.userAgent", "?")
    tz = cfg.get("timezone", cfg.get("int:timezone", "?"))
    loc = cfg.get("locale:all", "?")
    lines.append(f"{ident}: {os_persona:7s} tz={tz}  loc={loc}  ua=...{ua[-40:]}")
    return env, lines
