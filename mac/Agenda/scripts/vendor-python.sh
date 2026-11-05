#!/usr/bin/env bash
# Download a pinned relocatable CPython and install Agenda's dependencies.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$APP_DIR/../.." && pwd)"
VENDOR_DIR="$APP_DIR/vendor"
PYTHON_DIR="$VENDOR_DIR/python"

ARCHIVE="cpython-3.13.14+20260805-aarch64-apple-darwin-install_only.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260805/cpython-3.13.14%2B20260805-aarch64-apple-darwin-install_only.tar.gz"
SHA256="b0ead4fbac288282c0806412103038789e0b2c79e45ff95758bb11761f6d3b0e"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/Library/Caches}/Agenda"
CACHE_FILE="$CACHE_DIR/$ARCHIVE"

if [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: this pinned runtime is arm64-only." >&2
    exit 1
fi

mkdir -p "$CACHE_DIR" "$VENDOR_DIR"

if [[ ! -f "$CACHE_FILE" ]]; then
    echo "==> Downloading CPython 3.13.14…"
    curl --fail --location --retry 3 --output "$CACHE_FILE.part" "$URL"
    mv "$CACHE_FILE.part" "$CACHE_FILE"
fi

actual="$(shasum -a 256 "$CACHE_FILE" | awk '{print $1}')"
if [[ "$actual" != "$SHA256" ]]; then
    rm -f "$CACHE_FILE"
    echo "ERROR: CPython archive checksum mismatch." >&2
    echo "expected: $SHA256" >&2
    echo "actual:   $actual" >&2
    exit 1
fi

echo "==> Extracting relocatable Python…"
rm -rf "$PYTHON_DIR"
tar -xzf "$CACHE_FILE" -C "$VENDOR_DIR"
[[ -x "$PYTHON_DIR/bin/python3" ]] || {
    echo "ERROR: archive did not produce vendor/python/bin/python3" >&2
    exit 1
}

echo "==> Installing Agenda Python dependencies…"
"$PYTHON_DIR/bin/python3" -m pip install \
    --disable-pip-version-check \
    --no-compile \
    --requirement "$REPO_DIR/requirements.txt"

echo "==> Vendored $("$PYTHON_DIR/bin/python3" -V 2>&1)"
echo "    $PYTHON_DIR"
