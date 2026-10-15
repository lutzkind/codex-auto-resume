"""Quota detection and reset-time parsing."""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime

from support import QUOTA_MESSAGE, StateCase, task_complete
from codex_auto_resume import quota


class ParseRetryTimeTests(unittest.TestCase):
    def test_clock_time_only_uses_local_timezone(self) -> None:
        now = datetime(2026, 10, 15, 5, 30).timestamp()
        parsed = quota.parse_retry_time(QUOTA_MESSAGE, now)
        self.assertIsNotNone(parsed)
        local = datetime.fromtimestamp(parsed)
        self.assertEqual((local.hour, local.minute), (7, 51))
        self.assertGreater(parsed, now)

    def test_clock_time_already_passed_rolls_to_next_day(self) -> None:
        now = datetime(2026, 10, 15, 9, 0).timestamp()
        parsed = quota.parse_retry_time("try again at 7:51 AM.", now)
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, now)
        self.assertLess(parsed - now, 24 * 3600)

    def test_full_date_format(self) -> None:
        now = datetime(2026, 10, 15, 5, 30).timestamp()
        parsed = quota.parse_retry_time(
            "You\u2019ve hit your usage limit. Try again at Nov 3rd, 2026 4:07 PM.", now
        )
        expected = datetime(2026, 11, 3, 16, 7).timestamp()
        self.assertEqual(parsed, expected)

    def test_full_date_does_not_roll_forward(self) -> None:
        now = datetime(2026, 10, 15, 5, 30).timestamp()
        parsed = quota.parse_retry_time(
            "try again at Oct 1st, 2026 1:00 AM.", now
        )
        self.assertEqual(parsed, datetime(2026, 10, 1, 1, 0).timestamp())

    def test_iso_with_offset(self) -> None:
        parsed = quota.parse_retry_time("try again at 2026-10-15T07:51:00+02:00")
        self.assertEqual(parsed, datetime(2026, 10, 15, 5, 51).timestamp())

    def test_iso_z_suffix(self) -> None:
        parsed = quota.parse_retry_time("try again at 2026-10-15T07:51:00Z")
        expected = datetime(2026, 10, 15, 7, 51).timestamp()
        self.assertAlmostEqual(parsed, expected, delta=1)

    def test_no_retry_time(self) -> None:
        self.assertIsNone(quota.parse_retry_time("You\u2019ve hit your usage limit. Try again later."))
        self.assertIsNone(quota.parse_retry_time(""))
        self.assertIsNone(quota.parse_retry_time("connection reset by peer"))

    def test_malformed_time_is_rejected(self) -> None:
        self.assertIsNone(quota.parse_retry_time("try again at 99:99 ZZ."))
        self.assertIsNone(quota.parse_retry_time("try again at 13:00 PM."))

    def test_twelve_am_and_pm(self) -> None:
        now = datetime(2026, 10, 15, 0, 5).timestamp()
        self.assertEqual(
            datetime.fromtimestamp(quota.parse_retry_time("try again at 12:30 AM.", now)).hour,
            0,
        )
        now = datetime(2026, 10, 15, 11, 0).timestamp()
        self.assertEqual(
            datetime.fromtimestamp(quota.parse_retry_time("try again at 12:30 PM.", now)).hour,
            12,
        )


