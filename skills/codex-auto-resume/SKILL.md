---
name: codex-auto-resume
description: Report or change the state of the codex-auto-resume plugin, which resumes the exact Codex session that hit a usage limit once the quota resets. Use when the user asks about usage limits, quota resets, automatic resume, a stalled session after a limit, or the status of codex-auto-resume.
---

# codex-auto-resume

`codex-auto-resume` watches for Codex usage-limit interruptions, records the
explicit session id and the reset time Codex reported, and resumes that exact
session from a small background helper once the legitimate quota is available
again.

## Reading the current state

```bash
codex-auto-resume status
codex-auto-resume status --json
codex-auto-resume doctor
codex-auto-resume logs -n 50 --json
```

`status` prints `enabled` or `disabled`, whether the helper is running, and one
block per pending recovery. With nothing to resume the helper is deliberately
not resident, so this is healthy:

```
enabled
helper: not running
no pending sessions
```

While waiting, it looks like this:

```
enabled
helper: running
session: 01a13e0c-d592-7401-b638-b8b61003c313
  state: waiting_for_quota
  reset_at: 2026-10-15 07:51:34 UTC (source: app_server)
  resume_at: 2026-10-15 07:51:44 UTC
  session_attached: no
  attempts: 0
```

## Changing behaviour

```bash
codex-auto-resume disable              # stop automatic resume, keep state
codex-auto-resume enable               # allow automatic resume
codex-auto-resume cancel <SESSION-ID>  # cancel one pending recovery
codex-auto-resume cancel --all         # cancel all pending recoveries
```

## How it runs

The waiter runs outside Codex as one user-level background integration, chosen
automatically for the platform: a `systemd --user` unit on Linux (and WSL with
systemd), a `launchd` LaunchAgent on macOS, or a per-user Task Scheduler task on
Windows. `codex-auto-resume doctor` reports the detected platform and backend
first (`[ok] platform: …`, `[ok] service backend: …`); `install-service` and
`uninstall-service` manage the integration for the current platform. None of
this needs root.

## Rules

- Never claim a session was resumed unless `status`/`logs` show it.
- Never run `codex-auto-resume cancel` on the user's behalf unless the user
  asked for it: cancelling a recovery means the session will not continue by
  itself.
- This tool waits for the quota reset time Codex itself reported. It never
  bypasses, evades, or rotates around usage limits. If the user wants more
  access, the answer is a plan change or credits, not this tool.
- If the user hit a limit and wants the session to continue automatically, tell
  them to close the Codex session (or let it exit) and confirm
  `codex-auto-resume status` shows a pending recovery; the helper then resumes
  it after the reset.
