"""Helper lifecycle: waiting, resuming the exact session, cleanup, safety."""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from support import QUOTA_MESSAGE, FakeClock, StateCase, task_complete
from test_ratelimit import make_fake_codex
from codex_auto_resume import helper, quota, ratelimit, runner, state


class HelperCase(StateCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock(time.time())
        self.clock.apply(helper)
        self.fake_bin = make_fake_codex(self.tmp)
        self.scenario_path = self.tmp / "scenario.json"
        self.argv_log = self.tmp / "argv.jsonl"
        self.resume_count = self.tmp / "resume-count.txt"
        self.addCleanup(lambda: os.environ.pop("FAKE_CODEX_SCENARIO", None))

    def write_scenario(self, **overrides) -> None:
        scenario = {
            "transcript": str(overrides.pop("transcript", self.tmp / "sessions" / "sid.jsonl")),
            "resume_outcome": "completed",
            "argv_log": str(self.argv_log),
            "resume_count_file": str(self.resume_count),
            "ordinary_usage_allowed": True,
            "windows": [],
        }
        scenario.update(overrides)
        self.scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
        os.environ["FAKE_CODEX_SCENARIO"] = str(self.scenario_path)

    def arm_pending(
        self,
        session_id: str = "sid",
        *,
        reset_at: float | None = None,
        resume_at: float | None = None,
        transcript: str | None = None,
        state_name: str = state.STATE_WAITING,
        attached: bool = False,
        host_pid: int | None = None,
        interruption_at: float | None = None,
    ) -> dict:
        record = state.new_pending(
            session_id,
            cwd=str(self.tmp),
            model=None,
            permission_mode="bypassPermissions",
            transcript_path=transcript or str(self.tmp / "sessions" / f"{session_id}.jsonl"),
            message=QUOTA_MESSAGE,
            reset_at=reset_at,
            reset_source="transcript_message" if reset_at else "unknown",
            interruption_at=interruption_at if interruption_at is not None else self.clock.time() - 10,
            now=self.clock.time(),
        )
        record["state"] = state_name
        if resume_at is not None:
            record["resume_at"] = resume_at
        state.write_pending(record)
        state.write_session(
            session_id,
            {
                "session_id": session_id,
                "cwd": str(self.tmp),
                "transcript_path": record["transcript_path"],
                "model": None,
                "permission_mode": "bypassPermissions",
                "attached": attached,
                "host_pid": host_pid,
                "host_pid_start": None,
            },
        )
        return record

    def run_helper(self) -> int:
        with mock.patch.object(helper.ratelimit, "query", side_effect=self.query):
            return helper.main(self.fake_bin)

    def query(self, codex_bin, codex_home=None, timeout=30.0, raw=False):  # noqa: ANN001
        scenario = json.loads(self.scenario_path.read_text())
        allowed = scenario.get("ordinary_usage_allowed")
        reset_epoch = scenario.get("reset_epoch")
        if reset_epoch and self.clock.time() >= reset_epoch:
            allowed = True
        return ratelimit.normalize(
            {
                "ordinaryUsageAllowed": allowed,
                "rateLimits": {
                    "limitId": "codex",
                    "primary": (
                        {
                            "usedPercent": 100 if allowed is False else 5,
                            "resetsAt": reset_epoch,
                        }
                        if reset_epoch
                        else None
                    ),
                },
            }
        )

    def resume_argv(self) -> list[list[str]]:
        if not self.argv_log.exists():
            return []
        return [json.loads(line) for line in self.argv_log.read_text().splitlines() if line.strip()]

    def resume_count_value(self) -> int:
        if not self.resume_count.exists():
            return 0
        return int(self.resume_count.read_text().strip() or "0")


class FullLifecycleTests(HelperCase):
    def test_quota_then_resume_after_reset_then_completion(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(
            transcript=str(transcript),
            resume_outcome="completed",
            ordinary_usage_allowed=True,
        )
        self.arm_pending(
            reset_at=self.clock.time() - 5,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
            interruption_at=self.clock.time() - 10,
        )

        self.assertEqual(self.run_helper(), 0)

        argv = self.resume_argv()
        self.assertEqual(len(argv), 1, "exactly one resume attempt")
        self.assertIn("resume", argv[0])
        self.assertIn("sid", argv[0])
        self.assertNotIn("--last", argv[0])
        self.assertIsNone(state.read_pending("sid"), "pending state cleared on completion")

    def test_waits_until_the_reset_before_resuming(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        reset_at = self.clock.time() + 3600
        self.arm_pending(reset_at=reset_at, resume_at=reset_at + 10, transcript=str(transcript))

        before = self.clock.time()
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(len(self.resume_argv()), 1)
        self.assertGreaterEqual(self.clock.value, reset_at)
        self.assertGreater(self.clock.value - before, 3000)

    def test_reset_already_passed_resumes_immediately(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time() - 90000)],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        self.arm_pending(
            reset_at=self.clock.time() - 86400,
            resume_at=self.clock.time() - 86400,
            transcript=str(transcript),
            interruption_at=self.clock.time() - 90000,
        )
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(len(self.resume_argv()), 1)
        self.assertEqual(self.clock.sleeps, [], "no waiting when the reset already passed")

    def test_repeated_quota_exhaustion_is_handled(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        first_reset = self.clock.time() + 60
        second_reset = self.clock.time() + 120

        calls = {"n": 0}
        original = runner.run_resume

        def flaky_resume(**kwargs):  # noqa: ANN003
            calls["n"] += 1
            self.write_scenario(
                transcript=str(transcript),
                resume_outcome="quota" if calls["n"] == 1 else "completed",
                reset_epoch=second_reset,
                ordinary_usage_allowed=calls["n"] == 2,
            )
            return original(**kwargs)

        self.write_scenario(
            transcript=str(transcript),
            resume_outcome="quota",
            reset_epoch=first_reset,
            ordinary_usage_allowed=False,
        )
        self.arm_pending(reset_at=first_reset, resume_at=first_reset, transcript=str(transcript))

        with mock.patch.object(runner, "run_resume", side_effect=flaky_resume):
            with mock.patch.object(helper.ratelimit, "query", side_effect=self.query):
                helper.main(self.fake_bin)

        self.assertEqual(calls["n"], 2, "the same session is resumed again after the next reset")
        self.assertIsNone(state.read_pending("sid"))

    def test_completion_cleanup_leaves_no_pending_state(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        self.arm_pending(reset_at=self.clock.time() - 1, resume_at=self.clock.time() - 1, transcript=str(transcript))
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(state.active_pending(), [])
        self.assertEqual(
            [p.name for p in self.paths.pending_dir().iterdir()], []
        )


class RestartTests(HelperCase):
    def test_helper_restart_then_reset_resumes_exactly_once(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        # A previous helper recorded the interruption and exited (service restart).
        self.arm_pending(reset_at=self.clock.time() + 120, resume_at=self.clock.time() + 130, transcript=str(transcript))
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 1)

    def test_machine_restart_with_passed_reset_recovers_immediately(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time() - 100000)],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        record = self.arm_pending(
            reset_at=self.clock.time() - 50000,
            resume_at=self.clock.time() - 50000,
            transcript=str(transcript),
            interruption_at=self.clock.time() - 100000,
        )
        self.assertEqual(record["state"], state.STATE_WAITING)
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_crash_during_resume_without_progress_is_replayed_once(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        self.arm_pending(
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
            state_name=state.STATE_RESUMING,
        )
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 1)

    def test_crash_during_resume_after_completion_does_not_replay(self) -> None:
        transcript = self.write_transcript("sid", [task_complete(at=self.clock.time())])
        self.write_scenario(transcript=str(transcript))
        self.arm_pending(
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
            state_name=state.STATE_RESUMING,
        )
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 0)
        self.assertIsNone(state.read_pending("sid"))

    def test_crash_during_resume_without_evidence_is_failed_not_replayed(self) -> None:
        transcript = self.tmp / "sessions" / "sid.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            json.dumps({"type": "session_meta", "payload": {"session_id": "sid"}}) + "\n",
            encoding="utf-8",
        )
        self.write_scenario(transcript=str(transcript))
        self.arm_pending(
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
            state_name=state.STATE_RESUMING,
        )
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 0)
        self.assertEqual(state.read_pending("sid")["state"], state.STATE_FAILED)


