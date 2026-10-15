"""Hook entry points: session registry, arming, cancellation, cleanup."""

from __future__ import annotations

import io
import json
import os
import sys
import time
import unittest
from unittest import mock

from support import QUOTA_MESSAGE, StateCase, task_complete
from codex_auto_resume import hooks, quota, state


def payload(session_id: str, transcript: str, event: str, **extra) -> dict:
    base = {
        "session_id": session_id,
        "transcript_path": transcript,
        "cwd": "/tmp/project",
        "hook_event_name": event,
        "model": "gpt-6-luna",
        "permission_mode": "bypassPermissions",
    }
    base.update(extra)
    return base


class HookCase(StateCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(hooks, "_ensure_helper_running")
        self.helper_running = patcher.start()
        self.addCleanup(patcher.stop)

    def run_hook(self, payload_dict: dict) -> int:
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload_dict))):
            return hooks.main(payload_dict.get("hook_event_name", "Unknown"))


class SessionStartTests(HookCase):
    def test_registers_session_with_host_pid(self) -> None:
        record = payload("sid", "/tmp/t.jsonl", "SessionStart", source="startup")
        self.assertEqual(self.run_hook(record), 0)
        stored = state.read_session("sid")
        self.assertEqual(stored["cwd"], "/tmp/project")
        self.assertTrue(stored["attached"])
        self.assertIsInstance(stored["host_pid"], int)
        self.assertEqual(stored["source"], "startup")

    def test_clears_pending_when_the_user_resumes_manually(self) -> None:
        state.write_pending(
            state.new_pending(
                "sid",
                cwd="/tmp/project",
                model=None,
                permission_mode=None,
                transcript_path="/tmp/t.jsonl",
                message=QUOTA_MESSAGE,
                reset_at=time.time() + 100,
                reset_source="transcript_message",
                interruption_at=time.time() - 10,
            )
        )
        self.run_hook(payload("sid", "/tmp/t.jsonl", "SessionStart", source="resume"))
        self.assertIsNone(state.read_pending("sid"))

    def test_keeps_pending_when_our_own_resume_starts(self) -> None:
        state.write_pending(
            state.new_pending(
                "sid",
                cwd=None,
                model=None,
                permission_mode=None,
                transcript_path=None,
                message=QUOTA_MESSAGE,
                reset_at=time.time() + 100,
                reset_source="transcript_message",
                interruption_at=time.time() - 10,
            )
        )
        state.mark_launching("sid")
        self.run_hook(payload("sid", "/tmp/t.jsonl", "SessionStart", source="resume"))
        self.assertIsNotNone(state.read_pending("sid"))

    def test_empty_session_id_is_ignored(self) -> None:
        self.assertEqual(self.run_hook({"hook_event_name": "SessionStart"}), 0)
        self.assertEqual(list(state.iter_sessions()), [])


class SessionEndTests(HookCase):
    def test_arms_recovery_after_a_quota_interruption(self) -> None:
        path = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=time.time())],
        )
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        pending = state.read_pending("sid")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["state"], state.STATE_WAITING)
        self.assertIsNotNone(pending["reset_at"])
        self.assertEqual(pending["reset_source"], "transcript_message")
        self.assertFalse(state.read_session("sid")["attached"])
        self.helper_running.assert_called()

    def test_does_not_arm_after_a_normal_turn(self) -> None:
        path = self.write_transcript("sid", [task_complete(at=time.time())])
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        self.assertIsNone(state.read_pending("sid"))

    def test_does_not_arm_after_a_network_error(self) -> None:
        path = self.write_transcript(
            "sid",
            [
                task_complete(
                    message="stream disconnected before completion",
                    error_info="response_stream_disconnected",
                    at=time.time(),
                )
            ],
        )
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        self.assertIsNone(state.read_pending("sid"))

    def test_does_not_arm_after_an_aborted_turn(self) -> None:
        path = self.write_transcript(
            "sid",
            [
                {
                    "timestamp": "2026-10-15T05:31:00Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "reason": "interrupted"},
                }
            ],
        )
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        self.assertIsNone(state.read_pending("sid"))

    def test_duplicate_session_end_keeps_one_record(self) -> None:
        path = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=1000.0)],
        )
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        first = state.read_pending("sid")
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        second = state.read_pending("sid")
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(len(state.iter_pending()), 1)

    def test_cancelled_interruption_is_not_re_armed(self) -> None:
        path = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=time.time())],
        )
        state.write_cancelled("sid", "user_cancel", time.time() + 5)
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        self.assertIsNone(state.read_pending("sid"))

    def test_clears_pending_when_the_session_ends_cleanly(self) -> None:
        path = self.write_transcript("sid", [task_complete(at=time.time())])
        state.write_pending(
            state.new_pending(
                "sid",
                cwd=None,
                model=None,
                permission_mode=None,
                transcript_path=str(path),
                message=QUOTA_MESSAGE,
                reset_at=time.time() + 100,
                reset_source="transcript_message",
                interruption_at=time.time() - 100,
            )
        )
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        self.assertIsNone(state.read_pending("sid"))

    def test_missing_transcript_does_not_arm(self) -> None:
        self.run_hook(payload("sid", "/tmp/does-not-exist.jsonl", "SessionEnd", reason="other"))
        self.assertIsNone(state.read_pending("sid"))

    def test_out_of_credits_is_recorded_but_not_resumable(self) -> None:
        path = self.write_transcript(
            "sid",
            [
                task_complete(
                    message="Your workspace is out of credits. Add credits to continue.",
                    error_info="usage_limit_exceeded",
                    at=time.time(),
                )
            ],
        )
        self.run_hook(payload("sid", str(path), "SessionEnd", reason="other"))
        pending = state.read_pending("sid")
        self.assertEqual(pending["state"], state.STATE_NOT_RESUMABLE)
        self.assertEqual(state.active_pending(), [])


