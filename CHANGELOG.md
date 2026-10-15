# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-15

Initial release.

### Added

- Codex plugin packaging: `.codex-plugin/plugin.json` manifest (the format the
  installed CLI loads bundled hooks from) declaring the `hooks/hooks.json` hook
  pack, the bundled skill and the plugin interface, plus a
  `.agents/plugins/marketplace.json` marketplace entry so the repository can be
  added as a local or Git marketplace. A portable Agent Plugins root
  `plugin.json` is deliberately **not** shipped: on `codex-cli 0.162.1` it takes
  precedence over `.codex-plugin/plugin.json` and silently disables
  plugin-bundled hooks (`hooks/list` then reports zero handlers).
- Lifecycle hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`,
  `Interrupt`) that register the explicit session UUID, cwd, model, permission
  mode and host process, and that arm a pending recovery when a session ends on
  a usage-limit interruption.
- Quota detector driven by Codex's structured
  `error.codex_error_info == "usage_limit_exceeded"` value, with a carefully
  tested message parser as the only fallback. Network errors, auth failures,
  cancellation, normal completion and sandbox/permission failures are
  explicitly rejected.
- Reset-time resolution from `account/rateLimits/read`
  (`ordinaryUsageAllowed`, per-window `resetsAt`), falling back to parsing the
  `try again at …` text Codex prints (clock time, fully dated form, ISO-8601
  with offset), and finally to bounded exponential backoff.
- Out-of-process helper that sleeps until the reported reset time and then runs
  `codex exec resume <SESSION-ID> "<continue prompt>"`. It survives SSH logout,
  service restart and reboot, and resumes immediately when the reset time has
  already passed.
- Safety semantics: no auto-resume after user interrupt, successful turn,
  normal completion, `disable`, or `cancel`; detached-session gating;
  superseded-recovery detection; no replay of an ambiguous in-flight resume;
  no `--last` anywhere.
- CLI: `status`, `enable`, `disable`, `cancel`, `logs`, `doctor`,
  `trust-hooks`, `install-service`, `uninstall-service`, `hook`, `helper`.
- `trust-hooks` persists Codex hook trust through Codex itself
  (`hooks/list` for the current hash, `config/value/write` to store it).
- Install/uninstall shell scripts, a reference systemd user unit, a secret
  scan script, and 139 unit/integration tests plus a deterministic fake Codex
  CLI harness.
