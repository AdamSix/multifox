#!/bin/bash
# packaging/build_app.sh — build the multifox desktop app with PyInstaller
#
#   ./packaging/build_app.sh    build dist/multifox.app (+ a zip next to it)
#
# Architecture: the build matches the machine you run it on. For Apple
# Silicon build on an arm64 Mac, for Intel on an x86_64 Mac (or CI).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

[[ -x "$PY" ]] || { echo "error: .venv missing — run 'python3 install.py' first" >&2; exit 1; }
"$PY" -c "import PyInstaller" 2>/dev/null || "$PY" -m pip install --quiet pyinstaller

# stage the installed Camoufox browser + GeoIP DB + addons as a zip inside the
# bundle so the app installs offline (the launcher falls back to downloading
# if the payload is absent)
PAYLOAD_ZIP="$ROOT/build/bundle_payload.zip"
STAGE="$ROOT/build/payload_stage"
rm -f "$PAYLOAD_ZIP"
rm -rf "$STAGE"
CAMOU_CACHE=$("$PY" -c "from camoufox.pkgman import INSTALL_DIR; print(INSTALL_DIR)")
[[ -d "$CAMOU_CACHE/browsers" ]] || { echo "error: no camoufox browser in $CAMOU_CACHE — run '$PY -m camoufox fetch' first" >&2; exit 1; }
mkdir -p "$STAGE"
for part in browsers geoip addons; do
  if [[ -d "$CAMOU_CACHE/$part" ]]; then
    cp -Rc "$CAMOU_CACHE/$part" "$STAGE/$part" 2>/dev/null || cp -R "$CAMOU_CACHE/$part" "$STAGE/$part"
  fi
done
ditto -c -k --sequesterRsrc "$STAGE" "$PAYLOAD_ZIP"
rm -rf "$STAGE"
echo "staged $(du -sh "$PAYLOAD_ZIP" | cut -f1) browser payload zip into the bundle"

cd "$ROOT"
"$PY" -m PyInstaller --noconfirm --clean --distpath "$ROOT/dist" --workpath "$ROOT/build" "$ROOT/packaging/multifox.spec"

APP="$ROOT/dist/multifox.app"
[[ -d "$APP" ]] || { echo "error: $APP was not produced" >&2; exit 1; }

# zip for distribution (ditto preserves macOS metadata/permissions)
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ROOT/dist/multifox.zip"

echo
echo "built: $APP"
echo "zipped: $ROOT/dist/multifox.zip"
echo
echo "note: the app is unsigned. On another Mac it will refuse to open the"
echo "first time: right-click -> Open, or System Settings -> Privacy &"
echo "Security -> Open Anyway (or: xattr -dr com.apple.quarantine /path/to/multifox.app)"
