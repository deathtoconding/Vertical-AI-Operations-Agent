#!/usr/bin/env bash
# Secret scan used by `make security` and the security workflow (SEC-005).
#
# gitleaks does the deep scan in CI (git history, entropy, provider patterns). This script is
# the offline, dependency-free guard that also runs in sandboxes without network access: it
# greps the tracked working tree for credential-shaped literals and fails if it finds one.
set -euo pipefail

cd "$(dirname "$0")/.."

PATTERNS='(BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.)'
# `AIOPS_..._TOKEN=` with a literal value is allowed only for obvious placeholders.
TOKEN_ASSIGNMENT='AIOPS_[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|KEY)[A-Z0-9_]*=[^"'"'"'$<{[:space:]]{8,}'

fail=0
while IFS= read -r -d '' file; do
    case "$file" in
        ./.git/*|./.venv/*|./.pgdata/*|./artifacts/*|./node_modules/*) continue ;;
        # Example/template files contain development placeholders by design and are never
        # loaded by the application; they are reviewed as documentation instead.
        *.example|*.example.*) continue ;;
    esac
    # The redaction tests must feed the log and audit pipelines something that *looks* like a
    # credential — that is the behaviour under test. Those literals are unmistakably synthetic
    # (an alphabet run, an all-zero prefix), so they are excluded by shape rather than by
    # switching the rule off for a whole directory.
    SYNTHETIC='abc defghijklmnop|abcdefghijklmnopqrstuvwx|000000000000|1234567890$|sk-live-1234'
    SYNTHETIC="${SYNTHETIC// /}"
    if grep -IqnE "$PATTERNS" "$file"; then
        real="$(grep -InE "$PATTERNS" "$file" | grep -vE "$SYNTHETIC" || true)"
        if [ -n "$real" ]; then
            echo "SECRET-LIKE CONTENT: $file" >&2
            printf '%s\n' "$real" | sed 's/^/    /' >&2
            fail=1
        fi
    fi
    if grep -InE "$TOKEN_ASSIGNMENT" "$file" | grep -vE '(changeme|placeholder|example|your-|xxx|test-|<|\$\{)' >/dev/null 2>&1; then
        echo "HARD-CODED CREDENTIAL: $file" >&2
        grep -InE "$TOKEN_ASSIGNMENT" "$file" | sed 's/^/    /' >&2
        fail=1
    fi
done < <(find . -type f \
    -not -path './.git/*' -not -path './.venv/*' -not -path './.pgdata/*' \
    -not -path './artifacts/*' -not -path '*/__pycache__/*' -print0)

if [ "$fail" -ne 0 ]; then
    echo "secret scan FAILED" >&2
    exit 1
fi
echo "secret scan passed (no credential-shaped literals in the working tree)"
