#!/usr/bin/env bash
# Minimal secret scan: fail if anything that looks like a credential is tracked.
#
# This is a guard rail, not a replacement for a real scanner: it looks for the
# credential shapes that could realistically end up in this repository.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

found=0

scan() {
  local label="$1" pattern="$2" allow="${3:-}"
  local hits
  hits="$(grep -rInE \
    --exclude-dir=.git \
    --exclude-dir=__pycache__ \
    --exclude=secret-scan.sh \
    "$pattern" . 2>/dev/null || true)"
  if [ -n "$allow" ] && [ -n "$hits" ]; then
    hits="$(printf '%s\n' "$hits" | grep -vE "$allow" || true)"
  fi
  if [ -n "$hits" ]; then
    printf '\n%s:\n%s\n' "$label" "$hits"
    found=1
  fi
}

scan "OpenAI-style API keys" 'sk-[A-Za-z0-9_-]{20,}'
scan "GitHub tokens" 'gh[pousr]_[A-Za-z0-9]{20,}'
scan "AWS access key ids" 'AKIA[0-9A-Z]{16}'
scan "Slack tokens" 'xox[baprs]-[A-Za-z0-9-]{10,}'
scan "Private key blocks" 'BEGIN (RSA|EC|OPENSSH|PGP) PRIVATE KEY'
scan "Bearer tokens" 'Bearer [A-Za-z0-9._-]{20,}'
scan "JWT-looking strings" 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.'
scan "assigned secrets" '(password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)[[:space:]]*[=:][[:space:]]*.[^"'"'"'[:space:]]{12,}' 'super-secret'

# Personal absolute paths: allow the documented placeholders only.
scan "absolute personal paths" '/(home|Users)/[A-Za-z0-9._-]+/' '/(home|Users)/(you|your|user|someone)/'
scan "personal email addresses" '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' '@users\.noreply\.github\.com|@github\.com|@example\.(com|org|net)|@chatgpt\.com|@openai\.com|@agentplugins\.org'

if [ "$found" = "1" ]; then
  echo
  echo "secret scan: FAILED"
  exit 1
fi
echo "secret scan: clean"
