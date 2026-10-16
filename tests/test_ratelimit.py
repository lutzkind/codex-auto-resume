"""Rate-limit snapshot handling and resume command construction."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from support import FAKE_CODEX, REPO_ROOT, StateCase
from codex_auto_resume import ratelimit, runner


class NormalizeTests(unittest.TestCase):
    def test_maps_camel_case_response(self) -> None:
        snapshot = ratelimit.normalize(
            {
                "ordinaryUsageAllowed": False,
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 100.0, "windowDurationMins": 300, "resetsAt": 1792050694},
                    "secondary": {"usedPercent": 90.0, "windowDurationMins": 10080, "resetsAt": 1792472674},
                    "planType": "plus",
                    "rateLimitReachedType": "rate_limit_reached",
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 100.0, "resetsAt": 1792050694},
                    }
                },
            }
        )
        self.assertFalse(snapshot["ordinary_usage_allowed"])
        self.assertEqual(snapshot["plan_type"], "plus")
        slots = {window["slot"] for window in snapshot["windows"]}
        self.assertEqual(slots, {"primary", "secondary"})

    def test_handles_missing_fields(self) -> None:
        snapshot = ratelimit.normalize({})
        self.assertIsNone(snapshot["ordinary_usage_allowed"])
        self.assertEqual(snapshot["windows"], [])

    def test_handles_null_window(self) -> None:
        snapshot = ratelimit.normalize({"rateLimits": {"primary": None, "secondary": None}})
        self.assertEqual(snapshot["windows"], [])


class AvailabilityTests(unittest.TestCase):
    def test_allowed_flag_wins(self) -> None:
        snapshot = {"ordinary_usage_allowed": True, "windows": [], "upsell_reset_at": None}
        verdict = ratelimit.availability(snapshot, 1000.0)
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["reason"], "ordinary_usage_allowed")

    def test_blocked_returns_next_reset(self) -> None:
        snapshot = {
            "ordinary_usage_allowed": False,
            "windows": [
                {"slot": "primary", "used_percent": 100.0, "resets_at": 2000.0},
                {"slot": "secondary", "used_percent": 100.0, "resets_at": 5000.0},
            ],
            "upsell_reset_at": None,
        }
        verdict = ratelimit.availability(snapshot, 1000.0)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["next_reset_at"], 2000.0)

    def test_past_reset_is_not_used(self) -> None:
        snapshot = {
            "ordinary_usage_allowed": False,
            "windows": [{"slot": "primary", "used_percent": 100.0, "resets_at": 500.0}],
            "upsell_reset_at": None,
        }
        verdict = ratelimit.availability(snapshot, 1000.0)
        self.assertIsNone(verdict["next_reset_at"])

    def test_upsell_reset_is_a_fallback(self) -> None:
        snapshot = {
            "ordinary_usage_allowed": False,
            "windows": [],
            "upsell_reset_at": 3000.0,
        }
        self.assertEqual(ratelimit.availability(snapshot, 1000.0)["next_reset_at"], 3000.0)

    def test_null_allowed_with_no_exhausted_window_is_optimistic(self) -> None:
        snapshot = {
            "ordinary_usage_allowed": None,
            "windows": [{"slot": "primary", "used_percent": 12.5, "resets_at": 2000.0}],
            "upsell_reset_at": None,
        }
        verdict = ratelimit.availability(snapshot, 1000.0)
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["reason"], "no_exhausted_window")

    def test_missing_snapshot_is_unknown(self) -> None:
        verdict = ratelimit.availability(None, 1000.0)
        self.assertIsNone(verdict["allowed"])
        self.assertEqual(verdict["reason"], "no_snapshot")


class ResumeCommandTests(unittest.TestCase):
    def test_command_uses_the_explicit_session_id(self) -> None:
        command = runner.build_resume_command("codex", "01a13e0c-d592-7401", "continue")
        self.assertIn("01a13e0c-d592-7401", command)
        self.assertEqual(command[-1], "continue")
        self.assertEqual(command[:2], ["codex", "exec"])

    def test_command_never_uses_last(self) -> None:
        for model in (None, "gpt-6-luna"):
            command = runner.build_resume_command("codex", "sid", "continue", model)
            self.assertNotIn("--last", command)
            self.assertNotIn("-c", command)

    def test_model_is_only_passed_when_it_differs(self) -> None:
        os.environ["CODEX_HOME"] = tempfile.mkdtemp(prefix="car-home-")
        try:
            Path(os.environ["CODEX_HOME"], "config.toml").write_text(
                'model = "gpt-6-luna"\n', encoding="utf-8"
            )
            self.assertIsNone(runner.effective_model("gpt-6-luna"))
            self.assertEqual(runner.effective_model("gpt-5.5"), "gpt-5.5")
            self.assertIsNone(runner.effective_model(None))
        finally:
            shutil.rmtree(os.environ["CODEX_HOME"], ignore_errors=True)

    def test_pending_resume_at_after_reset_uses_margin(self) -> None:
        self.assertEqual(runner.pending_resume_at(1000.0, 0, now=900.0, margin=10.0), 1010.0)

    def test_pending_resume_at_already_passed_is_immediate(self) -> None:
        self.assertEqual(runner.pending_resume_at(1000.0, 0, now=5000.0), 5000.0)

    def test_pending_resume_at_without_reset_uses_backoff(self) -> None:
        value = runner.pending_resume_at(None, 0, now=100.0)
        self.assertGreater(value, 100.0)


class EventClassificationTests(unittest.TestCase):
    def test_quota_error_event(self) -> None:
        found = runner._classify_event(
            {"type": "error", "message": "You\u2019ve hit your usage limit. Try again later."}
        )
        self.assertEqual(found[0], runner.RESULT_QUOTA)

    def test_turn_completed_event(self) -> None:
        self.assertEqual(runner._classify_event({"type": "turn.completed"})[0], runner.RESULT_COMPLETED)

    def test_other_turn_failed_is_an_error(self) -> None:
        found = runner._classify_event({"type": "turn.failed", "error": {"message": "boom"}})
        self.assertEqual(found[0], runner.RESULT_ERROR)

    def test_unrelated_events_are_ignored(self) -> None:
        self.assertIsNone(runner._classify_event({"type": "item.completed"}))
        self.assertIsNone(runner._classify_event({"type": "thread.started"}))

    def test_quota_message_on_error_event_is_quota(self) -> None:
        found = runner._classify_event({"type": "error", "message": "The usage limit has been reached"})
        self.assertEqual(found[0], runner.RESULT_QUOTA)


class RealFakeCodexTests(StateCase):
    """The fake CLI must actually behave like the surfaces we rely on."""

    def fake_bin(self) -> str:
        return make_fake_codex(self.tmp)

    def test_app_server_rate_limits_round_trip(self) -> None:
        scenario = self.tmp / "scenario.json"
        scenario.write_text(
            json.dumps(
                {
                    "ordinary_usage_allowed": False,
                    "windows": [{"slot": "primary", "used_percent": 100, "resets_at": 2000}],
                }
            ),
            encoding="utf-8",
        )
        os.environ["FAKE_CODEX_SCENARIO"] = str(scenario)
        try:
            snapshot = ratelimit.query(self.fake_bin(), timeout=20)
        finally:
            os.environ.pop("FAKE_CODEX_SCENARIO", None)
        self.assertIsNotNone(snapshot)
        self.assertFalse(snapshot["ordinary_usage_allowed"])
        self.assertEqual(snapshot["windows"][0]["resets_at"], 2000.0)

    def test_version_probe(self) -> None:
        from codex_auto_resume import codexenv

        self.assertEqual(codexenv.codex_version(self.fake_bin()), "codex-cli 0.0.0-fake")


def make_fake_codex(directory: Path) -> str:
    """Return an executable path for the fake Codex CLI.

    The repository copy is used directly: a checkout is executable by
    definition (CI runners execute steps from it), while ``/tmp`` can be
    mounted ``noexec`` — which is exactly how the self-hosted CI containers are
    configured. The temp-dir wrapper is only a fallback. On Windows the wrapper
    is a ``.cmd`` shim (the same shape an npm-managed Codex installs), so the
    runner's ``cmd.exe`` spawn path is exercised by the tests.
    """
    if sys.platform == "win32":
        target = directory / "codex.cmd"
        target.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{FAKE_CODEX}" %*\r\n',
            encoding="ascii",
        )
        return str(target)
    if os.access(FAKE_CODEX, os.X_OK):
        return str(FAKE_CODEX)
    target = directory / "codex"
    target.write_text(
        "#!/bin/sh\nexec {python} {script} \"$@\"\n".format(
            python=shlex.quote(sys.executable), script=shlex.quote(str(FAKE_CODEX))
        ),
        encoding="utf-8",
    )
    target.chmod(0o755)
    return str(target)


if __name__ == "__main__":
    unittest.main()
