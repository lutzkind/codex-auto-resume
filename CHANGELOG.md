# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.1] - 2026-10-27

Patch release for a missed auto-resume observed in production.

### Fixed

- **Clock-only reset times are anchored to the interruption.** `try again at
  8:00 AM` is now resolved against the timestamp of the failed turn, not the
  later time the transcript happens to be read. A helper that re-reads the
  transcript after the reset therefore keeps the same-day reset instead of
  silently rolling it forward 24 hours (the production failure on
  2026-10-27: interrupted at 06:17, first re-read at 08:01, next attempt
  scheduled for the following morning).
- **A live TUI is recognised through Codex's own refusal.** Resuming a thread
  that a TUI still owns returns `thread-store conflict: ... already has an
  active writer`; that is now classified as "still attached", keeps the
  pending recovery, and rechecks after the normal attached-session interval.
  It is no longer mistaken for a quota result or a hard failure, and the CLI's
  error text is kept in `last_message` and the log.
- **No tight retry loop after repeated quota exhaustion.** A resume that still
  reports quota with an already-passed reset uses the bounded retry policy and
  honours `MAX_ATTEMPTS` instead of retrying immediately.
- **Failed launches are no longer masked by an unchanged transcript.** A
  plain-text CLI failure (for example an active writer or a stream error) is
  reported as such; the transcript is only used to reconcile a resume when a
  new turn was actually written. Captured diagnostics are redacted for the
  continuation prompt so logs still never contain prompts.
- **Manual continuation supersedes a stale recovery before the wait.** If the
  user continues the session themselves, the old pending record is cleared as
  superseded even while the session is still attached, instead of waiting for
  a next-day reset and then re-evaluating it.
- **A running turn is no longer mistaken for its older quota error.** The
  newest transcript lifecycle event is now considered, including
  `task_started`; a session that is mid-turn is not armed by the detection
  sweep, and a helper crash during its own resume is not replayed.

Regression coverage includes the production clock-only timing (interruption
before the reset, transcript first read after it), no next-day rollover, the
captured active-writer diagnostic, repeated quota exhaustion, unchanged
transcripts, and the existing exact-session, cancellation, disable, restart,
hook-idempotency and macOS/Windows suites.

## [0.2.0] - 2026-10-16

Cross-platform release: macOS and native Windows join Linux, WSL stays on the
Linux path, and the Linux installation is unchanged in behaviour.

### Added

- **Platform abstraction.** The portable core (quota detection, exact session
  UUID tracking, recovery state, reset handling, ordering, retry, cancellation,
  enable/disable, hook handling) is now clearly separated from one small
  service-lifecycle adapter per platform in `codex_auto_resume/service/`
  (`base.py`, `linux.py`, `macos.py`, `windows.py`). `install-service`,
  `uninstall-service`, `status`, `doctor` and the hooks select the adapter
  automatically. WSL selects the Linux adapter and never a Windows one.
- **macOS support** with a native `launchd` LaunchAgent in
  `~/Library/LaunchAgents` (`launchctl bootstrap`/`bootout`/`kickstart`/`kill`,
  no deprecated `load`/`unload`): starts at login (`RunAtLoad`), restarts on
  failure only (`KeepAlive{SuccessfulExit: false}`), never kills an in-flight
  resume (`AbandonProcessGroup`), logs to the state directory, and needs no
  root. `scripts/install.sh` now installs it (no lingering equivalent exists;
  the agent starts again at login and resumes immediately when the reset time
  has passed).
