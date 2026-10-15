# codex-auto-resume

A standalone [Codex CLI](https://github.com/openai/codex) plugin that continues
**the exact session** that a usage-limit interruption stopped, once the quota
reset time reported by Codex has passed.

```
codex                                  # normal usage, unchanged
  ↓
normal work
  ↓
You've hit your usage limit … or try again at 7:51 AM.
  ↓
session id + reset time recorded, outside the Codex process
  ↓
you can close the terminal, log out, even reboot
  ↓
7:51 AM → the same session is resumed automatically
  ↓
work continues
  ↓
repeat if the quota is exhausted again, stop when the task finishes
```

## What it does

- Detects a **usage-limit interruption** in the Codex CLI you already have
  installed.
- Records the **explicit session id** (UUID) and the reported reset time.
- Waits in a small helper process that lives **outside** Codex, so it survives
  the terminal, the SSH connection, and a reboot.
- Resumes **that exact session** with `codex exec resume <SESSION-ID>` once the
  legitimate quota is available again, and continues the unfinished task.
- Repeats the cycle if the quota is exhausted again.
- Clears its state when the session finishes normally.

## What it does not do

- It does **not** circumvent, evade, multiply, rotate around, or otherwise
  bypass OpenAI usage limits. It waits for the reset time Codex itself
  reported and then continues your own session. Adding credits, upgrading a
  plan, or changing accounts is entirely up to you.
- It does **not** change your model, your account, your provider, your approval
  policy, or your sandbox settings.
- It does **not** scrape your terminal. It reads Codex's own structured session
  data.
- It does **not** auto-resume after you cancel. See
  [Safety and stop semantics](#safety-and-stop-semantics).
- It does **not** run a web service, database, queue, message bus, or second
  scheduler. It is one small Python file tree and one user systemd unit.

## Requirements

- **Codex CLI 0.162.x** (verified against `codex-cli 0.162.1`). The plugin uses
  the `.codex-plugin/plugin.json` Codex manifest, the `hooks/hooks.json` hook
  pack, the `SessionStart` / `Stop` / `SessionEnd` / `Interrupt` /
  `UserPromptSubmit` lifecycle hooks, the `codex app-server` JSON-RPC methods
  `hooks/list`, `config/value/write`, and `account/rateLimits/read`, and
  `codex exec resume <SESSION_ID>`. Earlier or later versions may work but are
  **not** tested.
- **Python 3.9+** (standard library only, no third-party packages).
- **Linux with a user systemd instance** for the out-of-process waiter
  (`systemctl --user`). macOS works for everything except the service: run
  `codex-auto-resume helper` under `launchd` or any supervisor you prefer.
- A Codex account whose usage limits reset on their own (ChatGPT Plus/Pro/Business
  usage windows). Accounts blocked by *spent credits* are recorded but never
  auto-resumed, because waiting does not fix them.

## Installation

```bash
git clone https://github.com/lutzkind/codex-auto-resume.git
cd codex-auto-resume

# 1. put the command line tool on PATH
mkdir -p ~/.local/bin
ln -sf "$PWD/bin/codex-auto-resume" ~/.local/bin/codex-auto-resume

# 2. install the plugin into Codex
codex features enable plugins                     # once, if plugins are disabled
codex plugin marketplace add "$PWD"               # local marketplace
codex plugin add codex-auto-resume@codex-auto-resume

# 3. persist hook trust (the one thing Codex requires for hooks to run)
codex-auto-resume trust-hooks

# 4. install the user service that does the waiting
codex-auto-resume install-service

# 4b. let the service run without an open login session (needed for reboot survival)
loginctl enable-linger "$USER"

# 5. check everything
codex-auto-resume doctor
```

`scripts/install.sh` performs exactly these steps, and
`scripts/uninstall.sh` undoes them. Both are thin wrappers around the native
Codex and `systemctl` commands above.

To install from the published Git marketplace instead of a local checkout:

```bash
codex plugin marketplace add lutzkind/codex-auto-resume
codex plugin add codex-auto-resume@codex-auto-resume
codex-auto-resume trust-hooks
codex-auto-resume install-service
```

After installation your workflow stays `codex`. There is no wrapper command and
no `aar codex` alias.

### Hook trust

Codex refuses to run hooks whose hash it has not been told to trust. The TUI
does this when you approve a hook in `/hooks`. `codex-auto-resume trust-hooks`
does the same thing programmatically:

1. it asks Codex for the discovered hooks and their current hashes
   (`hooks/list` over `codex app-server --stdio`);
2. it writes `hooks.state."<hook key>".trusted_hash` through Codex's own
   `config/value/write` API.

Nothing is guessed: the hash always comes from Codex itself. If you prefer to
approve by hand, open `/hooks` in the Codex TUI and trust the five
`codex-auto-resume` handlers; `codex-auto-resume doctor` will then report
`plugin hooks trusted: ok`.

## Enabling, disabling, cancelling

```bash
codex-auto-resume status              # enabled/disabled + pending recoveries
codex-auto-resume status --json       # machine readable
codex-auto-resume enable              # allow automatic resume
codex-auto-resume disable             # stop automatic resume (keeps pending state)
codex-auto-resume cancel <SESSION-ID> # cancel one pending recovery
codex-auto-resume cancel --all        # cancel everything pending
codex-auto-resume logs -n 100         # recent helper events (JSONL with --json)
codex-auto-resume doctor              # check the local installation
```

`disable` does not delete state, and it does not touch a session you are
already using. `cancel` writes a tombstone for that interruption; if the same
session is interrupted by a *new* usage limit later, it is eligible again.

`status` looks like this while waiting:

```
enabled
helper: running
session: 01a13e0c-d592-7401-b638-b8b61003c313
  state: waiting_for_quota
  reset_at: 2026-10-15 07:51:34 UTC (source: app_server)
  resume_at: 2026-10-15 07:51:44 UTC
  session_attached: no
  attempts: 0
  cwd: /home/you/project
```

## Debugging

```bash
codex-auto-resume doctor                 # binary, features, trust, service, quota read
codex-auto-resume logs --json | tail -50
systemctl --user status codex-auto-resume-helper.service
journalctl --user -u codex-auto-resume-helper.service -n 100
```

Every state change is logged with ids, timestamps, and statuses. Logs never
contain prompts, transcripts, tokens, or credentials.

## How quota reset detection works

Two independent, structured signals, in this order:

1. **The interrupted turn.** Codex records each turn's outcome in the session
   transcript (`rollout-*.jsonl`). A usage-limit interruption appears as an
   `event_msg` / `task_complete` entry whose `error.codex_error_info` is
   `usage_limit_exceeded`. That structured value — not terminal text — is the
   detector. Ordinary failures (`response_stream_disconnected`,
   `unauthorized`, `context_window_exceeded`, `sandbox_error`, …), user
   cancellation (`turn_aborted`), and normal completions are explicitly *not*
   treated as quota interruptions, so they are never auto-resumed.
2. **The reset time.**
   - The helper asks Codex for the authoritative snapshot through
     `account/rateLimits/read`. The response contains
     `ordinaryUsageAllowed` and, per quota window, `resetsAt` as exact Unix
     seconds. When `ordinaryUsageAllowed` is `false`, the earliest future
     `resetsAt` among exhausted windows (or `rateLimitUpsell.reset_at`) becomes
     the wake-up time. This snapshot is also re-read after a resume that hit the
     limit again, because the backend recomputes the window and the time printed
     in an older error message can already be stale.
   - If the snapshot is unavailable, the reset time is parsed from the message
     Codex printed: `... or try again at 7:51 AM.` (parsed in the host's local
     timezone, which is the timezone Codex formatted it in), or the fully
     dated form `... or try again at Nov 3rd, 2026 4:07 PM.`, or an ISO-8601
     timestamp with an offset.
   - If neither is available, the helper backs off exponentially instead of
     hammering Codex.

When Codex reported an exact reset time the helper sleeps until it (one timer,
plus an hourly safety wake-up and immediate wake-up when state changes). If the
reset time has already passed when recovery starts — for example after a
reboot — the resume is attempted immediately instead of waiting for the next
day.

### Two detection paths, because Codex fires no hook for a failed turn

The plugin's hooks are the fast path and the session registry, but they cannot
be the only trigger:

- a turn that ends in an error runs no `Stop` hook (only a turn that completes
  does), so the quota failure itself produces no hook event;
- an interactive TUI session lives on the shared local app-server daemon, so
  quitting the client does not reliably run the `SessionEnd` hook. Observed on
  this host: one TUI session produced no `SessionEnd` at all, and another
  produced it about 70 seconds after the client was gone — after the sweep had
  already armed the recovery.

The helper therefore also runs a **detection sweep**: a bounded look at Codex's
own session data for the usage-limit signature. The sweep runs when the helper
starts (including at boot), on every hook signal, and on a short interval while
any Codex session is open (20 s) or during a 10-minute residual window after
the last one closes (60 s). A sweep reads the state directory and the tail of
recently modified transcripts only, skips sessions that are already armed,
cancelled or older than 24 hours, and costs a few file reads.

The reset wait itself is still a single sleep until the exact reset timestamp,
not a poll: the sweep exists only to notice that a quota failure happened.

## Safety and stop semantics

Automatic resume **never** happens after:

- an explicit user cancellation (Esc / Ctrl-C turn interrupt), because the
  helper sees the `Interrupt` hook and drops the pending recovery;
- a deliberate session exit after a successful turn (`Stop` hook clears the
  pending recovery);
- normal completion of the task;
- `codex-auto-resume disable`;
- `codex-auto-resume cancel <SESSION-ID>`.

Additional guards:

- The helper only resumes a session that is **detached**: Codex reported
  `SessionEnd`, or the process that hosted the session is gone. While you still
  have the session open in a TUI, nothing is resumed behind your back, because
  two live hosts for one session is worse than waiting. A session found by the
  detection sweep that never registered a host process counts as detached only
  once Codex has stopped writing its transcript for 90 seconds. Set
  `resume_when_attached` to `true` in the state `config.json` if you
  deliberately want the other behaviour.
- If you come back and make progress in the session yourself, the helper sees a
  newer successful turn in the transcript and discards the pending recovery
  instead of resuming.
- If the resume fails for a reason that waiting cannot fix, the recovery is
  marked `failed` and never retried.
- If the helper restarts in the middle of a resume, it decides from the
  transcript: a turn that finished normally is not replayed; a resume with no
  evidence is marked `failed` rather than fired twice.
- An interruption older than 24 hours is ignored, so a long-stopped machine
  does not resume week-old work.
- Accounts blocked by spent credits, spend caps or admin limits are recorded as
  `not_resumable` and never auto-resumed.

## Reboot behaviour

`codex-auto-resume-helper.service` is a user unit with
`WantedBy=default.target` and `Restart=no`. It starts at login/boot, and exits
immediately when there is no pending recovery. When it finds pending state it
either keeps waiting or resumes at once if the reset time has already passed.

For the service to run without an open login session, enable lingering:

```bash
loginctl enable-linger "$USER"
```

`codex-auto-resume doctor` reports whether the unit is installed and running.

## Cancellation and uninstall

```bash
codex-auto-resume cancel --all                     # stop pending recoveries
codex-auto-resume disable                          # stop automatic resume
codex-auto-resume uninstall-service                # remove the user unit
codex plugin remove codex-auto-resume@<marketplace>
codex plugin marketplace remove <marketplace>
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/codex-auto-resume"
```

`scripts/uninstall.sh` runs all of the above, with `--purge` to also delete the
state directory.

## Architecture

```
codex CLI (interactive TUI or exec)
  │  hooks: SessionStart · UserPromptSubmit · Stop · SessionEnd · Interrupt
  ▼
bin/codex-auto-resume hook <Event>          ── records session id, cwd, host pid
  │                                            ── on SessionEnd: reads the transcript,
  │                                               arms a pending recovery
  ▼
$XDG_STATE_HOME/codex-auto-resume/
  ├── config.json              enabled flag, continue prompt, margin
  ├── sessions/<id>.json       session registry (id, cwd, transcript, host pid)
  ├── pending/<id>.json        one recovery per session: reset_at, resume_at, attempts
  ├── cancelled/<id>.json      cancellation tombstone
  ├── launching/<id>.json      marks our own resume vs. a manual resume
  └── logs/helper.log          structured JSONL, size capped
  ▲
  │  systemctl --user (codex-auto-resume-helper.service)
  │
helper process ── waits for reset_at (one sleep, hourly safety wake-up)
  │            ── asks Codex: account/rateLimits/read → ordinaryUsageAllowed, resetsAt
  │            ── if quota is available: codex exec resume <SESSION-ID> "<continue prompt>"
  └────────────── classifies the outcome, clears or re-arms state
```

Files installed by the plugin:

| Path | Purpose |
|---|---|
| `.codex-plugin/plugin.json` | Codex plugin manifest (`hooks`, `skills`, `interface`) |
| `hooks/hooks.json` | The five lifecycle hook handlers |
| `skills/codex-auto-resume/SKILL.md` | Teaches the Codex agent to report status |
| `bin/codex-auto-resume` | CLI and hook entry point (Python, stdlib only) |
| `src/codex_auto_resume/*.py` | Implementation |
| `systemd/` | Reference copy of the user unit |

### Why there is no root `plugin.json`

The portable Agent Plugins root manifest is the long-term format, but on the
installed CLI (`codex-cli 0.162.1`) it is not yet a working home for bundled
hooks:

- `codex_utils_plugins::find_plugin_manifest_path` prefers a root
  `plugin.json` and only falls back to `.codex-plugin/plugin.json` when the
  root file is absent or is not an Agent Plugins manifest.
- `core-plugins` loads plugin-bundled hooks only for the legacy manifest
  format: `if manifest_format == PluginManifestFormat::Legacy {
  load_plugin_hooks(..) } else { (Vec::new(), Vec::new()) }`.

Shipping a root `plugin.json` therefore makes the plugin report **zero hooks**.
This was verified against the installed CLI: with the root manifest present,
`hooks/list` returned an empty list; with only `.codex-plugin/plugin.json`, all
five handlers are discovered with their `currentHash`. `scripts/check-manifests.py`
fails the build if a root `plugin.json` is reintroduced, and the same validation
runs in CI. Moving to the portable manifest is a one-file change once the CLI
loads hooks for the Agent Plugins format.

## State files

Plain JSON, one file per session, written atomically (temp file + `fsync` +
`rename`). A truncated or corrupt file only ever loses one recovery; it never
crashes the helper. Directories are `0700` and files `0600`.

## Privacy

The tool reads the Codex transcripts and `config.toml`; it writes only its own
state directory. It makes no network calls of its own — every network request
is made by the Codex CLI. Nothing is uploaded, and logs contain ids, statuses
and timestamps rather than prompts or transcripts. See
[SECURITY.md](SECURITY.md).

## Limitations

- Resuming happens on a **new continuation turn**. The interrupted request is
  never replayed byte-for-byte; the session continues with a neutral prompt.
- Unattended resume needs approvals that never block. `doctor` warns when
  `approval_policy` is interactive; use `approval_policy = "never"` (or an
  equivalent) if you want fully hands-off resumption.
- A session that is still attached to a live TUI is not resumed (see above).
- Accounts blocked by spent credits, spend caps, or admin limits are recorded
  as `not_resumable` and never auto-resumed.
- Exactly-once delivery is not claimed across a crash: the helper decides from
  the transcript and refuses to fire a resume twice.
- Detection needs the helper to be running. It is started at boot and by every
  hook, and while nothing is pending it exits after a 10-minute residual window
  so it is not a permanently resident process. In the unlikely case that you
  hit the limit and close the client more than 10 minutes before the helper
  started its last check, the recovery is picked up the next time Codex starts
  or the helper runs.
- Only Linux user systemd is wired up out of the box.

## Development

```bash
python3 -m unittest discover -s tests -t tests -v   # 190 tests, no dependencies
python3 -m compileall src bin/codex-auto-resume
scripts/check.sh                                    # tests + hygiene checks
```

`tests/fake_codex.py` is a deterministic fake Codex CLI that implements the
app-server methods, `exec resume`, and transcript writing, so the entire
wait/resume lifecycle is covered without spending real quota. It runs straight
from the checkout, so a `noexec` `/tmp` (as the self-hosted CI runners use) does
not matter.

## License

MIT — see [LICENSE](LICENSE).
