# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the multifox desktop app (Windows).

Build with:  .\packaging\build_app.ps1    (produces dist/multifox/multifox.exe)
Separate from multifox.spec because BUNDLE (.app) is macOS-only.
"""

from PyInstaller.utils.hooks import collect_all

import os.path

# repo root = parent of this spec file's packaging/ dir (SPECPATH is set by PyInstaller)
_specdir = os.path.abspath(SPECPATH)
if os.path.isfile(_specdir):
    _specdir = os.path.dirname(_specdir)
ROOT = os.path.dirname(_specdir)

datas = [(os.path.join(ROOT, "multifox/static"), "multifox/static"), (os.path.join(ROOT, "proxies.conf"), ".")]
binaries = []
hiddenimports = []

# bundled Camoufox browser + GeoIP DB + addons as a zip, staged by
# build_app.ps1 so the app installs offline with no first-run download.
payload = os.path.join(ROOT, "build/bundle_payload.zip")
if os.path.isfile(payload):
    datas.append((payload, "bundle_payload"))

# packages with data files / native drivers that static analysis misses
for pkg in (
    "playwright",                  # node driver bundle
    "camoufox",
    "browserforge",
    "apify_fingerprint_datapoints",  # fingerprint data
    "screeninfo",
    "language_tags",
    "ua_parser",
    "webview",                     # pywebview: native dashboard window
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "launcher.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pip", "setuptools"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="multifox",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="multifox",
)
