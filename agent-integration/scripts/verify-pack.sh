#!/usr/bin/env bash
# Verify that only portable, privacy-safe agent material is publishable.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC_RELEASE_ROOT="$(cd "$ROOT/.." && pwd)/public-release"
PUBLISH_PATHS=(
    "$ROOT/README.md"
    "$ROOT/docs"
    "$ROOT/cursor"
    "$ROOT/claude"
    "$PUBLIC_RELEASE_ROOT/.github/ISSUE_TEMPLATE/bug_report.md"
)

required=(
    "$ROOT/README.md"
    "$ROOT/docs/API.md"
    "$ROOT/docs/CURSOR.md"
    "$ROOT/docs/CLAUDE_CODE.md"
    "$ROOT/cursor/.cursor/rules/agenda-todo-pickup.mdc"
    "$ROOT/cursor/.cursor/rules/agenda-todo-closeout.mdc"
    "$ROOT/claude/CLAUDE.md"
    "$PUBLIC_RELEASE_ROOT/.github/ISSUE_TEMPLATE/bug_report.md"
)

for path in "${required[@]}"; do
    if [[ ! -f "$path" ]]; then
        echo "ERROR: required agent-pack file is missing: $path" >&2
        exit 1
    fi
done

# Generic patterns are shared with the repo-wide gate; maintainer-specific
# terms live in the gitignored .privacy-patterns.local at the repo root.
REPO_ROOT="$(cd "$ROOT/.." && pwd)"
pattern_files=("$REPO_ROOT/scripts/privacy-patterns.txt")
local_patterns="$REPO_ROOT/.privacy-patterns.local"
# Linked worktrees lack the gitignored file; use the main checkout's copy.
if [[ ! -f "$local_patterns" ]] && common="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"; then
    local_patterns="$(dirname "$common")/.privacy-patterns.local"
fi
[[ -f "$local_patterns" ]] && pattern_files+=("$local_patterns")
forbidden=()
while IFS= read -r line; do forbidden+=("$line"); done \
    < <(grep -h -v -E '^[[:space:]]*(#|$)' "${pattern_files[@]}")
forbidden+=('\bjobfiles?\b' 'agenda/jobs')

failed=0
for pattern in "${forbidden[@]}"; do
    if rg -n -i --hidden --glob '!.DS_Store' "$pattern" "${PUBLISH_PATHS[@]}"; then
        echo "ERROR: forbidden public-pack pattern matched: $pattern" >&2
        failed=1
    fi
done

for phrase in '127.0.0.1:8552' 'in_progress' 'running'; do
    if ! rg -q --hidden --fixed-strings "$phrase" \
        "$ROOT/cursor" "$ROOT/claude"; then
        echo "ERROR: required workflow phrase is missing: $phrase" >&2
        failed=1
    fi
done

if (( failed )); then
    exit 1
fi

"$ROOT/scripts/verify-copy-prompt.py"

echo "Agent integration pack passed privacy and parity checks."
