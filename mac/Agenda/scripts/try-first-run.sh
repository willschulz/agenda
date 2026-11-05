#!/usr/bin/env bash
# try-first-run.sh — experience Agenda's first launch without touching your
# real installation.
#
# Builds a separate "Agenda Sandbox.app" from this checkout and launches it
# with a brand-new, empty data folder, so you see exactly what a new user
# sees: the seeded starter board and the welcome tour. It can run alongside
# your normal Agenda.
#
# What keeps it isolated (see Sources/Agenda/AgendaRuntime.swift):
#   - its own bundle ID (com.willschulz.Agenda.Sandbox): separate preferences,
#     web storage, and single-instance guard;
#   - its own data folder (~/Library/Application Support/Agenda Sandbox) and
#     port (8562), so the real database at .../Application Support/Agenda and
#     the real server on 8552 are never opened, seeded, backed up, or stopped;
#   - ad-hoc signed with no entitlements, so iCloud sync is impossible, and
#     sandbox mode also refuses CloudKit, Sparkle updates, launch-at-login,
#     and keychain cleanup in code;
#   - installed to mac/Agenda/dist/sandbox/, never ~/Applications;
#   - its executable is named "Agenda Sandbox", so stopping the real app
#     (`pkill -x Agenda` in install.sh and the deploy steps) can't quit it.
#
# Usage:
#   scripts/try-first-run.sh            # build, wipe sandbox data, launch
#   scripts/try-first-run.sh --keep     # relaunch, keeping the sandbox's data
#   scripts/try-first-run.sh --no-build # skip `swift build` (reuse binary)
#   scripts/try-first-run.sh --clean    # quit the sandbox and delete its data

set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Agenda Sandbox"
BUNDLE_ID="com.willschulz.Agenda.Sandbox"
APP_DEST="$PKG_DIR/dist/sandbox/$APP_NAME.app"
DATA_DIR="$HOME/Library/Application Support/Agenda Sandbox"
REAL_DATA_DIR="$HOME/Library/Application Support/Agenda"
PORT=8562

build=1
wipe=1
for arg in "$@"; do
    case "$arg" in
        --no-build) build=0 ;;
        --keep) wipe=0 ;;
        --clean) build=-1 ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

# Safety rail: the data folder this script deletes must never be the real one.
if [[ "$DATA_DIR" == "$REAL_DATA_DIR" || "$DATA_DIR" != *"Agenda Sandbox" ]]; then
    echo "ERROR: refusing to use data folder $DATA_DIR" >&2
    exit 1
fi

quit_sandbox() {
    # Quit only the sandbox app (matched by bundle path), then its server
    # (matched by the sandbox port), never the real Agenda on 8552.
    pkill -f "$APP_DEST/Contents/MacOS/" 2>/dev/null || true
    local pids
    pids="$(lsof -nP -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
        kill $pids 2>/dev/null || true
    fi
    sleep 0.5
}

if (( build == -1 )); then
    quit_sandbox
    rm -rf "$DATA_DIR"
    defaults delete "$BUNDLE_ID" >/dev/null 2>&1 || true
    echo "==> Sandbox quit; its data and preferences are gone. Your real Agenda was not touched."
    exit 0
fi

if (( build == 1 )); then
    echo "==> Building release binary…"
    (cd "$PKG_DIR" && swift build -c release)
fi

quit_sandbox

echo "==> Assembling $APP_NAME.app in dist/sandbox/…"
mkdir -p "$(dirname "$APP_DEST")"
"$PKG_DIR/scripts/assemble-app.sh" "$APP_DEST"

PLIST="$APP_DEST/Contents/Info.plist"
mv "$APP_DEST/Contents/MacOS/Agenda" "$APP_DEST/Contents/MacOS/$APP_NAME"
/usr/libexec/PlistBuddy \
    -c "Set :CFBundleExecutable $APP_NAME" \
    -c "Set :CFBundleIdentifier $BUNDLE_ID" \
    -c "Set :CFBundleName $APP_NAME" \
    -c "Set :CFBundleDisplayName $APP_NAME" \
    -c "Add :AgendaSandbox bool true" \
    -c "Set :AgendaDefaultLaunchAtLogin false" \
    -c "Delete :SUFeedURL" \
    -c "Delete :SUEnableAutomaticChecks" \
    "$PLIST"

echo "==> Signing (ad-hoc, no entitlements: iCloud sync is impossible)…"
codesign --sign - --force "$APP_DEST"
codesign --verify --deep --strict "$APP_DEST"
if codesign -d --entitlements - --xml "$APP_DEST" 2>/dev/null | grep -q icloud; then
    echo "ERROR: sandbox build unexpectedly carries iCloud entitlements" >&2
    exit 1
fi

if (( wipe == 1 )); then
    echo "==> Starting from a brand-new data folder…"
    rm -rf "$DATA_DIR"
    defaults delete "$BUNDLE_ID" >/dev/null 2>&1 || true
fi

echo "==> Launching $APP_NAME (port $PORT, data in \"$DATA_DIR\")…"
open -n "$APP_DEST"
echo
echo "    Your real Agenda (port 8552, \"$REAL_DATA_DIR\") is untouched."
echo "    Run again for another fresh first launch; --clean removes the sandbox."