class StopAndInterruptTests(HookCase):
    def test_stop_clears_a_waiting_recovery(self) -> None:
        path = self.write_transcript("sid", [task_complete(at=time.time())])
        state.write_pending(
            state.new_pending(
                "sid",
                cwd=None,
                model=None,
                permission_mode=None,
                transcript_path=str(path),
                message=QUOTA_MESSAGE,
                reset_at=time.time() + 100,
                reset_source="transcript_message",
                interruption_at=time.time(),
            )
        )
        self.run_hook(payload("sid", str(path), "Stop"))
        self.assertIsNone(state.read_pending("sid"))

    def test_stop_does_not_touch_a_record_that_is_resuming(self) -> None:
        path = self.write_transcript("sid", [task_complete(at=time.time())])
        record = state.new_pending(
            "sid",
            cwd=None,
            model=None,
            permission_mode=None,
            transcript_path=str(path),
            message=QUOTA_MESSAGE,
            reset_at=time.time() + 100,
            reset_source="transcript_message",
            interruption_at=time.time(),
        )
        record["state"] = state.STATE_RESUMING
        state.write_pending(record)
        self.run_hook(payload("sid", str(path), "Stop"))
        self.assertIsNotNone(state.read_pending("sid"))

    def test_interrupt_cancels_a_waiting_recovery(self) -> None:
        state.write_pending(
            state.new_pending(
                "sid",
                cwd=None,
                model=None,
                permission_mode=None,
                transcript_path=None,
                message=QUOTA_MESSAGE,
                reset_at=time.time() + 100,
                reset_source="transcript_message",
                interruption_at=time.time(),
            )
        )
        self.run_hook(payload("sid", "/tmp/t.jsonl", "Interrupt"))
        self.assertIsNone(state.read_pending("sid"))
        self.assertFalse(state.is_cancelled("sid", time.time() + 1000))

    def test_user_prompt_submit_keeps_pending(self) -> None:
        path = self.write_transcript("sid", [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded")])
        pending = state.new_pending(
            "sid",
            cwd=None,
            model=None,
            permission_mode=None,
            transcript_path=str(path),
            message=QUOTA_MESSAGE,
            reset_at=time.time() + 100,
            reset_source="transcript_message",
            interruption_at=time.time(),
        )
        state.write_pending(pending)
        self.run_hook(payload("sid", str(path), "UserPromptSubmit", turn_id="t1", prompt="hi"))
        self.assertIsNotNone(state.read_pending("sid"))


class RobustnessTests(HookCase):
    def test_unknown_event_is_a_noop(self) -> None:
        self.assertEqual(hooks.main("NotAnEvent"), 0)

    def test_invalid_stdin_does_not_raise(self) -> None:
        with mock.patch.object(sys, "stdin", io.StringIO("this is not json")):
            self.assertEqual(hooks.main("SessionEnd"), 0)

    def test_empty_stdin_does_not_raise(self) -> None:
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            self.assertEqual(hooks.main("SessionStart"), 0)

    def test_handler_exception_is_swallowed(self) -> None:
        with mock.patch.object(hooks, "handle_session_end", side_effect=RuntimeError("boom")):
            with mock.patch.object(sys, "stdin", io.StringIO("{}")):
                self.assertEqual(hooks.main("SessionEnd"), 0)


if __name__ == "__main__":
    unittest.main()
