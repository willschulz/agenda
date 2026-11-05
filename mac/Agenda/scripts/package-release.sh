#!/usr/bin/env bash
# package-release.sh — produce a distributable Agenda.app and DMG.
#
# Signs inside-out with hardened runtime, then notarizes and staples both the
# app and the DMG. Parameterized by signing identity so the same code path
# serves an ad-hoc dry run and a real Developer ID release.
#
# Usage:
#   scripts/package-release.sh                      # auto-detect identity
#   scripts/package-release.sh --identity -         # ad-hoc dry run
#   scripts/package-release.sh --no-build           # reuse .build/release binary
#   scripts/package-release.sh --skip-notarize      # sign and package only
#
# Options:
#   --identity <id>         codesign identity. Default: the first
#                           "Developer ID Application" in the keychain,
#                           falling back to "-" (ad-hoc) with a warning.
#   --notary-profile <name> notarytool keychain profile (default: agenda-notary)
#   --output <dir>          artifact directory (default: <pkg>/dist)
#   --version <v>           override the version used in artifact filenames
#   --entitlements <path>   override app entitlements (for Development CloudKit)
#   --provisioning-profile <path>
#                           embed an advanced-capability provisioning profile
#   --skip-notarize         never notarize, even if credentials exist
#   --no-build              skip `swift build -c release`
#
# See RELEASE.md for the two manual Apple steps this depends on.

set -euo pipefail

PKG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Agenda"
BINARY_NAME="Agenda"
APP_ENTITLEMENTS="$PKG_DIR/Agenda.entitlements"
PY_ENTITLEMENTS="$PKG_DIR/python.entitlements"
PROVISIONING_PROFILE=""

IDENTITY=""
NOTARY_PROFILE="agenda-notary"
OUTPUT_DIR="$PKG_DIR/dist"
VERSION=""
SKIP_NOTARIZE=0
DO_BUILD=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --identity)        IDENTITY="${2:?--identity needs a value}"; shift 2 ;;
        --notary-profile)  NOTARY_PROFILE="${2:?--notary-profile needs a value}"; shift 2 ;;
        --output)          OUTPUT_DIR="${2:?--output needs a value}"; shift 2 ;;
        --version)         VERSION="${2:?--version needs a value}"; shift 2 ;;
        --entitlements)    APP_ENTITLEMENTS="${2:?--entitlements needs a value}"; shift 2 ;;
        --provisioning-profile)
                            PROVISIONING_PROFILE="${2:?--provisioning-profile needs a value}"; shift 2 ;;
        --skip-notarize)   SKIP_NOTARIZE=1; shift ;;
        --no-build)        DO_BUILD=0; shift ;;
        -h|--help)         sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    esac
done

# --- resolve signing identity ---------------------------------------------
if [[ -z "$IDENTITY" ]]; then
    IDENTITY="$(
        security find-identity -v -p codesigning 2>/dev/null \
        | awk -F'"' '/Developer ID Application/ {print $2; exit}'
    )"
    if [[ -z "$IDENTITY" ]]; then
        IDENTITY="-"
        echo "WARNING: no Developer ID Application certificate found."
        echo "         Falling back to an ad-hoc (-) signature. This validates the"
        echo "         pipeline but is NOT distributable: Gatekeeper will reject it."
        echo "         See RELEASE.md to create the certificate."
        echo
    fi
fi

ADHOC=0
[[ "$IDENTITY" == "-" ]] && ADHOC=1

# codesign rejects --timestamp for ad-hoc signatures; a secure timestamp
# requires a real identity Apple's timestamp server can vouch for.
TIMESTAMP_FLAG="--timestamp"
(( ADHOC )) && TIMESTAMP_FLAG="--timestamp=none"

# --- decide whether notarization can run ----------------------------------
NOTARIZE=1
NOTARIZE_SKIP_REASON=""
if (( SKIP_NOTARIZE )); then
    NOTARIZE=0
    NOTARIZE_SKIP_REASON="--skip-notarize was passed"
elif (( ADHOC )); then
    NOTARIZE=0
    NOTARIZE_SKIP_REASON="the signature is ad-hoc; Apple only notarizes Developer ID builds"
elif ! xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
    # `security find-generic-password` cannot find this: notarytool profiles
    # are not stored as ordinary login-keychain generic passwords (they live
    # in the Local Items/iCloud keychain), so asking notarytool itself is the
    # only reliable existence check. A real API call is deliberate here, not
    # just an existence probe -- it also catches a stored profile whose
    # credentials Apple has since rejected.
    NOTARIZE=0
    NOTARIZE_SKIP_REASON="no working notarytool keychain profile named '$NOTARY_PROFILE' (see RELEASE.md step 2)"
