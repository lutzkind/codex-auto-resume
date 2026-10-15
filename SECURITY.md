# Security

## What this tool does with your machine

`codex-auto-resume` is a local tool. It:

- reads Codex's own session transcripts (`$CODEX_HOME/sessions/**/rollout-*.jsonl`)
  and the Codex `config.toml`, both read-only;
- writes its own state under `$XDG_STATE_HOME/codex-auto-resume`
  (default `~/.local/state/codex-auto-resume`), owned by you with `0700`
  directories and `0600` files;
- runs `codex exec resume <SESSION-ID> <prompt>` as a child process, with the
  same user, the same `$CODEX_HOME`, and the same configuration as a normal
  `codex` run;
- installs one user-level systemd unit so waiting survives logout and reboot.

It does **not**:

- read, copy, or transmit `auth.json`, API keys, tokens, or `.env` files;
- make any network request of its own. Every network call is made by the Codex
  CLI you already run;
- send telemetry anywhere;
- change your Codex model, account, provider, or approval policy;
- escalate permissions. It never passes
  `--dangerously-bypass-approvals-and-sandbox`, and it never bypasses sandbox
  or approval settings.

## What it deliberately does not do

- **No quota circumvention.** The tool never rotates accounts, never creates
  new accounts, never retries to defeat a limit, and never changes who pays.
  It waits for the reset time that Codex itself reported and then continues the
  user's own session.
- **No bypass of hook trust.** Hook trust is persisted through Codex's own
  app-server `config/value/write` API using the hash Codex reports
  (`hooks/list`), exactly like approving a hook in the `/hooks` menu. This
  project never guesses at or hand-edits undocumented trust formats.
- **No `--last`.** Recovery is always bound to one explicit session id.

## Reporting a vulnerability

Open a private security advisory on GitHub
(<https://github.com/lutzkind/codex-auto-resume/security/advisories/new>) or
email the maintainer listed in `plugin.json`. Please include the version, the
platform, and a minimal reproduction.

## Repository hygiene

The repository contains no credentials and no host-specific data. Before each
release the maintainers run:

- the full test suite (`python3 -m unittest discover -s tests -t tests`);
- `scripts/secret-scan.sh`, which greps for common credential shapes;
- `git grep` for absolute host paths and personal identifiers.

If you fork this project, do the same before publishing your fork.
