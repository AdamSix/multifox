# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ff-sessions desktop app.

Build with:  ./build_app.sh        (macOS, produces dist/ff-sessions.app)
Per-architecture builds: run on an arm64 Mac for Apple Silicon, an Intel Mac
(or CI runner) for x86_64 — PyInstaller does not cross-compile.
"""

from PyInstaller.utils.hooks import collect_all

datas = [("static", "static"), ("proxies.conf", ".")]
binaries = []
hiddenimports = []

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
    name="ff-sessions",
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
    name="ff-sessions",
)
app = BUNDLE(
    coll,
    name="ff-sessions.app",
    icon=None,
    bundle_identifier="com.ffsessions.app",
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
    },
)
