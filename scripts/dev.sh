#!/usr/bin/env bash
# dev.sh — run Agenda's server and web UI from this checkout, safely.
#
# This is the everyday development loop. It never touches your installed
# Agenda app or its database:
#   - data lives in ./data/dev.db (gitignored), seeded with the starter board
#     the first time;
#   - the server listens on 127.0.0.1:8551 (your installed Agenda uses 8552);
#   - Python comes from the same pinned runtime the app bundles
#     (mac/Agenda/vendor/python), downloaded on first use. No Xcode needed.
#
# Python changes reload automatically. For changes to static/index.html,
# reload the browser page.
#
# Usage:
#   scripts/dev.sh            # start (seed ./data/dev.db if it doesn't exist)
#   scripts/dev.sh --fresh    # delete ./data/dev.db first, then start
#   scripts/dev.sh --no-open  # don't open the browser
#   AGENDA_DEV_PORT=8553 scripts/dev.sh   # use another port (never 8552)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${AGENDA_DEV_PORT:-8551}"
DB="$ROOT/data/dev.db"
open_browser=1
fresh=0

for arg in "$@"; do
    case "$arg" in
        --fresh) fresh=1 ;;
        --no-open) open_browser=0 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

if [[ "$PORT" == "8552" || "$PORT" == "8562" ]]; then
    echo "ERROR: port $PORT belongs to the installed Agenda (8552) or Agenda Sandbox (8562)." >&2
    echo "       Pick another port, e.g. AGENDA_DEV_PORT=8553." >&2
    exit 1
fi

PY="$ROOT/mac/Agenda/vendor/python/bin/python3"
STAMP="$ROOT/mac/Agenda/vendor/.requirements.sha256"
want="$(shasum -a 256 requirements.txt | awk '{print $1}')"
if [[ ! -x "$PY" ]] || [[ "$(cat "$STAMP" 2>/dev/null)" != "$want" ]]; then
    echo "==> Setting up Agenda's pinned Python (one-time download, about a minute)…"
    "$ROOT/mac/Agenda/scripts/vendor-python.sh"
    echo "$want" > "$STAMP"
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: something is already listening on port $PORT." >&2
    echo "       Another dev server? Stop it, or use AGENDA_DEV_PORT=<port>." >&2
    exit 1
fi

mkdir -p "$ROOT/data"
if [[ "$fresh" == 1 ]]; then
    rm -f "$DB" "$DB-wal" "$DB-shm"
fi
export AGENDA_DB_PATH="$DB"
if [[ ! -f "$DB" ]]; then
    echo "==> Creating a development board at data/dev.db"
    PYTHONDONTWRITEBYTECODE=1 "$PY" seed.py
fi

URL="http://127.0.0.1:$PORT/"
echo "==> Agenda dev server: $URL  (data: data/dev.db; Ctrl-C to stop)"
if [[ "$open_browser" == 1 ]]; then
    ( sleep 2; open "$URL" ) &
fi
PYTHONDONTWRITEBYTECODE=1 exec "$PY" -m uvicorn main:app --reload \
    --host 127.0.0.1 --port "$PORT"