- **Native Windows support** with a per-user Task Scheduler task created from
  generated XML (`schtasks`): logon trigger, `RestartOnFailure`, no time limit,
  `InteractiveToken` + `LeastPrivilege` (no administrator rights, no service).
  When Task Scheduler refuses to start a per-user task in a non-interactive
  session (observed on GitHub's Windows runners, which have no desktop logon),
  the same helper command is started detached instead, so the recovery still
  runs; `scripts/install.ps1` / `scripts/uninstall.ps1` install and remove it.
- **Windows hook launcher** `bin/codex-auto-resume.cmd` plus a `commandWindows`
  entry in every `hooks/hooks.json` handler. Codex runs command hooks through
  `cmd.exe` on Windows and picks `commandWindows` there, so hooks work without
  a POSIX shell; the launcher finds a Python 3.9+ interpreter itself.
- **Portable process identity.** Helper liveness keeps its Linux guarantees and
  gains equivalents elsewhere: macOS compares the recorded `ps` start time
  (2 s tolerance, `LC_ALL=C`) plus the `sysctl` boot id; Windows compares the
  exact `GetProcessTimes` creation time and the kernel boot time. Command lines
  come from `ps` (macOS), `NtQueryInformationProcess` (Windows) and `/proc`
  (Linux), so a recycled pid is rejected on every platform.
- **Windows wake channel.** Windows has no `SIGUSR1`, so a running helper waits
  on a per-installation named event (`Local\codex-auto-resume-helper-<hash>`)
  and `disable`/`cancel`/hooks signal it with `SetEvent`; where the event cannot
  be created the sleep slice is bounded so state changes are still noticed
  within seconds.
- **Portable state locations.** Linux keeps `${XDG_STATE_HOME:-~/.local/state}`,
  macOS uses `~/Library/Application Support/codex-auto-resume` (an explicitly
  set `XDG_STATE_HOME` and an existing legacy `~/.local/state` directory are
  honoured), Windows uses `%LOCALAPPDATA%\codex-auto-resume`. The installed
  service records the state directory (and `CODEX_HOME`, when set) explicitly,
  so no platform's minimal service environment changes which state is used.
- **Shared daemon detection without command lines.** A session whose host
  process is Codex's shared app-server daemon is now also recognised through
  Codex's own `app-server-daemon/daemon.pid` / `app-server.pid` record, which
  works on every platform, with the previous command-line markers kept as a
  fallback.
- Cross-platform tests: platform detection (Linux/macOS/Windows/WSL1/WSL2/
  unknown), adapter selection, generated systemd unit / launchd plist / Windows
  task XML (including paths with spaces and non-ASCII characters), reference
  file drift, per-platform process identity, wake channels, `.cmd` spawn
  handling, and hook commands executed through `sh` and `cmd.exe`. CI now runs
  Ubuntu, macOS **and Windows** with Python 3.9 and 3.12, plus a
  `platform-lifecycle` job per OS that installs, starts, wakes, uses and
  uninstalls the real service manager.
- Real-platform integration test
  (`tests/test_platform_integration.py`): the service manager starts a helper
  that resumes a pending recovery through the fake Codex CLI, recording the
  exact `exec resume <SESSION-UUID>` invocation, then cleans the state up.

### Changed

- `hooks/hooks.json` handlers carry `commandWindows` alongside `command`;
  hook hashes change, so `codex-auto-resume trust-hooks` must be run once after
  upgrading (the TUI `/hooks` approval works too).
- The systemd unit's `ExecStart` now passes `--state-dir` (and `--codex-home`
  when set) explicitly. The unit, its restart semantics, `KillMode=process`,
  `WantedBy=default.target` and `Restart=on-failure` are otherwise unchanged,
  and an existing installation keeps its state directory.
- `status --json` reports `platform` and `service` (backend, installed, active,
  enabled). The human-readable `status` output is unchanged.
- `doctor` reports the detected platform and service backend first
  (`[ok] platform: macOS arm64`, `[ok] service backend: launchd …`) and asks the
  right backend to start the helper in its failure message. The remaining
  checks are unchanged.
- `codexenv.resolve_codex_binary` also looks at the documented Windows install
  locations (`%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe`,
  `~/.codex/packages/standalone/codex(.exe)`).
- The resume child is spawned with platform-appropriate detachment, and a
  Codex installed as a `.cmd` shim (npm on Windows) is started through
  `cmd.exe /d /s /c` with a correctly quoted command line.

### Fixed

- A hook running on Windows would have recorded `cmd.exe` as the session host,
  which exits immediately and would have made every session look detached. The
  recorded host is now the first non-shell ancestor (verified by test).
