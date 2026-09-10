#!/usr/bin/env python3
"""
Generate one Camoufox persona per identity (called by ffid.sh create).

For each profiles/idN directory, asks the camoufox python package to build a
full fingerprint config (BrowserForge-generated, matching real-world device
distributions), doing a GeoIP lookup through that identity's SOCKS5 proxy so
timezone / locale / geolocation match the exit IP. The resulting CAMOU_CONFIG_*
environment variables are written to profiles/idN/persona.env, which ffid.sh
launch sources before starting the browser.

Run with the project venv: .venv/bin/python gen_personas.py [count]

The identity count comes from ffid.sh (FFID_SESSIONS, default 10). Proxies are
cycled modulo the entries in proxies.conf; the OS/screen presets cycle too,
but every identity still gets a unique randomly-generated BrowserForge
fingerprint.
"""

import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONF = ROOT / "proxies.conf"
PROFILES = ROOT / "profiles"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
if not 1 <= N <= 100:
    sys.exit(f"error: identity count must be 1-100 (got {N})")

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


def proxy_entries():
    return [
        line.strip()
        for line in CONF.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def main():
    from browserforge.fingerprints import Screen
    from camoufox.utils import launch_options

    entries = proxy_entries()
    if not entries:
        sys.exit("error: proxies.conf has no entries")

    for i in range(N):
        ident = f"id{i + 1}"
        out = PROFILES / ident / "persona.env"
        if not out.parent.is_dir():
            sys.exit(f"error: {out.parent} missing — run create first")

        os_persona = OS_PERSONAS[i % len(OS_PERSONAS)]
        w, h = SCREENS[i % len(SCREENS)]
        kwargs = {
            "os": os_persona,
            "screen": Screen(min_width=1024, max_width=w, min_height=700, max_height=h),
            "block_webrtc": True,
            "i_know_what_im_doing": True,
        }
        entry = entries[i % len(entries)]
        if entry == "DIRECT":
            print(f"{ident}: DIRECT — no GeoIP lookup, timezone will not match an exit IP")
        else:
            kwargs["proxy"] = {"server": f"socks5://{entry}"}
            kwargs["geoip"] = True  # looked up *through* the proxy

        try:
            opts = launch_options(**kwargs)
        except Exception as exc:  # proxy down, GeoIP DB missing, etc.
            if "geoip" not in kwargs:
                raise
            print(f"{ident}: GeoIP lookup failed ({exc}); falling back to random locale")
            del kwargs["geoip"]
            opts = launch_options(**kwargs)

        # env additions the launcher would have set (CAMOU_CONFIG_* chunks etc.)
        added = {
            k: str(v)
            for k, v in opts["env"].items()
            if k.startswith("CAMOU_")
        }
        if not added:
            sys.exit(f"error: no CAMOU_CONFIG generated for {ident}")

        with out.open("w") as fh:
            for key, value in sorted(added.items()):
                fh.write(f"export {key}={shlex.quote(value)}\n")

        cfg = json.loads("".join(v for k, v in sorted(added.items()) if k.startswith("CAMOU_CONFIG_")))
        ua = cfg.get("navigator.userAgent", "?")
        tz = cfg.get("timezone", cfg.get("int:timezone", "?"))
        print(f"{ident}: {os_persona:7s} tz={tz}  ua=...{ua[-40:]}")

    print(f"\n{N} personas written to {PROFILES}/idN/persona.env")


if __name__ == "__main__":
    main()
