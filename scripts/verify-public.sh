#!/usr/bin/env bash
# Privacy gate for the public repository.
#
# Scans every tracked (or, outside git, every regular) text file for the
# generic patterns in scripts/privacy-patterns.txt plus, when present, the
# maintainer's gitignored .privacy-patterns.local. CI runs this on every PR.
#
# Usage: scripts/verify-public.sh [root]

set -euo pipefail

ROOT="$(cd "${1:-$(dirname "$0")/..}" && pwd)"
cd "$ROOT"

pattern_files=("scripts/privacy-patterns.txt")
[[ -f .privacy-patterns.local ]] && pattern_files+=(".privacy-patterns.local")

patterns="$(mktemp)"
trap 'rm -f "$patterns"' EXIT
grep -h -v -E '^[[:space:]]*(#|$)' "${pattern_files[@]}" > "$patterns"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    files=()
    while IFS= read -r -d '' f; do files+=("$f"); done \
        < <(git ls-files -z --cached --others --exclude-standard)
else
    files=()
    while IFS= read -r -d '' f; do files+=("$f"); done \
        < <(find . -type f -not -path './.git/*' -print0)
fi

# The pattern files themselves necessarily contain the patterns.
filtered=()
for f in "${files[@]}"; do
    f="${f#./}"
    case "$f" in
        scripts/privacy-patterns.txt|.privacy-patterns.local) continue ;;
    esac
    [[ -f "$f" ]] && filtered+=("$f")
done

if rg -n -i --no-messages -f "$patterns" -- "${filtered[@]}"; then
    echo "ERROR: private or credential-like content found (see matches above)." >&2
    echo "       Patterns: ${pattern_files[*]}" >&2
    exit 1
fi

echo "Privacy gate passed: ${#filtered[@]} files, $(wc -l < "$patterns" | tr -d ' ') patterns (${pattern_files[*]})."
