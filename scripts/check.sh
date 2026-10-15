#!/usr/bin/env bash
# Local pre-publish check: tests, compile check, whitespace, secret scan.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
fail=0

note() { printf '\n==> %s\n' "$*"; }
bad() { printf 'FAIL: %s\n' "$*"; fail=1; }

note "tests"
python3 -W ignore::ResourceWarning -m unittest discover -s tests -t tests || bad "tests failed"

note "compile"
python3 -m compileall -q src bin/codex-auto-resume || bad "compileall failed"

note "manifests"
python3 scripts/check-manifests.py || bad "plugin packaging is invalid"

note "trailing whitespace"
if grep -rIn --exclude-dir=.git -E ' +$' . >/dev/null 2>&1; then
  grep -rIn --exclude-dir=.git -E ' +$' . | head -20
  bad "trailing whitespace found"
else
  echo "clean"
fi

note "secret scan"
scripts/secret-scan.sh || bad "secret scan found something"

note "forbidden patterns"
python3 scripts/check-no-last.py || bad "the implementation must never resume the most recent session"

if [ "$fail" = "0" ]; then
  printf '\ncheck: OK\n'
else
  printf '\ncheck: FAILED\n'
fi
exit "$fail"
