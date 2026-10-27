"""CLI surface: status, enable/disable, cancel, logs, doctor."""

from __future__ import annotations

import io
import json
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from support import QUOTA_MESSAGE, StateCase, task_complete
from codex_auto_resume import __version__, cli, log, paths, state


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def pending(session_id: str = "sid", **overrides) -> dict:
    record = state.new_pending(
        session_id,
        cwd="/tmp/project",
        model="gpt-6-luna",
        permission_mode="bypassPermissions",
        transcript_path="/tmp/rollout.jsonl",
        message=QUOTA_MESSAGE,
        reset_at=time.time() + 3600,
        reset_source="transcript_message",
        interruption_at=time.time() - 60,
    )
    record.update(overrides)
    return record


class StatusTests(StateCase):
    def test_no_pending_sessions(self) -> None:
        code, out, _ = run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("enabled", out)
        self.assertIn("no pending sessions", out)

    def test_disabled_is_reported(self) -> None:
        state.set_enabled(False)
        _, out, _ = run_cli("status")
        self.assertIn("disabled", out)

    def test_pending_details(self) -> None:
        state.write_pending(pending("01a13e0c-d592-7401-b638-b8b61003c313"))
        code, out, _ = run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("01a13e0c-d592-7401-b638-b8b61003c313", out)
        self.assertIn("waiting_for_quota", out)
        self.assertIn("reset_at:", out)
        self.assertIn("resume_at:", out)

    def test_json_output_is_machine_readable(self) -> None:
        state.write_pending(pending("sid"))
        _, out, _ = run_cli("status", "--json")
        payload = json.loads(out)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["version"], __version__)
        self.assertEqual(payload["pending"][0]["session_id"], "sid")
        self.assertEqual(payload["pending"][0]["state"], "waiting_for_quota")

    def test_json_reports_helper_state(self) -> None:
        _, out, _ = run_cli("status", "--json")
        payload = json.loads(out)
        self.assertFalse(payload["helper_running"])
        self.assertEqual(payload["helper_state"], "idle")
        self.assertIsNone(payload["helper_pid"])

    def test_json_reports_a_running_helper(self) -> None:
        from codex_auto_resume import helper

        helper.write_pid()
        with mock.patch.object(
            paths, "process_cmdline", return_value="python3 -m codex_auto_resume helper"
        ):
            _, out, _ = run_cli("status", "--json")
        payload = json.loads(out)
        self.assertTrue(payload["helper_running"])
        self.assertEqual(payload["helper_state"], "running")
        self.assertEqual(payload["helper_pid"], os.getpid())

    def test_status_does_not_report_a_recycled_pid_as_the_helper(self) -> None:
        from codex_auto_resume import helper

        helper.write_pid()
        with mock.patch.object(
            paths, "process_cmdline", return_value="/root/cli-persistence/bin/codex-auto-resume status"
        ):
            code, out, _ = run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("helper: not running", out)

    def test_no_secrets_or_prompts_in_status(self) -> None:
        state.write_pending(pending("sid"))
        _, out, _ = run_cli("status", "--json")
        self.assertNotIn("token", out.lower())
        self.assertNotIn("prompt", out.lower())


class EnableDisableTests(StateCase):
    def test_disable_then_enable(self) -> None:
        code, _, _ = run_cli("disable")
        self.assertEqual(code, 0)
        self.assertFalse(state.is_enabled())
        run_cli("enable")
        self.assertTrue(state.is_enabled())

    def test_disable_keeps_pending_state(self) -> None:
        state.write_pending(pending("sid"))
        run_cli("disable")
        self.assertIsNotNone(state.read_pending("sid"))

    def test_enable_restarts_the_helper_when_work_is_pending(self) -> None:
        state.write_pending(pending("sid"))
        with mock.patch("codex_auto_resume.hooks._ensure_helper_running") as ensure:
            run_cli("enable")
        ensure.assert_called_once()

    def test_enable_without_pending_work_does_not_start_the_helper(self) -> None:
        with mock.patch("codex_auto_resume.hooks._ensure_helper_running") as ensure:
            run_cli("enable")
        ensure.assert_not_called()


