"""Persistent state: atomicity, corruption tolerance, cancellation, cleanup."""

from __future__ import annotations

import json
import os
import time
import unittest

from support import StateCase
from codex_auto_resume import state


def new_record(session_id: str = "sid-1", **overrides) -> dict:
    record = state.new_pending(
        session_id,
        cwd="/tmp/project",
        model="gpt-6-luna",
        permission_mode="bypassPermissions",
        transcript_path="/tmp/rollout.jsonl",
        message="You\u2019ve hit your usage limit.",
        reset_at=time.time() + 3600,
        reset_source="transcript_message",
        interruption_at=time.time(),
    )
    record.update(overrides)
    return record


class SettingsTests(StateCase):
    def test_defaults_are_enabled(self) -> None:
        self.assertTrue(state.is_enabled())

    def test_disable_round_trip(self) -> None:
        state.set_enabled(False)
        self.assertFalse(state.is_enabled())
        state.set_enabled(True)
        self.assertTrue(state.is_enabled())

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX mode bits; %LOCALAPPDATA% ACLs apply")
    def test_settings_file_has_restrictive_permissions(self) -> None:
        state.set_enabled(False)
        mode = os.stat(self.paths.config_file()).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_unknown_keys_are_not_persisted(self) -> None:
        state.save_settings({"enabled": True, "continue_prompt": "x", "bogus": 1})
        stored = json.loads(self.paths.config_file().read_text())
        self.assertNotIn("bogus", stored)


class PendingTests(StateCase):
    def test_write_and_read(self) -> None:
        state.write_pending(new_record())
        stored = state.read_pending("sid-1")
        self.assertEqual(stored["state"], state.STATE_WAITING)
        self.assertEqual(stored["session_id"], "sid-1")

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        for _ in range(10):
            state.write_pending(new_record())
        leftovers = [p.name for p in self.paths.pending_dir().iterdir() if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_corrupt_record_is_ignored_not_fatal(self) -> None:
        state.write_pending(new_record())
        self.paths.pending_dir().joinpath("sid-1.json").write_text(
            "{ this is not json", encoding="utf-8"
        )
        self.assertIsNone(state.read_pending("sid-1"))
        self.assertEqual(state.active_pending(), [])

    def test_active_pending_sorted_by_resume_at(self) -> None:
        state.write_pending(new_record("later", resume_at=time.time() + 100))
        state.write_pending(new_record("sooner", resume_at=time.time() + 10))
        self.assertEqual([r["session_id"] for r in state.active_pending()], ["sooner", "later"])

    def test_terminal_records_are_not_active(self) -> None:
        state.write_pending(new_record("done", state=state.STATE_COMPLETED))
        state.write_pending(new_record("bad", state=state.STATE_FAILED))
        state.write_pending(new_record("nope", state=state.STATE_NOT_RESUMABLE))
        self.assertEqual(state.active_pending(), [])

    def test_multiple_sessions_are_independent(self) -> None:
        for session_id in ("a", "b", "c"):
            state.write_pending(new_record(session_id))
        self.assertEqual(len(state.iter_pending()), 3)
        state.clear_pending("b", "cancelled")
        self.assertEqual(sorted(r["session_id"] for r in state.iter_pending()), ["a", "c"])

    def test_clear_pending_is_idempotent(self) -> None:
        state.write_pending(new_record())
        state.clear_pending("sid-1", "completed")
        state.clear_pending("sid-1", "completed")
        self.assertIsNone(state.read_pending("sid-1"))

    def test_duplicate_write_keeps_one_record(self) -> None:
        state.write_pending(new_record())
        state.write_pending(new_record(attempts=3))
        records = state.iter_pending()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["attempts"], 3)


class CancellationTests(StateCase):
    def test_tombstone_covers_the_interruption_it_cancelled(self) -> None:
        interruption = 1000.0
        state.write_cancelled("sid", "user_cancel", interruption)
        self.assertTrue(state.is_cancelled("sid", interruption))
        self.assertTrue(state.is_cancelled("sid", interruption - 5))

    def test_newer_interruption_is_eligible_again(self) -> None:
        now = time.time()
        state.write_cancelled("sid", "user_cancel", now)
        self.assertFalse(state.is_cancelled("sid", now + 600))

    def test_unrelated_session_is_not_cancelled(self) -> None:
        state.write_cancelled("sid", "user_cancel", 1000.0)
        self.assertFalse(state.is_cancelled("other", 1000.0))

    def test_missing_tombstone(self) -> None:
        self.assertFalse(state.is_cancelled("sid", None))


class LaunchMarkerTests(StateCase):
    def test_marker_round_trip(self) -> None:
        state.mark_launching("sid", {"resume_at": 5})
        marker = state.read_launching("sid")
        self.assertIsNotNone(marker)
        self.assertEqual(marker["resume_at"], 5)
        state.clear_launching("sid")
        self.assertIsNone(state.read_launching("sid"))

    def test_stale_marker_expires(self) -> None:
        state.mark_launching("sid")
        marker = state.read_launching("sid", max_age_seconds=-1)
        self.assertIsNone(marker)


class HelperTests(StateCase):
    def test_backoff_is_bounded_and_growing(self) -> None:
        first = state.next_resume_delay(0)
        second = state.next_resume_delay(3)
        capped = state.next_resume_delay(50)
        self.assertLess(first, second)
        self.assertLessEqual(capped, state.MAX_AUTOMATIC_DELAY_SECONDS)
        self.assertGreaterEqual(capped, state.MIN_AUTOMATIC_DELAY_SECONDS)

    def test_new_pending_unknown_reset_uses_a_default_delay(self) -> None:
        now = 1_000_000.0
        record = state.new_pending(
            "sid",
            cwd=None,
            model=None,
            permission_mode=None,
            transcript_path=None,
            message=None,
            reset_at=None,
            reset_source="unknown",
            interruption_at=now,
            now=now,
        )
        self.assertEqual(record["resume_at"], now + state.DEFAULT_DELAY_SECONDS)


if __name__ == "__main__":
    unittest.main()
