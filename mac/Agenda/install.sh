#!/usr/bin/env bash
# install.sh — build Agenda.app and install it to ~/Applications for local use.
#
# Usage:
#   ./install.sh             # build + install
#   ./install.sh --no-build  # skip swift build, just re-bundle the existing binary
#
# The app bundles Agenda's server payload and vendored Python runtime, then
# launches the local server behind a persistent WKWebView.
#
# This is the DEVELOPMENT path: the bundle is ad-hoc signed for this Mac and
# carries no hardened runtime. For a distributable build, use
# scripts/package-release.sh instead.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Agenda"
BINARY_NAME="Agenda"
APP_DEST="$HOME/Applications/$APP_NAME.app"
LEGACY_APP="$HOME/Applications/AgendaMac.app"

# --- build ---
if [[ "${1:-}" != "--no-build" ]]; then
    echo "==> Building release binary…"
    (cd "$SCRIPT_DIR" && swift build -c release)
fi

# --- stop running instance ---
# "AgendaMac" is the pre-rename name; both would contend for port 8552.
for name in "$BINARY_NAME" "AgendaMac"; do
    if pgrep -qx "$name" 2>/dev/null; then
        echo "==> Stopping running instance ($name)…"
        pkill -x "$name" 2>/dev/null || true
    fi
done
PID_FILE="$HOME/Library/Application Support/Agenda/server.pid"
if [[ -f "$PID_FILE" ]]; then
    server_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$server_pid" =~ ^[0-9]+$ ]] && kill -0 "$server_pid" 2>/dev/null; then
        kill "$server_pid" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$server_pid" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$server_pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi
sleep 0.2

# --- retire the pre-rename bundle ---
if [[ -d "$LEGACY_APP" ]]; then
    echo "==> Removing superseded ${LEGACY_APP}…"
    rm -rf "$LEGACY_APP"
fi

# --- assemble bundle (shared with the release pipeline) ---
echo "==> Assembling $APP_NAME.app in ~/Applications…"
"$SCRIPT_DIR/scripts/assemble-app.sh" "$APP_DEST"

# --- ad-hoc sign ---
# No --deep: Apple discourages it for signing because it cannot apply
# per-binary entitlements, and the vendored Mach-O files already carry valid
# ad-hoc linker signatures. --deep remains correct for *verification*.
echo "==> Signing (ad-hoc)…"
codesign --sign - --force "$APP_DEST"
codesign --verify --deep --strict "$APP_DEST"

echo "==> Installed: $APP_DEST"
echo "==> Launching…"
open "$APP_DEST"
