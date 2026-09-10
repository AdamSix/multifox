#!/bin/bash
# build_app.sh — build the ff-sessions desktop app with PyInstaller
#
#   ./build_app.sh           build dist/ff-sessions.app (+ a zip next to it)
#
# Architecture: the build matches the machine you run it on. For Apple
# Silicon build on an arm64 Mac, for Intel on an x86_64 Mac (or CI).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"

[[ -x "$PY" ]] || { echo "error: .venv missing — run 'python3 install.py' first" >&2; exit 1; }
"$PY" -c "import PyInstaller" 2>/dev/null || "$PY" -m pip install --quiet pyinstaller

"$PY" -m PyInstaller --noconfirm --clean --distpath "$ROOT/dist" --workpath "$ROOT/build" "$ROOT/ff-sessions.spec"

APP="$ROOT/dist/ff-sessions.app"
[[ -d "$APP" ]] || { echo "error: $APP was not produced" >&2; exit 1; }

# zip for distribution (ditto preserves macOS metadata/permissions)
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ROOT/dist/ff-sessions.zip"

echo
echo "built: $APP"
echo "zipped: $ROOT/dist/ff-sessions.zip"
echo
echo "note: the app is unsigned. On another Mac, right-click -> Open the first"
echo "time (or run: xattr -dr com.apple.quarantine /path/to/ff-sessions.app)"
