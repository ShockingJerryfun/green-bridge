#!/usr/bin/env bash
# Scoped syntax/diff checks; no tests, pushes, or correctness claims.
# Usage: bash prepush_gate.sh [repo] [changed-file ...]
set -euo pipefail
REPO="${1:-.}"
if [ "$#" -gt 0 ]; then shift; fi
cd "$REPO"
cd "$(git rev-parse --show-toplevel)"
rc=0
cache=''
cleanup() { if [ -n "$cache" ]; then rm -rf -- "$cache"; fi; }
trap cleanup EXIT

echo '== Diff whitespace checks =='
git diff --check || rc=1
git diff --cached --check || rc=1
for file in "$@"; do
    if [ ! -f "$file" ]; then
        printf 'ERROR: selected file missing: %s\n' "$file" >&2
        rc=1
        continue
    fi
    case "$file" in
        *.py)
            if ! command -v python3 >/dev/null 2>&1; then
                echo 'ERROR: Python 3.8+ required for selected Python files' >&2
                rc=1
                continue
            fi
            if [ -z "$cache" ]; then cache="$(mktemp -d)"; fi
            printf 'Python syntax: %s\n' "$file"
            PYTHONPYCACHEPREFIX="$cache" python3 -m py_compile "$file" || rc=1
            if command -v pyflakes >/dev/null 2>&1; then
                pyflakes "$file" || rc=1
            else
                echo 'pyflakes: skipped (not installed)'
            fi
            ;;
        *.sh)
            printf 'Bash syntax: %s\n' "$file"
            bash -n "$file" || rc=1
            ;;
        *) printf 'No syntax checker configured for: %s\n' "$file" ;;
    esac
done
if [ "$#" -eq 0 ]; then echo 'No explicit files: syntax checks skipped'; fi

echo '== Working tree (informational; review task scope before staging) =='
git status --short
git log -1 --oneline 2>/dev/null || echo 'No commit yet'
if up="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)"; then
    printf 'Cached upstream: %s; ahead/behind: ' "$up"
    git rev-list --left-right --count "HEAD...$up"
fi
if [ "$rc" -eq 0 ]; then
    echo 'GATE: selected checks found no issue; not runtime/correctness evidence'
else
    echo 'GATE: findings; inspect before dispatch'
fi
exit "$rc"
