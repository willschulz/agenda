#!/usr/bin/env bash
# install.sh — build Agenda.app and install it to ~/Applications for local use.
#
# Usage:
#   ./install.sh                  # build + install
#   ./install.sh --no-build       # skip swift build, just re-bundle the existing binary
#   ./install.sh --replace-signed # allow replacing a signed (release/sync) Agenda
#
# The app bundles Agenda's server payload and vendored Python runtime, then
# launches the local server behind a persistent WKWebView.
#
# This is the DEVELOPMENT path: the bundle is ad-hoc signed for this Mac and
# carries no hardened runtime. For a distributable build, use
# scripts/package-release.sh instead.
#
# The installed copy REPLACES ~/Applications/Agenda.app and opens your real
# database. To try changes without that, use ../../scripts/dev.sh (web UI and
# server) or scripts/try-first-run.sh (a separate "Agenda Sandbox" app).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Agenda"
BINARY_NAME="Agenda"
APP_DEST="$HOME/Applications/$APP_NAME.app"
LEGACY_APP="$HOME/Applications/AgendaMac.app"

build=1
replace_signed=0
for arg in "$@"; do
    case "$arg" in
        --no-build) build=0 ;;
        --replace-signed) replace_signed=1 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

# --- refuse to silently replace a signed install ---
# A release (or maintainer sync) build carries a Team ID; an install.sh build
# doesn't. Replacing one with the other turns off iCloud sync and automatic
# updates without any visible sign, so ask for it explicitly.
if [[ -d "$APP_DEST" && "$replace_signed" == 0 ]]; then
    team="$(codesign -dv "$APP_DEST" 2>&1 | sed -n 's/^TeamIdentifier=//p')"
    if [[ -n "$team" && "$team" != "not set" ]]; then
        cat >&2 <<EOF
ERROR: ~/Applications/Agenda.app is a signed Agenda (Team ID $team), probably
the release you use every day. install.sh would replace it with an unsigned
development build that:
  - opens the same database (your real board),
  - has no iCloud sync, so your iPhone stops getting changes, and
  - no longer updates itself.

To try your changes without touching it:
  scripts/dev.sh                      (from the repo root: server + web UI on
                                       port 8551 with a scratch database)
  mac/Agenda/scripts/try-first-run.sh (a separate "Agenda Sandbox" app)

If you really mean to replace it, back up first (Settings → Reveal Backups in
Finder) and rerun with --replace-signed. Reinstall the release DMG afterwards
to get sync and updates back.
EOF
        exit 1
    fi
fi

# --- build ---
if [[ "$build" == 1 ]]; then
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
