#!/usr/bin/env bash
# Remove codex-auto-resume from the local Codex CLI and user systemd.
#
#   scripts/uninstall.sh            # keep state (session/pending records)
#   scripts/uninstall.sh --purge    # also delete the state directory
set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

CODEX_BIN="${CODEX_AUTO_RESUME_CODEX_BIN:-$(command -v codex || true)}"
MARKETPLACE_NAME="codex-auto-resume"
PLUGIN_SELECTOR="codex-auto-resume@${MARKETPLACE_NAME}"
STATE_DIR="${CODEX_AUTO_RESUME_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/codex-auto-resume}"
CLI="${CODEX_AUTO_RESUME_BIN_DIR:-$HOME/.local/bin}/codex-auto-resume"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

if [ -x "$CLI" ]; then
  log "stopping automatic resume"
  "$CLI" disable || true
  log "removing the user service"
  "$CLI" uninstall-service || warn "could not remove the user service"
fi

if [ -n "$CODEX_BIN" ]; then
  log "removing the plugin"
  "$CODEX_BIN" plugin remove "$PLUGIN_SELECTOR" || warn "plugin remove reported an error"
  log "removing the marketplace"
  "$CODEX_BIN" plugin marketplace remove "$MARKETPLACE_NAME" || warn "marketplace remove reported an error"
else
  warn "codex CLI not found; remove the plugin and marketplace manually"
fi

if [ "$PURGE" = "1" ]; then
  log "deleting state at $STATE_DIR"
  rm -rf "$STATE_DIR"
else
  log "keeping state at $STATE_DIR (use --purge to delete it)"
fi

log "done"
