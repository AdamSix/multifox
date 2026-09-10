"""
Generate one Camoufox persona per identity (called by core.create_profiles).

Asks the camoufox python package to build a full fingerprint config
(BrowserForge-generated, matching real-world device distributions). When the
identity has a SOCKS5 proxy, a GeoIP lookup goes through that proxy so
timezone / locale / geolocation match the exit IP. The resulting CAMOU_*
environment variables are stored in the identity's profile.json and set on
the browser process at launch.

The OS/screen presets cycle per identity, but every identity still gets a
unique randomly-generated BrowserForge fingerprint.
"""

import json

# OS mix roughly matching real-world desktop market share.
OS_PERSONAS = ["windows"] * 7 + ["macos"] * 2 + ["linux"]

# Screen size ceilings, cycled per identity. Without a constraint the generator
# clamps to the HOST screen, which would make a spoofed-Windows identity claim
# a MacBook resolution — a consistency tell. (Exact min==max constraints are
# too strict for the generator, so these are ranges.)
SCREENS = [
    (1920, 1080), (1920, 1080), (1366, 768), (1536, 864), (1920, 1080),
    (1440, 900), (2560, 1440), (1366, 768), (1600, 900), (1920, 1080),
]


def camou_config(env):
    """Decode the CAMOU_CONFIG_* chunks of a persona env back into one dict."""
    chunks = "".join(v for k, v in sorted(env.items()) if k.startswith("CAMOU_CONFIG_"))
    return json.loads(chunks) if chunks else {}


def generate_persona(index, proxy):
    """Build the CAMOU_* env for identity `index` (0-based); returns (env, log_lines).

    proxy is a proxies.conf entry ('host:port' or 'DIRECT').
    """
    from browserforge.fingerprints import Screen
    from camoufox.utils import launch_options

    ident = f"id{index + 1}"
    lines = []
    os_persona = OS_PERSONAS[index % len(OS_PERSONAS)]
    w, h = SCREENS[index % len(SCREENS)]
    kwargs = {
        "os": os_persona,
        "screen": Screen(min_width=1024, max_width=w, min_height=700, max_height=h),
        "block_webrtc": True,
        "i_know_what_im_doing": True,
    }
    if proxy == "DIRECT":
        lines.append(f"{ident}: DIRECT — no GeoIP lookup, timezone will not match an exit IP")
    else:
        kwargs["proxy"] = {"server": f"socks5://{proxy}"}
        kwargs["geoip"] = True  # looked up *through* the proxy

    try:
        opts = launch_options(**kwargs)
    except Exception as exc:  # proxy down, GeoIP DB missing, etc.
        if "geoip" not in kwargs:
            raise
        lines.append(f"{ident}: GeoIP lookup failed ({exc}); falling back to random locale")
        del kwargs["geoip"]
        opts = launch_options(**kwargs)

    env = {k: str(v) for k, v in opts["env"].items() if k.startswith("CAMOU_")}
    if not env:
        raise RuntimeError(f"no CAMOU_CONFIG generated for {ident}")

    cfg = camou_config(env)
    ua = cfg.get("navigator.userAgent", "?")
    tz = cfg.get("timezone", cfg.get("int:timezone", "?"))
    lines.append(f"{ident}: {os_persona:7s} tz={tz}  ua=...{ua[-40:]}")
    return env, lines