- `tests/fake_codex.py` no longer depends on the glibc `%-I` extension, so the
  test harness runs on Windows and macOS.

## [0.1.1] - 2026-10-15

Helper pid-state lifecycle fix.

### Fixed

- `doctor` no longer fails a healthy idle installation. Its last check
  required a running helper process, but the helper exits by design when there
  is nothing to resume, so every correctly idle installation reported
  `[!!] helper running` and `doctor: problems found` while `status` correctly
  said `helper: not running`. The check is now `helper state`: running is
  healthy, idle is healthy, and it only fails when an active recovery exists
  while automatic resume is enabled and no helper is serving it.
- `status` and `doctor` now share one canonical helper-liveness decision
  (`helper.helper_pid_state()`), so they can never disagree again. The hooks'
  wake-up call (`notify_helper`) uses the same decision before signalling.
- Helper pid ownership is robust against stale and recycled state:
  - helper startup reconciles a marker left behind by a killed or crashed
    predecessor before publishing its own;
  - the marker is validated against the recorded process start time, the
    kernel boot id (a marker from a previous boot is stale by definition) and
    the command line, which must be the helper invocation itself — a recycled
    pid belonging to any other program, including another `codex-auto-resume`
    subcommand, is rejected and never signalled;
  - malformed or non-JSON markers are treated as invalid and cleaned;
  - exit cleanup removes the marker only if it is still the helper's own, and
    reconciliation re-reads the record before unlinking so a replacement
    helper's marker is never deleted;
  - every exit path (idle exit, `SIGTERM`/`SIGINT`, exceptions, service stop)
    removes the marker, and a `SIGKILL`ed helper's leftovers are reconciled by
    the next helper start or the next `doctor` run.
- `status --json` additionally reports `helper_state` (`running`, `idle`,
  `stale`, `invalid`) and `helper_pid`. The human-readable `status` output is
  unchanged.

### Changed

- `doctor` prints `doctor: all checks passed` instead of `doctor: ok`.
- The app-server `clientInfo.version` now follows the package version instead
  of a hard-coded string.

### Added

- Regression tests for the helper pid lifecycle: healthy idle, stale pid,
  pid reuse by a live unrelated process and by another subcommand, malformed
  state, previous-boot markers, start-time mismatch, normal idle exit, signal
  shutdown, exception cleanup, startup reconciliation, `SIGKILL` recovery, and
  a genuinely running helper spawned exactly as the service does (217 tests).

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
- Detection sweep in the helper, because Codex fires no hook for a failed turn
  and a TUI client detach does not reliably run `SessionEnd` (observed: no
  `SessionEnd` at all in one case, and ~70 s late in another). The sweep inspects
  Codex's own session data for the usage-limit signature on start, on every hook
  signal, and on a short interval while a session is open, with a 10-minute
  residual window so the helper is not permanently resident. Both paths are
  idempotent: when a hook and the sweep see the same interruption the existing
  record is kept, which was observed live.
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
  service restart and reboot, resumes immediately when the reset time has
  already passed, and re-detects a second session that hits its quota while it
  waits for the first. If a resume hits the limit again, the next wake-up time
  comes from a fresh `account/rateLimits/read` snapshot rather than from the
  older message text.
- Safety semantics: no auto-resume after user interrupt, successful turn,
  normal completion, `disable`, or `cancel`; detached-session gating;
  superseded-recovery detection; no replay of an ambiguous in-flight resume;
  24-hour staleness bound; no `--last` anywhere.
- CLI: `status`, `enable`, `disable`, `cancel`, `logs`, `doctor`,
  `trust-hooks`, `install-service`, `uninstall-service`, `hook`, `helper`.
- `trust-hooks` persists Codex hook trust through Codex itself
  (`hooks/list` for the current hash, `config/value/write` to store it).
- Install/uninstall shell scripts, a reference systemd user unit, a secret
  scan script, and 190 unit/integration tests plus a deterministic fake Codex
  CLI harness.
