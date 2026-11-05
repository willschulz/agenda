#!/usr/bin/env bash
# assemble-app.sh — lay out a complete Agenda.app bundle at <dest>.
#
# Assembly only: it does not build, sign, or launch anything. Both
# install.sh (development) and scripts/package-release.sh (distribution)
# call this, so the bundle you test every day and the bundle that ships are
# structurally identical. Layout drift between the two is the kind of bug
# that only shows up on someone else's Mac.
#
# Usage: scripts/assemble-app.sh /path/to/Agenda.app

set -euo pipefail

DEST="${1:-}"
if [[ -z "$DEST" ]]; then
    echo "usage: $0 <dest-app-path>" >&2
    exit 2
fi

PKG_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # mac/Agenda
REPO_DIR="$(cd "$PKG_DIR/../.." && pwd)"      # agenda
BINARY_NAME="Agenda"
BINARY_SRC="$PKG_DIR/.build/release/$BINARY_NAME"
AI_HELPER_SRC="$PKG_DIR/.build/release/AgendaAIHelper"
PLIST_SRC="$PKG_DIR/Sources/Agenda/Info.plist"
ICON_SRC="$PKG_DIR/Sources/Agenda/AppIcon.icns"
PYTHON_SRC="$PKG_DIR/vendor/python"
SPARKLE_SRC="$PKG_DIR/.build/release/Sparkle.framework"
AGENT_PACK_SRC="$REPO_DIR/agent-integration"

if [[ ! -f "$BINARY_SRC" ]]; then
    echo "ERROR: binary not found at $BINARY_SRC" >&2
    echo "       run: (cd $PKG_DIR && swift build -c release)" >&2
    exit 1
fi
if [[ ! -x "$AI_HELPER_SRC" ]]; then
    echo "ERROR: local AI helper not found at $AI_HELPER_SRC" >&2
    echo "       run: (cd $PKG_DIR && swift build -c release)" >&2
    exit 1
fi
if [[ ! -f "$PLIST_SRC" ]]; then
    echo "ERROR: Info.plist not found at $PLIST_SRC" >&2
    exit 1
fi
if [[ ! -x "$PYTHON_SRC/bin/python3" ]]; then
    "$PKG_DIR/scripts/vendor-python.sh"
fi
if [[ ! -d "$SPARKLE_SRC" ]]; then
    echo "ERROR: Sparkle.framework not found at $SPARKLE_SRC" >&2
    echo "       run: (cd $PKG_DIR && swift build -c release)" >&2
    exit 1
fi
if [[ ! -x "$AGENT_PACK_SRC/scripts/verify-pack.sh" ]]; then
    echo "ERROR: agent integration verifier is missing." >&2
    exit 1
fi
"$AGENT_PACK_SRC/scripts/verify-pack.sh"

rm -rf "$DEST"
mkdir -p "$DEST/Contents/MacOS" "$DEST/Contents/Resources" "$DEST/Contents/Frameworks"
cp "$BINARY_SRC" "$DEST/Contents/MacOS/$BINARY_NAME"
mkdir -p "$DEST/Contents/Resources/bin"
cp "$AI_HELPER_SRC" "$DEST/Contents/Resources/bin/AgendaAIHelper"
chmod 755 "$DEST/Contents/Resources/bin/AgendaAIHelper"
cp "$PLIST_SRC" "$DEST/Contents/Info.plist"
if [[ -f "$ICON_SRC" ]]; then
    cp "$ICON_SRC" "$DEST/Contents/Resources/AppIcon.icns"
fi

cp -R "$PYTHON_SRC" "$DEST/Contents/Resources/python"
cp -R "$SPARKLE_SRC" "$DEST/Contents/Frameworks/Sparkle.framework"
mkdir -p "$DEST/Contents/Resources/server"
cp "$REPO_DIR/main.py" "$REPO_DIR/db.py" "$REPO_DIR/seed.py" \
   "$REPO_DIR/board_order.py" \
   "$REPO_DIR/insights.py" "$REPO_DIR/sync_tombstones.py" \
   "$REPO_DIR/sync_recovery.py" "$REPO_DIR/sync_contract.py" \
   "$REPO_DIR/todo_structure.py" \
   "$REPO_DIR/usage_collect.py" "$REPO_DIR/usage_ledger.py" \
   "$REPO_DIR/usage_quota.py" "$REPO_DIR/usage_statusline.py" \
   "$REPO_DIR/usage_cursor.py" \
   "$REPO_DIR/pricing.json" \
   "$DEST/Contents/Resources/server/"
cp -R "$REPO_DIR/static" "$DEST/Contents/Resources/server/static"
mkdir -p "$DEST/Contents/Resources/agent-integration"
cp "$AGENT_PACK_SRC/README.md" "$DEST/Contents/Resources/agent-integration/"
cp -R "$AGENT_PACK_SRC/docs" "$AGENT_PACK_SRC/cursor" \
   "$AGENT_PACK_SRC/claude" "$DEST/Contents/Resources/agent-integration/"

# codesign refuses to sign a bundle carrying extended attributes ("resource
# fork, Finder information, or similar detritus not allowed in a bundle"), and
# quarantine flags inherited from the downloaded CPython archive count. Strip
# them here so both callers get a signable tree.
#
# Deliberately NOT removing the vendored runtime's __pycache__ trees: the app
# runs with PYTHONDONTWRITEBYTECODE=1, so deleted bytecode would never be
# regenerated and every launch would pay full stdlib compile cost.
find "$DEST" -name '.DS_Store' -delete 2>/dev/null || true
xattr -cr "$DEST" 2>/dev/null || true

echo "==> Assembled: $DEST"
