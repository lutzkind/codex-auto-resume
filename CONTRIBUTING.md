# Contributing

Thanks for helping improve `codex-auto-resume`.

## Ground rules

1. **No quota circumvention.** Nothing in this project may rotate accounts,
   create accounts, defeat or evade limits, or change the payer. Waiting for
   the reset Codex itself reported is the only permitted behaviour.
2. **No `--last`.** Every resume must be bound to an explicit session id.
3. **Never guess Codex internals.** Use documented/verified surfaces: hook
   payloads, session transcripts, the app-server JSON-RPC API, and the CLI. If
   a format cannot be verified against the installed CLI, do not parse it.
4. **No new infrastructure.** No database, queue, web service, or second
   scheduler. State is small JSON files; waiting is one user systemd unit.
5. **No dependencies.** Standard library only, Python 3.9+.
6. Keep changes small and add tests for every behaviour change.

## Development

```bash
git clone https://github.com/lutzkind/codex-auto-resume.git
cd codex-auto-resume
python3 -m unittest discover -s tests -t tests -v
```

The suite needs no third-party packages. `tests/fake_codex.py` is a fake Codex
CLI that implements the app-server methods, `exec resume`, and transcript
writing, so the full lifecycle is testable without spending quota.

Before opening a pull request:

```bash
scripts/check.sh          # tests, compile check, whitespace and secret scan
```

## Verifying against a real Codex install

When you rely on a Codex surface, confirm it against the installed CLI rather
than against documentation alone. Useful probes:

```bash
codex --version
codex exec --json "reply with one word"        # transcript + error shapes
ls "${CODEX_HOME:-$HOME/.codex}/sessions"/**/rollout-*.jsonl
codex plugin list --marketplace <your-marketplace>
```

Plugin-bundled hooks are only discovered for the `.codex-plugin/plugin.json`
manifest format. The authoritative check is Codex's own `hooks/list` app-server
method, which `codex-auto-resume doctor` and `codex-auto-resume trust-hooks`
already use: it reports every discovered handler with its `currentHash` and
`trustStatus`. Never assume a manifest works because the file looks right;
confirm that `hooks/list` reports the handlers.

If a Codex release changes a hook payload, an app-server field, or the
transcript shape, update the parser, add a regression test with the captured
payload, and note the supported version range in `README.md`.

## Reporting bugs

Include the `codex --version` output, `codex-auto-resume status --json`,
`codex-auto-resume doctor`, and the relevant `codex-auto-resume logs --json`
lines. Logs are designed to be safe to share: no prompts, transcripts, tokens
or credentials. If you think you found a security issue, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Commit style

Short imperative subject lines, a blank line, then what changed and why. Keep
unrelated changes out of the same commit.
