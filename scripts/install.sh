#!/usr/bin/env bash
# Install codex-auto-resume into the local Codex CLI and user systemd.
#
# Steps:
#   1. put the CLI on PATH (~/.local/bin by default)
#   2. enable the Codex `plugins` feature if needed
#   3. add this checkout as a Codex plugin marketplace and install the plugin
#   4. persist hook trust through Codex's own config API
#   5. install and enable the user service that does the waiting
#
# Everything here is additive: no existing Codex configuration, plugin, hook,
# skill or MCP server is modified.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${CODEX_AUTO_RESUME_BIN_DIR:-$HOME/.local/bin}"
CODEX_BIN="${CODEX_AUTO_RESUME_CODEX_BIN:-$(command -v codex || true)}"
MARKETPLACE_NAME="codex-auto-resume"
PLUGIN_SELECTOR="codex-auto-resume@${MARKETPLACE_NAME}"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

if [ -z "$CODEX_BIN" ]; then
  echo "error: the 'codex' CLI was not found on PATH" >&2
  exit 1
fi

log "Codex: $CODEX_BIN ($("$CODEX_BIN" --version 2>/dev/null || echo 'version unknown'))"

log "installing the CLI into $BIN_DIR"
mkdir -p "$BIN_DIR"
ln -sfn "$REPO_ROOT/bin/codex-auto-resume" "$BIN_DIR/codex-auto-resume"
CLI="$BIN_DIR/codex-auto-resume"

if "$CODEX_BIN" features list 2>/dev/null | awk '$1 == "plugins" {print $3}' | grep -qx false; then
  log "enabling the Codex 'plugins' feature"
  "$CODEX_BIN" features enable plugins
fi

log "adding marketplace '$MARKETPLACE_NAME' ($REPO_ROOT)"
if "$CODEX_BIN" plugin marketplace list 2>/dev/null | grep -q "$MARKETPLACE_NAME"; then
  "$CODEX_BIN" plugin marketplace upgrade "$MARKETPLACE_NAME" >/dev/null 2>&1 || true
else
  "$CODEX_BIN" plugin marketplace add "$REPO_ROOT" || warn "marketplace add reported an error; continuing"
fi

log "installing plugin $PLUGIN_SELECTOR"
"$CODEX_BIN" plugin add "$PLUGIN_SELECTOR" --marketplace "$MARKETPLACE_NAME" --json >/dev/null 2>&1 \
  || "$CODEX_BIN" plugin add "$PLUGIN_SELECTOR" \
  || warn "plugin add reported an error; check 'codex plugin list'"

log "persisting hook trust"
"$CLI" trust-hooks

log "installing the user service"
"$CLI" install-service

# Without lingering, the user manager (and so the waiting helper) stops when the
# last login session ends. Needed for the wait to survive logout and reboot.
if command -v loginctl >/dev/null 2>&1; then
  if [ "$(loginctl show-user "$(id -un)" -p Linger 2>/dev/null | cut -d= -f2)" != "yes" ]; then
    log "enabling lingering so the waiter survives logout and reboot"
    loginctl enable-linger "$(id -un)" 2>/dev/null || warn "could not enable lingering; run: sudo loginctl enable-linger $(id -un)"
  fi
fi

log "doctor"
"$CLI" doctor || true

log "done. Use Codex normally: codex"
log "status: $CLI status"
