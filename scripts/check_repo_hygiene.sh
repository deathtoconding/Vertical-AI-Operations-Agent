#!/usr/bin/env bash
# Cheap, fast checks that catch the mistakes humans and models actually make.
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0

# 1. Merge conflict markers
if git grep -nE '^(<<<<<<<|=======$|>>>>>>>)' -- ':!docs/backlog/jira-import.csv' 2>/dev/null; then
  echo "FAIL: merge conflict markers present"; fail=1
fi

# 2. Committed secrets by filename
if git ls-files | grep -E '(^|/)(\.env($|\.)|[^/]*\.pem|[^/]*\.key|id_rsa)$' | grep -v '\.example$'; then
  echo "FAIL: secret-looking files are tracked"; fail=1
fi

# 3. Large files (>2MB) that are not explicitly allowed
while read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    *.csv|*.json|*.lock) continue ;;
  esac
  size=$(wc -c < "$f")
  if [ "$size" -gt 2097152 ]; then echo "FAIL: $f is ${size} bytes"; fail=1; fi
done < <(git ls-files | grep -vE '\.(png|jpg|ico)$' || true)

# 4. No shell/exec primitives in agent-facing code (AI coding rule 7)
if git grep -nE '\b(os\.system|subprocess\.(run|Popen|call|check_output)|shell=True)\b' -- 'app/**' ':!app/sandbox/**' 2>/dev/null; then
  echo "FAIL: process execution found in application code"; fail=1
fi

# 5. No placeholder implementations presented as functionality (rule 5)
if git grep -nE 'TODO: implement|FIXME: placeholder|raise NotImplementedError$' -- 'app/**' 2>/dev/null; then
  echo "FAIL: placeholder implementation found"; fail=1
fi

# 6. The ignore rules must not swallow source. An unanchored `logs/` in .gitignore silently
#    excluded `app/integrations/logs/` from the repository while it still existed in a working
#    tree: it passed every local test and was missing from every clone and container build.
#    Anything git does not carry does not ship, so this is checked before anything is committed.
ignored_sources="$(git check-ignore --stdin < <(find app tests evals scripts -name '*.py'   -not -path '*__pycache__*' | sort) 2>/dev/null || true)"
if [ -n "$ignored_sources" ]; then
  echo "FAIL: .gitignore excludes source files, so they would not be shipped:"
  echo "$ignored_sources"
  fail=1
fi

# 7. The documented provider paths must exist (OPS-020…OPS-023). A backlog artefact that is not in
#    the tree is a plan that has drifted from the code.
while read -r provider; do
  [ -e "$provider" ] || { echo "FAIL: documented integration provider is missing: $provider"; fail=1; }
done < <(grep -rhoE 'app/integrations/[a-z]+/[a-z_]+\.py' docs/planning/domain.md | sort -u)

exit $fail