class ClassifyTests(unittest.TestCase):
    def test_structured_quota_error(self) -> None:
        self.assertEqual(
            quota.classify_error(QUOTA_MESSAGE, "usage_limit_exceeded"), "quota"
        )

    def test_structured_quota_error_without_message(self) -> None:
        self.assertEqual(quota.classify_error(None, "usage_limit_exceeded"), "quota")

    def test_text_only_fallback(self) -> None:
        self.assertEqual(quota.classify_error(QUOTA_MESSAGE, None), "quota")

    def test_credits_message_is_not_resumable(self) -> None:
        message = "Your workspace is out of credits. Add credits to continue."
        self.assertEqual(quota.classify_message(message), "quota_no_reset")

    def test_non_quota_errors_rejected(self) -> None:
        for message, info in [
            ("stream disconnected before completion", "response_stream_disconnected"),
            ("401 Unauthorized", "unauthorized"),
            ("context window exceeded", "context_window_exceeded"),
            ("sandbox denied", "sandbox_error"),
            ("connection reset by peer", None),
            ("model not found", None),
            ("", None),
        ]:
            self.assertIsNone(
                quota.classify_error(message, info), f"{info} / {message} must not be quota"
            )

    def test_unknown_structured_error_is_not_guessed_from_text(self) -> None:
        self.assertIsNone(
            quota.classify_error("You\u2019ve hit your usage limit.", "some_new_error")
        )


class LastTurnOutcomeTests(StateCase):
    def test_detects_quota_interruption(self) -> None:
        path = self.write_transcript("s1", [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded")])
        outcome = quota.last_turn_outcome(str(path))
        self.assertEqual(outcome["outcome"], quota.OUTCOME_QUOTA)
        self.assertTrue(outcome["resumable"])
        self.assertIsNotNone(outcome["reset_at"])

    def test_normal_completion_is_not_resumable(self) -> None:
        path = self.write_transcript("s2", [task_complete()])
        outcome = quota.last_turn_outcome(str(path))
        self.assertEqual(outcome["outcome"], quota.OUTCOME_COMPLETED)
        self.assertFalse(outcome["resumable"])

    def test_aborted_turn_is_not_resumable(self) -> None:
        path = self.write_transcript(
            "s3",
            [
                {
                    "timestamp": "2026-10-15T05:31:00Z",
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted", "reason": "interrupted"},
                }
            ],
        )
        outcome = quota.last_turn_outcome(str(path))
        self.assertEqual(outcome["outcome"], quota.OUTCOME_ABORTED)
        self.assertFalse(outcome["resumable"])

    def test_last_event_wins(self) -> None:
        path = self.write_transcript(
            "s4",
            [
                task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", turn_id="t1"),
                task_complete(turn_id="t2"),
            ],
        )
        self.assertEqual(quota.last_turn_outcome(str(path))["outcome"], quota.OUTCOME_COMPLETED)

    def test_malformed_event_is_tolerated(self) -> None:
        path = self.write_transcript("s5", [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded")])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{not json at all\n")
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
        outcome = quota.last_turn_outcome(str(path))
        self.assertEqual(outcome["outcome"], quota.OUTCOME_QUOTA)

    def test_truncated_tail_line_is_ignored(self) -> None:
        path = self.write_transcript("s6", [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded")])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"timestamp":"2026-10-15T05:35:00Z","type":"event_msg","payload":{"type":"task')
        outcome = quota.last_turn_outcome(str(path))
        self.assertEqual(outcome["outcome"], quota.OUTCOME_QUOTA)

    def test_missing_transcript(self) -> None:
        outcome = quota.last_turn_outcome(str(self.tmp / "nope.jsonl"))
        self.assertEqual(outcome["outcome"], quota.OUTCOME_UNKNOWN)
        self.assertFalse(outcome["resumable"])

    def test_large_transcript_tail_is_enough(self) -> None:
        events = [task_complete() for _ in range(200)]
        events.append(
            task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", turn_id="last")
        )
        path = self.write_transcript("s7", events)
        outcome = quota.last_turn_outcome(str(path), tail_bytes=1024 * 1024)
        self.assertEqual(outcome["outcome"], quota.OUTCOME_QUOTA)
        self.assertEqual(outcome["turn_id"], "last")

    def test_session_meta_is_read_from_the_head(self) -> None:
        path = self.write_transcript("s8", [task_complete()])
        meta = quota.session_meta(str(path))
        self.assertEqual(meta["session_id"], "s8")
        self.assertEqual(meta["cwd"], "/tmp/project")


if __name__ == "__main__":
    unittest.main()