class SafetyTests(HelperCase):
    def test_session_a_is_resumed_not_the_newer_session_b(self) -> None:
        transcript_a = self.write_transcript(
            "session-a",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript_a), resume_outcome="completed")
        self.arm_pending(
            "session-a",
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript_a),
        )
        # Session B starts afterwards and has no pending recovery.
        state.write_session(
            "session-b",
            {"session_id": "session-b", "cwd": str(self.tmp), "attached": True, "host_pid": os.getpid()},
        )

        self.assertEqual(self.run_helper(), 0)
        argv = self.resume_argv()
        self.assertEqual(len(argv), 1)
        self.assertIn("session-a", argv[0])
        self.assertNotIn("session-b", argv[0])
        self.assertNotIn("--last", argv[0])
        self.assertIsNone(state.read_pending("session-a"))
        self.assertIsNone(state.read_pending("session-b"))

    def test_disabled_means_no_resume(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript))
        self.arm_pending(reset_at=self.clock.time() - 1, resume_at=self.clock.time() - 1, transcript=str(transcript))
        state.set_enabled(False)
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 0)
        self.assertIsNotNone(state.read_pending("sid"), "state is kept while disabled")

    def test_cancelled_recovery_is_never_resumed(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript))
        self.arm_pending(reset_at=self.clock.time() - 1, resume_at=self.clock.time() - 1, transcript=str(transcript))
        state.write_cancelled("sid", "user_cancel", self.clock.time() + 1)
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 0)
        self.assertIsNone(state.read_pending("sid"))

    def test_attached_session_is_not_resumed(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript))
        self.arm_pending(
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
            attached=True,
            host_pid=os.getpid(),
        )
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 0)
        self.assertIsNotNone(state.read_pending("sid"))

    def test_dead_host_process_is_treated_as_detached(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        self.arm_pending(
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
            attached=True,
            host_pid=999999,
        )
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 1)

    def test_user_progress_supersedes_the_recovery(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [
                task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time() - 500),
                task_complete(at=self.clock.time()),
            ],
        )
        self.write_scenario(transcript=str(transcript))
        self.arm_pending(
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
            interruption_at=self.clock.time() - 500,
        )
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 0)
        self.assertIsNone(state.read_pending("sid"))

    def test_non_quota_failure_marks_failed_and_stops(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="error", error_message="boom")
        self.arm_pending(reset_at=self.clock.time() - 1, resume_at=self.clock.time() - 1, transcript=str(transcript))
        self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 1, "no hammering after a hard failure")
        self.assertEqual(state.read_pending("sid")["state"], state.STATE_FAILED)

    def test_quota_without_reset_time_waits_instead_of_hammering(self) -> None:
        """A definitively blocked account with no reset time must not be retried."""
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="quota", ordinary_usage_allowed=False)
        self.arm_pending(reset_at=None, resume_at=self.clock.time() - 1, transcript=str(transcript))
        with mock.patch.object(helper, "MAX_ITERATIONS", 1):
            self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 0, "no attempt while the account is blocked")
        pending = state.read_pending("sid")
        self.assertEqual(pending["state"], state.STATE_WAITING)
        self.assertGreater(pending["resume_at"], self.clock.time())

    def test_permanently_blocked_account_is_eventually_marked_failed(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="quota", ordinary_usage_allowed=False)
        self.arm_pending(reset_at=None, resume_at=self.clock.time() - 1, transcript=str(transcript))
        with mock.patch.object(helper, "MAX_ATTEMPTS", 1):
            self.assertEqual(self.run_helper(), 0)
        self.assertEqual(self.resume_count_value(), 0)
        self.assertEqual(state.read_pending("sid")["state"], state.STATE_FAILED)

    def test_quota_without_reset_time_retries_once_the_account_works(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [
                task_complete(
                    message="You\u2019ve hit your usage limit. Try again later.",
                    error_info="usage_limit_exceeded",
                    at=self.clock.time(),
                )
            ],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed", ordinary_usage_allowed=False)
        self.arm_pending(reset_at=None, resume_at=self.clock.time() - 1, transcript=str(transcript))

        calls = {"n": 0}
        original_query = self.query

        def flaky_query(*args, **kwargs):  # noqa: ANN002, ANN003
            calls["n"] += 1
            if calls["n"] >= 2:
                self.write_scenario(
                    transcript=str(transcript), resume_outcome="completed", ordinary_usage_allowed=True
                )
            return original_query(*args, **kwargs)

        with mock.patch.object(helper.ratelimit, "query", side_effect=flaky_query):
            helper.main(self.fake_bin)
        self.assertEqual(self.resume_count_value(), 1)

    def test_app_server_reset_is_used_when_the_message_has_no_time(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [
                task_complete(
                    message="You\u2019ve hit your usage limit. Try again later.",
                    error_info="usage_limit_exceeded",
                    at=self.clock.time(),
                )
            ],
        )
        reset_epoch = self.clock.time() + 7200
        self.write_scenario(
            transcript=str(transcript),
            resume_outcome="completed",
            reset_epoch=reset_epoch,
            ordinary_usage_allowed=False,
        )
        record = self.arm_pending(
            reset_at=None, resume_at=self.clock.time() - 1, transcript=str(transcript)
        )
        self.assertIsNone(record["reset_at"])
        self.assertEqual(self.run_helper(), 0)
        self.assertGreaterEqual(self.clock.value, reset_epoch)
        self.assertEqual(self.resume_count_value(), 1)

    def test_multiple_pending_sessions_are_resumed_by_explicit_id(self) -> None:
        transcripts = {}
        for session_id in ("session-a", "session-b"):
            transcripts[session_id] = self.write_transcript(
                session_id,
                [
                    task_complete(
                        message=QUOTA_MESSAGE,
                        error_info="usage_limit_exceeded",
                        at=self.clock.time(),
                    )
                ],
            )
            self.arm_pending(
                session_id,
                reset_at=self.clock.time() - 1,
                resume_at=self.clock.time() - 1,
                transcript=str(transcripts[session_id]),
            )
        self.write_scenario(
            transcript_template=str(self.tmp / "sessions" / "{session_id}.jsonl"),
            resume_outcome="completed",
        )
        self.assertEqual(self.run_helper(), 0)
        resumed = []
        for argv in self.resume_argv():
            self.assertNotIn("--last", argv)
            resumed.append(argv[argv.index("resume") + 3])
        self.assertEqual(sorted(resumed), ["session-a", "session-b"])
        self.assertEqual(state.active_pending(), [])


if __name__ == "__main__":
    unittest.main()
