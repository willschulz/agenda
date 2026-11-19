#!/usr/bin/env bash
# check.sh — run the same checks CI runs on a pull request.
#
# Usage:
#   scripts/check.sh           # everything that applies to this Mac
#   scripts/check.sh --quick   # Python tests + privacy gate only
#
# Steps whose tools are missing are reported as SKIPPED, not passed. CI runs
# every step on each pull request, so a skip here is not a failure, but it
# means CI is the first place that step runs.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

quick=0
[[ "${1:-}" == "--quick" ]] && quick=1

PY="$ROOT/mac/Agenda/vendor/python/bin/python3"
if [[ ! -x "$PY" ]]; then
    echo "==> Setting up Agenda's pinned Python (one-time download)…"
    "$ROOT/mac/Agenda/scripts/vendor-python.sh" || exit 1
    shasum -a 256 requirements.txt | awk '{print $1}' > "$ROOT/mac/Agenda/vendor/.requirements.sha256"
fi

results=()
failed=0

run() {
    local name="$1"; shift
    echo
    echo "==> $name"
    if "$@"; then
        results+=("PASS     $name")
    else
        results+=("FAIL     $name")
        failed=1
    fi
}

skip() {
    results+=("SKIPPED  $1 ($2)")
}

run "Python tests" env PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 "$PY" -m unittest discover -s tests

if command -v rg >/dev/null 2>&1; then
    run "Privacy gate" scripts/verify-public.sh
else
    skip "Privacy gate" "needs ripgrep: brew install ripgrep"
fi

if [[ "$quick" == 0 ]]; then
    if command -v swift >/dev/null 2>&1; then
        run "Swift: AgendaSyncCore" bash -c 'cd apple/AgendaSyncCore && swift test'
        run "Swift: macOS app" bash -c 'cd mac/Agenda && swift test'
    else
        skip "Swift tests" "needs Xcode or the Command Line Tools: xcode-select --install"
    fi
    if command -v rg >/dev/null 2>&1; then
        run "Agent integration pack" agent-integration/scripts/verify-pack.sh
    else
        skip "Agent integration pack" "needs ripgrep: brew install ripgrep"
    fi
fi

echo
echo "==> Summary"
printf '    %s\n' "${results[@]}"
exit "$failed"
