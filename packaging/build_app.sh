#!/bin/bash
# packaging/build_app.sh — build the multifox desktop app with PyInstaller
#
#   ./packaging/build_app.sh    build dist/multifox.app (+ a zip next to it)
#
# Architecture: the build matches the machine you run it on. For Apple
# Silicon build on an arm64 Mac, for Intel on an x86_64 Mac (or CI).
#
# Signing + notarization (optional, macOS only): set MULTIFOX_SIGN_IDENTITY
# to a "Developer ID Application: Name (TEAMID)" identity in your keychain to
# codesign the app with the hardened runtime. With no identity set the app
# builds unsigned, as before.
#
# To also notarize, set one of:
#   MULTIFOX_NOTARY_PROFILE   name of a profile stored via
#                              `xcrun notarytool store-credentials <name>`
#                              (local/interactive use)
#   APPLE_API_KEY_PATH, APPLE_API_KEY_ID, APPLE_API_ISSUER
#                              App Store Connect API key .p8 path + ids
#                              (headless/CI use)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

SIGN_IDENTITY="${MULTIFOX_SIGN_IDENTITY:-}"
ENTITLEMENTS="$ROOT/packaging/entitlements.plist"

# Sign every Mach-O under $1 bottom-up: loose dylibs/executables first, then
# nested .app/.framework bundles innermost-first (find -depth is post-order),
# ending with $1 itself if it is a bundle. Apple's notarizer requires the
# hardened runtime + a secure timestamp on every executable in the tree, not
# just the outer app — including binaries shipped as opaque data (e.g. the
# Camoufox browser payload zip, which the notarizer unpacks to check).
sign_tree() {
  local root="$1"
  while IFS= read -r -d '' bundle; do
    while IFS= read -r -d '' f; do
      file -b "$f" | grep -q "Mach-O" || continue
      codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$SIGN_IDENTITY" "$f"
    done < <(find "$bundle" -mindepth 1 \( -name "*.app" -o -name "*.framework" \) -prune -o -type f -print0)
    codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$SIGN_IDENTITY" "$bundle"
  done < <(find "$root" \( -name "*.app" -o -name "*.framework" \) -depth -print0)
}

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
if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "signing bundled Camoufox browser binaries as \"$SIGN_IDENTITY\""
  sign_tree "$STAGE"
fi
ditto -c -k --sequesterRsrc "$STAGE" "$PAYLOAD_ZIP"
rm -rf "$STAGE"
echo "staged $(du -sh "$PAYLOAD_ZIP" | cut -f1) browser payload zip into the bundle"

cd "$ROOT"
"$PY" -m PyInstaller --noconfirm --clean --distpath "$ROOT/dist" --workpath "$ROOT/build" "$ROOT/packaging/multifox.spec"

APP="$ROOT/dist/multifox.app"
[[ -d "$APP" ]] || { echo "error: $APP was not produced" >&2; exit 1; }

if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "signing $APP as \"$SIGN_IDENTITY\""
  sign_tree "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
else
  echo "note: MULTIFOX_SIGN_IDENTITY not set, building unsigned"
fi

# zip for distribution (ditto preserves macOS metadata/permissions)
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ROOT/dist/multifox.zip"

if [[ -n "$SIGN_IDENTITY" ]]; then
  NOTARY_ARGS=()
  if [[ -n "${MULTIFOX_NOTARY_PROFILE:-}" ]]; then
    NOTARY_ARGS=(--keychain-profile "$MULTIFOX_NOTARY_PROFILE")
  elif [[ -n "${APPLE_API_KEY_PATH:-}" ]]; then
    NOTARY_ARGS=(--key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER")
  fi

  if [[ ${#NOTARY_ARGS[@]} -gt 0 ]]; then
    echo "submitting $ROOT/dist/multifox.zip for notarization"
    xcrun notarytool submit "$ROOT/dist/multifox.zip" "${NOTARY_ARGS[@]}" --wait
    xcrun stapler staple "$APP"
    # re-zip so the distributed archive contains the stapled app
    rm -f "$ROOT/dist/multifox.zip"
    ditto -c -k --sequesterRsrc --keepParent "$APP" "$ROOT/dist/multifox.zip"
  else
    echo "note: no MULTIFOX_NOTARY_PROFILE or APPLE_API_KEY_* set, skipping notarization"
    echo "the app is signed but will still be flagged by Gatekeeper until notarized"
  fi
fi

echo
echo "built: $APP"
echo "zipped: $ROOT/dist/multifox.zip"
if [[ -z "$SIGN_IDENTITY" ]]; then
  echo
  echo "note: the app is unsigned. On another Mac it will refuse to open the"
  echo "first time: right-click -> Open, or System Settings -> Privacy &"
  echo "Security -> Open Anyway (or: xattr -dr com.apple.quarantine /path/to/multifox.app)"
fi