fi

# --- preflight: entitlements must be strict XML ----------------------------
# codesign parses entitlements with AMFIUnserializeXML, which is stricter than
# `plutil -lint`. A double hyphen inside an XML comment is invalid XML: plutil
# accepts it, AMFI fails with "syntax error near line N", and you find out only
# after every nested binary has already been signed. A real XML parser catches
# the whole class in a second.
for ent in "$APP_ENTITLEMENTS" "$PY_ENTITLEMENTS"; do
    if ! /usr/bin/python3 -c 'import sys,xml.dom.minidom; xml.dom.minidom.parse(sys.argv[1])' \
            "$ent" 2>/dev/null; then
        echo "ERROR: $ent is not well-formed XML." >&2
        echo "       codesign will reject it. Most likely cause: a double hyphen" >&2
        echo "       inside an XML comment. Details:" >&2
        /usr/bin/python3 -c 'import sys,xml.dom.minidom; xml.dom.minidom.parse(sys.argv[1])' "$ent" \
            2>&1 | tail -1 >&2
        exit 1
    fi
done

# --- build ----------------------------------------------------------------
if (( DO_BUILD )); then
    echo "==> Building release binary…"
    (cd "$PKG_DIR" && swift build -c release)
fi

# --- assemble -------------------------------------------------------------
STAGE_DIR="$OUTPUT_DIR/stage"
APP_PATH="$STAGE_DIR/$APP_NAME.app"
SPARKLE_FRAMEWORK="$APP_PATH/Contents/Frameworks/Sparkle.framework"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"
"$PKG_DIR/scripts/assemble-app.sh" "$APP_PATH"
if [[ -n "$PROVISIONING_PROFILE" ]]; then
    cp "$PROVISIONING_PROFILE" "$APP_PATH/Contents/embedded.provisionprofile"
fi

VERSION="${VERSION:-$(
    /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
        "$APP_PATH/Contents/Info.plist"
)}"
ARCH="$(uname -m)"
DMG_PATH="$OUTPUT_DIR/$APP_NAME-$VERSION-$ARCH.dmg"

echo "==> Identity: $IDENTITY"
echo "==> Version:  $VERSION ($ARCH)"
[[ -z "$PROVISIONING_PROFILE" ]] || echo "==> Profile:  $PROVISIONING_PROFILE"

# --- sign inside-out ------------------------------------------------------
# A bundle signature seals the code nested inside it, so nested Mach-O files
# must be signed FIRST. Signing the outer bundle before its contents produces
# a seal that --verify --strict rejects.
#
# Detection reads Mach-O magic bytes rather than trusting file extensions:
# the vendored CPython tree carries binaries with no extension (bin/python3.13)
# alongside .so and .dylib, and a missed one fails notarization.
#
# Written for bash 3.2 (what macOS ships): no mapfile, and no `${arr[@]}` on a
# possibly-empty array under `set -u`. The list goes through a temp file.
echo "==> Locating nested Mach-O files…"
MACHO_LIST="$(mktemp -t agenda-macho)"
trap 'rm -f "$MACHO_LIST"' EXIT
/usr/bin/python3 - "$APP_PATH" "$SPARKLE_FRAMEWORK" > "$MACHO_LIST" <<'PY'
import os
import sys

MAGIC = {
    b"\xcf\xfa\xed\xfe",  # 64-bit little-endian
    b"\xce\xfa\xed\xfe",  # 32-bit little-endian
    b"\xfe\xed\xfa\xcf",  # 64-bit big-endian
    b"\xfe\xed\xfa\xce",  # 32-bit big-endian
    b"\xca\xfe\xba\xbe",  # universal (fat)
    b"\xbf\xba\xfe\xca",  # universal, reversed
}

root = sys.argv[1]
sparkle_root = os.path.realpath(sys.argv[2])
main_executable = os.path.join(root, "Contents", "MacOS", "Agenda")
found = []
for dirpath, _dirnames, filenames in os.walk(root):
    for name in filenames:
        path = os.path.join(dirpath, name)
        real_path = os.path.realpath(path)
        if (
            os.path.islink(path)
            or path == main_executable
            or real_path.startswith(sparkle_root + os.sep)
        ):
            continue
        try:
            with open(path, "rb") as handle:
                if handle.read(4) in MAGIC:
                    found.append(path)
        except OSError:
            continue

# Deepest paths first, so a nested library is always signed before whatever
# directory-level bundle might seal it.
found.sort(key=lambda p: (-p.count(os.sep), p))
print("\n".join(found))
PY

