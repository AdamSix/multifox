"""
Generate one Camoufox persona per identity (called by core.create_profiles).

Asks the camoufox python package to build a full fingerprint config
(BrowserForge-generated, matching real-world device distributions). A GeoIP
lookup runs through the identity's proxy, or over the direct connection when it
has none, so timezone / locale / geolocation match the exit IP. The resulting
CAMOU_* environment variables are stored in the identity's profile.json and set
on the browser process at launch.

The OS/screen presets cycle per identity, but every identity still gets a
unique randomly-generated BrowserForge fingerprint.
"""

import json

# OS mix roughly matching real-world desktop market share, without Linux:
# desktop Linux Firefox is a fraction of a percent of real traffic, and the
# Linux font set ships no base Latin family, so the persona cannot render text.
OS_PERSONAS = ["windows"] * 8 + ["macos"] * 2

# Real display resolutions, cycled per identity. The generator is asked for an
# exact size, because given a range it picks sizes no device ships (1376x774)
# and a resolution that does not exist is trivial for a page to check. Not every
# size has a matching fingerprint, so generation walks the list until one works.
SCREENS = [
    (1920, 1080), (1680, 1050), (1536, 864), (1440, 900), (2560, 1440),
    (1920, 1200), (1600, 900), (1366, 768), (1280, 800), (2560, 1600),
]

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


def _generate(index, os_persona, kwargs):
    """launch_options with an exact screen size, trying SCREENS until one works."""
    from browserforge.fingerprints import Screen
    from camoufox.utils import launch_options

    for offset in range(len(SCREENS)):
        width, height = SCREENS[(index + offset) % len(SCREENS)]
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


def _patch(cfg, os_persona):
    """Repair the work area and fill in a missing oscpu, in place.

    Applied after generation rather than through launch_options(config=...):
    camoufox reads which domains the caller set and skips its own corrections
    for those, so passing one screen.* key would disable its screen clamping
    and taskbar fix wholesale.
    """
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


def generate_persona(index, proxy):
    """Build the CAMOU_* env for identity `index` (0-based); returns (env, log_lines).

    proxy is a proxies.conf entry ('host:port' or 'DIRECT').
    """
    ident = f"id{index + 1}"
    lines = []
    os_persona = OS_PERSONAS[index % len(OS_PERSONAS)]
    kwargs = {
        "os": os_persona,
        "block_webrtc": True,
        "i_know_what_im_doing": True,
        # Looked up through the proxy when there is one, over the direct
        # connection otherwise: a DIRECT identity that reports the host
        # timezone while claiming another region contradicts its own exit IP.
        "geoip": True,
    }
    if proxy != "DIRECT":
        kwargs["proxy"] = {"server": f"socks5://{proxy}"}

    try:
        opts = _generate(index, os_persona, kwargs)
    except NoUsableScreen:
        raise
    except Exception as exc:  # proxy down, GeoIP DB missing, offline, etc.
        lines.append(f"{ident}: GeoIP lookup failed ({exc}); falling back to random locale")
        del kwargs["geoip"]
        opts = _generate(index, os_persona, kwargs)

    env = {k: str(v) for k, v in opts["env"].items() if k.startswith("CAMOU_")}
    if not env:
        raise RuntimeError(f"no CAMOU_CONFIG generated for {ident}")

    cfg = camou_config(env)
    _patch(cfg, os_persona)
    env = _chunk(cfg, env)
    ua = cfg.get("navigator.userAgent", "?")
    tz = cfg.get("timezone", cfg.get("int:timezone", "?"))
    lines.append(f"{ident}: {os_persona:7s} tz={tz}  ua=...{ua[-40:]}")
    return env, lines