class CancelTests(StateCase):
    def test_cancel_by_session_id(self) -> None:
        state.write_pending(pending("sid"))
        code, out, _ = run_cli("cancel", "sid")
        self.assertEqual(code, 0)
        self.assertIn("cancelled", out)
        self.assertIsNone(state.read_pending("sid"))
        self.assertTrue(state.is_cancelled("sid", time.time() - 10))
        self.assertFalse(state.is_cancelled("sid", time.time() + 100))

    def test_cancel_all(self) -> None:
        for session_id in ("a", "b"):
            state.write_pending(pending(session_id))
        code, _, _ = run_cli("cancel", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(state.iter_pending(), [])

    def test_cancel_all_with_nothing_pending(self) -> None:
        code, out, _ = run_cli("cancel", "--all")
        self.assertEqual(code, 0)
        self.assertIn("no pending sessions", out)

    def test_cancel_without_target_is_a_usage_error(self) -> None:
        code, _, err = run_cli("cancel")
        self.assertEqual(code, 2)
        self.assertIn("session id", err)

    def test_cancel_unknown_session_writes_a_tombstone(self) -> None:
        code, _, _ = run_cli("cancel", "never-seen")
        self.assertEqual(code, 0)
        self.assertTrue(state.is_cancelled("never-seen", time.time() - 10))


class LogsTests(StateCase):
    def test_logs_are_structured(self) -> None:
        log.log("unit_test_event", session_id="sid", count=2)
        code, out, _ = run_cli("logs", "-n", "5")
        self.assertEqual(code, 0)
        self.assertIn("unit_test_event", out)
        self.assertIn("sid", out)

    def test_logs_json_mode(self) -> None:
        log.log("unit_test_event", session_id="sid")
        _, out, _ = run_cli("logs", "--json")
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertTrue(any(record["event"] == "unit_test_event" for record in records))

    def test_empty_log(self) -> None:
        code, out, _ = run_cli("logs")
        self.assertEqual(code, 0)
        self.assertIn("no log records", out)

    def test_secret_values_are_scrubbed(self) -> None:
        log.log("auth", access_token="super-secret", nested={"api_key": "nope"})
        _, out, _ = run_cli("logs", "--json")
        self.assertNotIn("super-secret", out)
        self.assertNotIn("nope", out)
        self.assertIn("redacted", out)


class DoctorTests(StateCase):
    def run_doctor(self) -> tuple[int, str]:
        """Run doctor with the host-specific checks stubbed out."""
        from contextlib import ExitStack

        from codex_auto_resume import service

        patches = [
            mock.patch("codex_auto_resume.codexenv.resolve_codex_binary", return_value="/bin/true"),
            mock.patch(
                "codex_auto_resume.codexenv.read_config",
                return_value={
                    "features": {"plugins": True},
                    "approval_policy": "never",
                },
            ),
            mock.patch(
                "codex_auto_resume.trust.plugin_hook_status",
                return_value=[{"key": "k", "trust_status": "trusted", "trusted": True}],
            ),
            mock.patch(
                "codex_auto_resume.ratelimit.query",
                return_value={"ordinary_usage_allowed": True, "windows": []},
            ),
            mock.patch.object(
                service,
                "service_status",
                return_value={
                    "backend": "systemd",
                    "display": "systemd --user",
                    "installed": True,
                    "active": "inactive",
                    "enabled": "enabled",
                    "artifact": "unit",
                    "detail": "",
                    "start_hint": "systemctl --user start codex-auto-resume-helper.service",
                },
            ),
            mock.patch.object(service, "available", return_value=(True, "systemd available")),
        ]
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            code, out, _ = run_cli("doctor")
        return code, out

    def test_doctor_reports_missing_codex(self) -> None:
        with mock.patch("codex_auto_resume.codexenv.resolve_codex_binary", return_value=None):
            code, out, _ = run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("codex binary: not found", out)

    def test_doctor_reports_a_problem_when_hooks_are_untrusted(self) -> None:
        with mock.patch("codex_auto_resume.codexenv.resolve_codex_binary", return_value="/bin/true"), mock.patch(
            "codex_auto_resume.trust.plugin_hook_status",
            return_value=[{"key": "k", "trust_status": "untrusted", "trusted": False}],
        ), mock.patch("codex_auto_resume.ratelimit.query", return_value=None):
            code, out, _ = run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("plugin hooks trusted", out)

    def test_doctor_is_green_when_idle(self) -> None:
        code, out = self.run_doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] helper state: healthy (idle", out)
        self.assertIn("doctor: all checks passed", out)

    def test_doctor_reconciles_a_stale_pid_file(self) -> None:
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {"pid": 999_999_999, "pid_start": 1.0, "started_at": 0.0},
        )
        code, out = self.run_doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] helper state: healthy (idle: removed stale pid marker", out)
        self.assertFalse(paths.helper_pid_file().exists())

    def test_doctor_reconciles_a_malformed_pid_file(self) -> None:
        paths.helper_pid_file().write_text("not a pid record", encoding="utf-8")
        code, out = self.run_doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("removed invalid pid marker", out)
        self.assertFalse(paths.helper_pid_file().exists())

    def test_doctor_is_green_with_a_running_helper(self) -> None:
        from codex_auto_resume import helper

        helper.write_pid()
        with mock.patch.object(
            paths, "process_cmdline", return_value="python3 -m codex_auto_resume helper"
        ):
            code, out = self.run_doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("[ok] helper state: healthy (running: pid", out)
        self.assertIn(str(os.getpid()), out)

    def test_doctor_fails_when_a_recovery_is_orphaned(self) -> None:
        state.write_pending(pending("sid"))
        code, out = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("[!!] helper state: not running with 1 active recovery", out)
        self.assertIn("doctor: problems found", out)
        self.assertIsNotNone(state.read_pending("sid"), "doctor must not delete pending state")

    def test_doctor_accepts_a_paused_recovery_while_disabled(self) -> None:
        state.write_pending(pending("sid"))
        state.set_enabled(False)
        code, out = self.run_doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("helper state: healthy (idle", out)
        self.assertIn("paused while disabled", out)
        self.assertIsNotNone(state.read_pending("sid"))


class VersionTests(StateCase):
    def test_version(self) -> None:
        code, out, _ = run_cli("version")
        self.assertEqual(code, 0)
        self.assertIn("codex-auto-resume", out)

    def test_no_command_prints_help(self) -> None:
        code, out, _ = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("usage", out.lower())


class HookCommandTests(StateCase):
    def test_hook_subcommand_dispatches(self) -> None:
        with mock.patch.object(cli.hooks, "main", return_value=0) as handler:
            code, _, _ = run_cli("hook", "SessionEnd")
        self.assertEqual(code, 0)
        handler.assert_called_once_with("SessionEnd")


class HelperCommandTests(StateCase):
    def test_helper_subcommand_passes_binary(self) -> None:
        with mock.patch.object(cli.helper, "main", return_value=0) as run:
            run_cli("helper", "--codex-bin", "/usr/bin/codex")
        run.assert_called_once_with("/usr/bin/codex")


if __name__ == "__main__":
    unittest.main()