MACHO_COUNT="$(grep -c . "$MACHO_LIST" || true)"
echo "    $MACHO_COUNT nested Mach-O files to sign"
if [[ "$MACHO_COUNT" -eq 0 ]]; then
    echo "ERROR: found no nested Mach-O files. The bundle is not assembled as" >&2
    echo "       expected — signing the outer bundle alone would ship unsigned code." >&2
    exit 1
fi

while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    # The interpreter is a separate process and inherits nothing from the app,
    # so it carries its own (currently empty) entitlements.
    if [[ "$(basename "$file")" == python3.* ]]; then
        codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP_FLAG" \
            --entitlements "$PY_ENTITLEMENTS" "$file"
    else
        codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP_FLAG" "$file"
    fi
done < "$MACHO_LIST"

# Sparkle carries nested XPC and app bundles whose signatures must be sealed
# deepest-first. Keep it out of the generic Mach-O pass above: Downloader.xpc
# has entitlements that must be preserved, while the other components should
# not inherit them. This sequence follows Sparkle's manual distribution guide.
echo "==> Signing nested Sparkle bundles…"
SPARKLE_VERSION="$SPARKLE_FRAMEWORK/Versions/Current"
codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP_FLAG" \
    "$SPARKLE_VERSION/XPCServices/Installer.xpc"
codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP_FLAG" \
    --preserve-metadata=entitlements \
    "$SPARKLE_VERSION/XPCServices/Downloader.xpc"
codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP_FLAG" \
    "$SPARKLE_VERSION/Autoupdate"
codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP_FLAG" \
    "$SPARKLE_VERSION/Updater.app"
codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP_FLAG" \
    "$SPARKLE_FRAMEWORK"

# Signing the bundle also signs Contents/MacOS/Agenda, so the main executable
# needs no separate pass.
echo "==> Signing the app bundle…"
codesign --force --sign "$IDENTITY" --options runtime "$TIMESTAMP_FLAG" \
    --entitlements "$APP_ENTITLEMENTS" "$APP_PATH"

echo "==> Verifying signature…"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
codesign -dv --verbose=2 "$APP_PATH" 2>&1 | grep -E 'Identifier|TeamIdentifier|flags|Signature'

# --- notarize + staple the app -------------------------------------------
if (( NOTARIZE )); then
    ZIP_PATH="$OUTPUT_DIR/$APP_NAME-$VERSION-$ARCH.zip"
    echo "==> Submitting the app for notarization (this can take a few minutes)…"
    # ditto, not zip: it preserves symlinks and extended attributes, which the
    # vendored Python tree depends on.
    ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"
    xcrun notarytool submit "$ZIP_PATH" \
        --keychain-profile "$NOTARY_PROFILE" --no-s3-acceleration --wait
    rm -f "$ZIP_PATH"

    echo "==> Stapling the app…"
    # Staple the app as well as the DMG: the DMG ticket covers the download,
    # but once the user drags the app out and goes offline, only the app's own
    # stapled ticket keeps it launching without a Gatekeeper round-trip.
    xcrun stapler staple "$APP_PATH"
else
    echo "==> Skipping notarization: $NOTARIZE_SKIP_REASON"
fi

# --- build the DMG --------------------------------------------------------
echo "==> Building DMG…"
DMG_ROOT="$OUTPUT_DIR/dmg-root"
rm -rf "$DMG_ROOT"
mkdir -p "$DMG_ROOT"
cp -R "$APP_PATH" "$DMG_ROOT/$APP_NAME.app"
ln -s /Applications "$DMG_ROOT/Applications"

rm -f "$DMG_PATH"
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_ROOT" \
    -fs HFS+ \
    -format UDZO \
    -quiet \
    "$DMG_PATH"
rm -rf "$DMG_ROOT"

if (( ! ADHOC )); then
    echo "==> Signing the DMG…"
    codesign --force --sign "$IDENTITY" "$TIMESTAMP_FLAG" "$DMG_PATH"
fi

if (( NOTARIZE )); then
    echo "==> Notarizing the DMG…"
    xcrun notarytool submit "$DMG_PATH" \
        --keychain-profile "$NOTARY_PROFILE" --no-s3-acceleration --wait
    echo "==> Stapling the DMG…"
    xcrun stapler staple "$DMG_PATH"
fi

# --- report ---------------------------------------------------------------
echo
echo "==> Artifacts"
echo "    app: $APP_PATH"
echo "    dmg: $DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"
echo
if (( NOTARIZE )); then
    echo "==> Gatekeeper assessment"
    spctl -a -vvv -t install "$DMG_PATH" 2>&1 || true
    echo
    echo "Expected: 'accepted' with 'source=Notarized Developer ID'."
else
    echo "This build is NOT distributable ($NOTARIZE_SKIP_REASON)."
    echo "It verifies the packaging pipeline only."
fi
