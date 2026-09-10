# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the multifox desktop app (Windows).

Build with:  .\\build_app.ps1    (produces dist/multifox/multifox.exe)
Separate from multifox.spec because BUNDLE (.app) is macOS-only.
"""

from PyInstaller.utils.hooks import collect_all

datas = [("static", "static"), ("proxies.conf", ".")]
binaries = []
hiddenimports = []

# bundled Camoufox browser + GeoIP DB + addons as a zip, staged by
# build_app.ps1 so the app installs offline with no first-run download.
import os.path

if os.path.isfile("build/bundle_payload.zip"):
    datas.append(("build/bundle_payload.zip", "bundle_payload"))

# packages with data files / native drivers that static analysis misses
for pkg in (
    "playwright",                  # node driver bundle
    "camoufox",
    "browserforge",
    "apify_fingerprint_datapoints",  # fingerprint data
    "screeninfo",
    "language_tags",
    "ua_parser",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["launcher.py"],
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
