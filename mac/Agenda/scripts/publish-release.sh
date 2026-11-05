#!/usr/bin/env bash
# publish-release.sh — publish a notarized DMG and its Sparkle appcast entry.
#
# package-release.sh must run first. This script does not build or sign code:
# it verifies the artifact, uploads it to the public releases-only repository,
# generates the EdDSA-signed appcast with Sparkle's Keychain key, and pushes
# that feed to GitHub Pages.
#
# Usage:
#   scripts/publish-release.sh [--notes /path/to/release-notes.md]

set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$(cd "$PKG_DIR/../.." && pwd)"
RELEASE_REPO="willschulz/agenda-releases"
NOTES_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --notes) NOTES_FILE="${2:?--notes needs a path}"; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done

PLIST="$PKG_DIR/Sources/Agenda/Info.plist"
VERSION="$(
    /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PLIST"
)"
BUILD="$(
    /usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$PLIST"
)"
ARCH="$(uname -m)"
DMG_PATH="$PKG_DIR/dist/Agenda-$VERSION-$ARCH.dmg"
AGENT_PACK_SRC="$REPO_DIR/agent-integration"
AGENT_PACK_ZIP="$PKG_DIR/dist/Agenda-agent-integration-$VERSION.zip"
PUBLIC_RELEASE_SRC="$REPO_DIR/public-release"
TAG="v$VERSION"
SPARKLE_TOOLS="$PKG_DIR/.build/artifacts/sparkle/Sparkle/bin"

if [[ ! -f "$DMG_PATH" ]]; then
    echo "ERROR: release DMG not found: $DMG_PATH" >&2
    echo "       run scripts/package-release.sh first" >&2
    exit 1
fi
if [[ ! -x "$SPARKLE_TOOLS/generate_appcast" ]]; then
    echo "ERROR: Sparkle tools are missing; run swift build -c release first." >&2
    exit 1
fi
if [[ -n "$NOTES_FILE" && ! -f "$NOTES_FILE" ]]; then
    echo "ERROR: release notes not found: $NOTES_FILE" >&2
    exit 1
fi
if [[ ! -x "$AGENT_PACK_SRC/scripts/verify-pack.sh" ]]; then
    echo "ERROR: agent integration verifier is missing." >&2
    exit 1
fi

echo "==> Verifying notarized release artifact…"
spctl -a -t install "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"
echo "==> Verifying public agent integration pack…"
"$AGENT_PACK_SRC/scripts/verify-pack.sh"

WORK="$(mktemp -d -t agenda-publish)"
trap 'rm -rf "$WORK"' EXIT
gh repo clone "$RELEASE_REPO" "$WORK/repo" -- --depth 1
mkdir -p "$WORK/feed" "$WORK/repo/docs"
cp -R "$PUBLIC_RELEASE_SRC/.github" "$WORK/repo/"
mkdir -p "$WORK/agent-integration"
cp "$AGENT_PACK_SRC/README.md" "$WORK/agent-integration/"
cp -R "$AGENT_PACK_SRC/docs" "$AGENT_PACK_SRC/cursor" \
    "$AGENT_PACK_SRC/claude" "$WORK/agent-integration/"
rm -f "$AGENT_PACK_ZIP"
ditto -c -k --keepParent "$WORK/agent-integration" "$AGENT_PACK_ZIP"
if [[ -f "$WORK/repo/docs/appcast.xml" ]]; then
    cp "$WORK/repo/docs/appcast.xml" "$WORK/feed/appcast.xml"
fi

ARCHIVE_NAME="$(basename "$DMG_PATH")"
cp "$DMG_PATH" "$WORK/feed/$ARCHIVE_NAME"
if [[ -n "$NOTES_FILE" ]]; then
    cp "$NOTES_FILE" "$WORK/feed/${ARCHIVE_NAME%.dmg}.md"
else
    cat > "$WORK/feed/${ARCHIVE_NAME%.dmg}.md" <<EOF
# Agenda $VERSION

Build $BUILD. See the GitHub release for details.
EOF
fi

echo "==> Generating EdDSA-signed appcast entry…"
"$SPARKLE_TOOLS/generate_appcast" \
    --download-url-prefix \
    "https://github.com/$RELEASE_REPO/releases/download/$TAG/" \
    --embed-release-notes \
    --maximum-deltas 0 \
    "$WORK/feed"

echo "==> Publishing GitHub Release ${TAG}…"
if gh release view "$TAG" --repo "$RELEASE_REPO" >/dev/null 2>&1; then
    gh release upload "$TAG" "$DMG_PATH" "$AGENT_PACK_ZIP" \
        --repo "$RELEASE_REPO" --clobber
else
    if [[ -n "$NOTES_FILE" ]]; then
        gh release create "$TAG" "$DMG_PATH" "$AGENT_PACK_ZIP" \
            --repo "$RELEASE_REPO" \
            --title "Agenda $VERSION" \
            --notes-file "$NOTES_FILE"
    else
        gh release create "$TAG" "$DMG_PATH" "$AGENT_PACK_ZIP" \
            --repo "$RELEASE_REPO" \
            --title "Agenda $VERSION" \
            --notes "Build $BUILD."
    fi
fi

cp "$WORK/feed/appcast.xml" "$WORK/repo/docs/appcast.xml"
(
    cd "$WORK/repo"
    git add docs/appcast.xml .github
    if git diff --cached --quiet; then
        echo "==> Appcast is already current."
    else
        git commit -m "Publish Agenda $VERSION"
        git push origin main
    fi
)

echo
echo "==> Published Agenda $VERSION (build $BUILD)"
echo "    release: https://github.com/$RELEASE_REPO/releases/tag/$TAG"
echo "    appcast: https://willschulz.github.io/agenda-releases/appcast.xml"
echo "    agent pack: $(basename "$AGENT_PACK_ZIP")"
