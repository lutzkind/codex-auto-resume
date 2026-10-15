# AGENTS.md — codex-auto-resume

Guidance for coding agents working in this repository.

## What this is

A standalone Codex CLI plugin that resumes **the exact session** a usage-limit
interruption stopped, once the reset time Codex reported has passed. It is
published at <https://github.com/lutzkind/codex-auto-resume> and installed on
this host (see "Host installation" below).

## Hard rules

1. **No quota circumvention.** Never add account rotation, account creation,
   limit evasion, or anything that changes who pays. The only permitted
   behaviour is waiting for the reset Codex itself reported.
2. **Never resume with `--last`.** Recovery is always bound to an explicit
   session UUID. CI and `scripts/check.sh` fail if `--last` appears under
   `src/`, `bin/` or `hooks/`.
3. **Never guess Codex internals.** Only use surfaces verified against the
   installed CLI: hook payloads, session transcripts, the app-server JSON-RPC
   API, and the CLI itself. Add a regression test with the captured payload
   whenever you touch a parser.
4. **No new infrastructure.** No database, queue, web service, second
   scheduler, or dependency. Standard library only, Python 3.9+.
5. **No permission escalation.** Never pass
   `--dangerously-bypass-approvals-and-sandbox`, never change approval or
   sandbox settings, never bypass hook trust.

## Commands

```bash
python3 -W ignore::ResourceWarning -m unittest discover -s tests -t tests -v  # all tests
python3 -m compileall -q src bin/codex-auto-resume
python3 scripts/check-manifests.py     # plugin packaging (Codex manifest, hooks, marketplace)
scripts/secret-scan.sh                 # credential scan
scripts/check.sh                       # everything above in one go
```

There is no package to install: `bin/codex-auto-resume` adds `src/` to
`sys.path` itself. `tests/fake_codex.py` is a deterministic fake Codex CLI used
by the integration tests; it implements the app-server methods, `exec resume`,
and transcript writing, so no real quota is spent in tests.

## Layout

| Path | Purpose |
|---|---|
| `.codex-plugin/plugin.json` | Codex plugin manifest: `hooks`, `skills`, `interface` |
| `hooks/hooks.json` | The five lifecycle hook handlers |
| `skills/codex-auto-resume/SKILL.md` | Skill for reporting status from inside Codex |
| `bin/codex-auto-resume` | CLI and hook entry point |
| `src/codex_auto_resume/hooks.py` | Hook handlers (session registry, arming, cleanup) |
| `src/codex_auto_resume/quota.py` | Usage-limit detection and reset-time parsing |
| `src/codex_auto_resume/ratelimit.py` | `account/rateLimits/read` client |
| `src/codex_auto_resume/helper.py` | Out-of-process waiter, detection sweep and resume loop |
| `src/codex_auto_resume/runner.py` | `codex exec resume` invocation and outcome classification |
| `src/codex_auto_resume/state.py` | Per-session JSON state, cancellation tombstones |
| `src/codex_auto_resume/trust.py` | Hook trust through Codex's own config API |
| `src/codex_auto_resume/service.py` | systemd user unit generation |
| `scripts/` | install, uninstall, manifest check, secret scan |
| `systemd/` | Reference copy of the generated unit |

## Packaging constraints (verified, do not regress)

* The Codex manifest must stay at `.codex-plugin/plugin.json`.
* **Do not add a root `plugin.json`.** On `codex-cli 0.162.1`
  `find_plugin_manifest_path` prefers it, and `core-plugins` only loads
  plugin-bundled hooks for the legacy manifest format, so a root Agent Plugins
  manifest silently disables all hooks (`hooks/list` then reports zero
  handlers). `scripts/check-manifests.py` fails the build if one appears.
* Confirm hook discovery with Codex itself, never by inspection:
  `codex-auto-resume trust-hooks --dry-run` prints every discovered handler
  with its `trustStatus`.

## Host installation (this machine)

| Item | Value |
|---|---|
| Checkout | `/root/codex-auto-resume` |
| CLI | `/root/cli-persistence/bin/codex-auto-resume` (wrapper → `bin/codex-auto-resume`) |
| Plugin payload | `/root/.codex/plugins/cache/codex-auto-resume/codex-auto-resume/<version>` |
| Marketplace config | `[marketplaces.codex-auto-resume]` in `/root/.codex/config.toml` |
| Hook trust | `[hooks.state."codex-auto-resume@…"]` in `/root/.codex/config.toml` |
| User service | `~/.config/systemd/user/codex-auto-resume-helper.service` |
| State | `${XDG_STATE_HOME:-~/.local/state}/codex-auto-resume` |

Reinstall after changing the plugin payload:

```bash
codex plugin remove codex-auto-resume@codex-auto-resume
codex plugin add codex-auto-resume@codex-auto-resume
codex-auto-resume trust-hooks     # the hash changes with any hook edit
codex-auto-resume doctor
```

Reinstall after changing the service generator:

```bash
codex-auto-resume install-service && systemctl --user restart codex-auto-resume-helper.service
```

Two host facts this project depends on. Both were verified on 2026-10-15.

1. `/usr/local/bin/ai-cli-autokill.sh` (cron, every minute) kills any TTY-less
   process whose command line contains `codex`, and it also matched the
   helper's own shell commands. Its documented infrastructure exception list was
   extended with the helper module (`codex_auto_resume helper`) and its direct
   children, which is the exception mechanism that script already provides for
   supervised infrastructure. The guard still kills an ordinary TTY-less
   `codex exec` (verified by running one and re-running the guard). The
   generated unit also sets `Restart=on-failure` plus
   `StartLimitIntervalSec=0`, so the waiter recovers by itself even if anything
   kills it.
2. `/root/codex-auto-resume` used to be shadowed in shell commands by that same
   guard, so `cd /root/codex-auto-resume && …` could be killed if it ran longer
   than a minute. `/root/agent-tmp/opencode/repo` is a convenience symlink to
   the checkout for running long commands locally; it is scratch and is not
   part of the project.

Known pre-existing host condition (not caused by this project): the
`[marketplaces.personal]` entry points at `/root/control-center-dashboard`,
which has no marketplace manifest, so `codex plugin list` and
`codex plugin marketplace list` exit with an error. `plugin add/list
--marketplace codex-auto-resume` and `hooks/list` are unaffected.

## Documentation duties

Update `README.md` (behaviour, limitations, packaging), `CHANGELOG.md`, and
this file whenever behaviour or installation changes. Record verified live
evidence (commands and observed output) in the host handoff under
`/root/mcp-shared/chatgpt/codex-auto-resume/`, not in the repository.
