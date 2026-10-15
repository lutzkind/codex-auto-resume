# AGENTS.md — codex-auto-resume

Guidance for coding agents working in this repository.

## Start here

- Run `git status -sb` before making changes and `git log -1 --oneline` before
  reporting state. A local checkout is not proof that it matches the canonical
  `origin/main`, and `origin/main` is not proof of what is installed on the
  host; check the live installation as described below when behaviour matters.
- `/root/REPO_MAP.md` is the host-level repository map. This repository's
  canonical checkout on this host is `/root/codex-auto-resume`.
- Read `README.md` before changing behaviour and `CHANGELOG.md` before
  releasing. `SECURITY.md` lists what this tool must never do.
- The repository root is the whole project: the Codex plugin manifest, the hook
  pack, the Python package, the tests and the install tooling. There is no
  package to install for development — `bin/codex-auto-resume` adds `src/` to
  `sys.path` itself.

## Hard rules

1. **No quota circumvention.** Never add account rotation, account creation,
   limit evasion, or anything that changes who pays. The only permitted
   behaviour is waiting for the reset Codex itself reported.
2. **Never resume the most-recent session implicitly.** Recovery is always bound
   to an explicit session UUID; `scripts/check-no-last.py` fails the build if the
   flag appears as executable text under `src/`, `bin/` or `hooks/`.
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
python3 -m unittest discover -s tests -t tests -v   # all tests (217)
python3 -m unittest discover -s tests -v            # same suite, CI-standard form
python3 -m compileall -q src bin/codex-auto-resume
python3 scripts/check-manifests.py                  # plugin packaging
python3 scripts/check-no-last.py                    # explicit-session-id guard
scripts/secret-scan.sh                              # credential scan
scripts/check.sh                                    # all of the above
```

Tests are hermetic and spend no real quota. `tests/fake_codex.py` is a
deterministic fake Codex CLI that implements the app-server methods,
`exec resume` and transcript writing, so the whole
start → quota → wait → exact-session resume → completion lifecycle runs in
process, with a fake clock to accelerate reset waits.

## Production

This repository's "production" is the local installation on this host. There is
no server, container, or hosted service.

| Item | Value |
|---|---|
| Checkout | `/root/codex-auto-resume` |
| CLI | `/root/cli-persistence/bin/codex-auto-resume` (wrapper → `bin/codex-auto-resume`) |
| Installed plugin payload | `/root/.codex/plugins/cache/codex-auto-resume/codex-auto-resume/<version>` |
| Marketplace config | `[marketplaces.codex-auto-resume]` in `/root/.codex/config.toml` |
| Hook trust | `[hooks.state."codex-auto-resume@…"]` in `/root/.codex/config.toml` |
| User service | `~/.config/systemd/user/codex-auto-resume-helper.service` |
| State | `${XDG_STATE_HOME:-~/.local/state}/codex-auto-resume` |
| Registered in | `/root/scripts/server_update_manifest.yaml` (git repos + CLI wrappers) |

- Do not add production runtime beyond the single user unit. The waiter must
  stay one small standard-library process plus the Codex CLI it invokes.
- Never change the user's Codex configuration except additively through Codex's
  own commands: `features enable plugins`, `plugin marketplace add`,
  `plugin add`, and `config/value/write` for hook trust.
- The plugin manifest must stay at `.codex-plugin/plugin.json`. **Do not add a
  root `plugin.json`**: on `codex-cli 0.162.1` `find_plugin_manifest_path`
  prefers it and `core-plugins` loads plugin hooks only for the legacy manifest
  format, so a root Agent Plugins manifest silently disables every hook
  (`hooks/list` then reports zero handlers). `scripts/check-manifests.py` fails
  the build if one appears.
- Confirm hook discovery with Codex itself, never by inspection:
  `codex-auto-resume trust-hooks --dry-run` prints every discovered handler with
  its `trustStatus`.
- Reinstall after changing code that hooks run:

  ```bash
  codex plugin remove codex-auto-resume@codex-auto-resume
  codex plugin add codex-auto-resume@codex-auto-resume
  codex-auto-resume trust-hooks
  codex-auto-resume doctor
  ```

- Reinstall after changing the service generator:

  ```bash
  codex-auto-resume install-service && systemctl --user restart codex-auto-resume-helper.service
  ```

Two host facts this project depends on, both verified on 2026-10-15:

1. `/usr/local/bin/ai-cli-autokill.sh` (cron, every minute) kills any TTY-less
   process whose command line contains `codex`. Its documented infrastructure
   exception list was extended with the helper module
   (`codex_auto_resume helper`) and its process tree, which is the mechanism
   that script already provides for supervised infrastructure. An ordinary
   TTY-less `codex exec` is still killed (verified). The generated unit also
   sets `Restart=on-failure` with `StartLimitIntervalSec=0`, so the waiter
   recovers by itself even if something kills it.
2. That same guard can kill long shell commands whose arguments contain the
   repository path, so `/root/agent-tmp/opencode/repo` is a scratch symlink to
   the checkout for running long local commands.

Known pre-existing host condition (not caused by this project): the
`[marketplaces.personal]` entry points at `/root/control-center-dashboard`,
which has no marketplace manifest, so `codex plugin list` and
`codex plugin marketplace list` exit with an error. `plugin add`/`plugin list
--marketplace codex-auto-resume` and `hooks/list` are unaffected.

Do not place credentials, auth files, prompts, or transcripts in documentation,
issues, or handoffs.

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
| `src/codex_auto_resume/helper.py` | Waiter, detection sweep and resume loop |
| `src/codex_auto_resume/runner.py` | `codex exec resume` invocation and outcome classification |
| `src/codex_auto_resume/state.py` | Per-session JSON state, cancellation tombstones |
| `src/codex_auto_resume/trust.py` | Hook trust through Codex's own config API |
| `src/codex_auto_resume/service.py` | systemd user unit generation |
| `scripts/` | install, uninstall, manifest check, session-id guard, secret scan |
| `systemd/` | Reference copy of the generated unit |

## Documentation duties

Update `README.md` (behaviour, limitations, packaging), `CHANGELOG.md`, and this
file whenever behaviour or installation changes. Record verified live evidence
(commands and observed output) in the host handoff under
`/root/mcp-shared/chatgpt/codex-auto-resume/`, not in the repository.
