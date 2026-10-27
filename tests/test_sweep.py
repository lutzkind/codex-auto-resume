"""Detection sweep: catch a usage limit that fired no hook.

Codex runs no hook when a turn fails, and an interactive TUI session lives on
the shared app-server daemon, so quitting the client does not fire SessionEnd
either. The helper therefore also inspects Codex's own session data.
"""

from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from support import QUOTA_MESSAGE, FakeClock, StateCase, task_complete, task_started
from test_helper import HelperCase
from codex_auto_resume import helper, paths, quota, state


class SweepCase(HelperCase):
    def rollout(self, session_id: str, events: list[dict], cwd: str = "/tmp/project"):
        """Write a transcript where Codex really writes them."""
        directory = paths.codex_home() / "sessions" / "2026" / "10" / "15"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-2026-10-15T05-30-21-{session_id}.jsonl"
        lines = [
            json.dumps(
                {
                    "timestamp": "2026-10-15T05:30:21.864Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": session_id,
                        "id": session_id,
                        "cwd": cwd,
                        "cli_version": "0.162.1",
                        "source": "cli",
                    },
                }
            )
        ]
        lines.extend(json.dumps(event) for event in events)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class SweepArmTests(SweepCase):
    def test_arms_a_session_that_hit_its_quota_without_any_hook(self) -> None:
        path = self.rollout(
            "01a13e22-5cf5-7ea3-82ac-0cf58b42f9b3",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
            cwd="/tmp/opencode/car-tui",
        )
        self.assertEqual(helper.sweep(self.clock.time()), 1)
        pending = state.read_pending("01a13e22-5cf5-7ea3-82ac-0cf58b42f9b3")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["state"], state.STATE_WAITING)
        self.assertEqual(pending["cwd"], "/tmp/opencode/car-tui")
        self.assertEqual(pending["transcript_path"], str(path))
        self.assertIsNotNone(pending["reset_at"])
        self.assertEqual(pending["reset_source"], "transcript_message")

    def test_sweep_is_idempotent(self) -> None:
        self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.assertEqual(helper.sweep(self.clock.time()), 1)
        created = state.read_pending("sid")["created_at"]
        self.assertEqual(helper.sweep(self.clock.time()), 0)
        self.assertEqual(state.read_pending("sid")["created_at"], created)
        self.assertEqual(len(state.iter_pending()), 1)

    def test_ignores_a_completed_turn(self) -> None:
        self.rollout("sid", [task_complete(at=self.clock.time())])
        self.assertEqual(helper.sweep(self.clock.time()), 0)
        self.assertIsNone(state.read_pending("sid"))

    def test_ignores_a_network_error(self) -> None:
        self.rollout(
            "sid",
            [
                task_complete(
                    message="stream disconnected before completion",
                    error_info="response_stream_disconnected",
                    at=self.clock.time(),
                )
            ],
        )
        self.assertEqual(helper.sweep(self.clock.time()), 0)
        self.assertIsNone(state.read_pending("sid"))

    def test_ignores_the_newest_event_even_if_an_older_one_was_quota(self) -> None:
        self.rollout(
            "sid",
            [
                task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time() - 60),
                task_complete(at=self.clock.time()),
            ],
        )
        self.assertEqual(helper.sweep(self.clock.time()), 0)

    def test_ignores_a_quota_error_while_a_newer_turn_is_still_running(self) -> None:
        self.rollout(
            "sid",
            [
                task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time() - 600),
                task_started(at=self.clock.time() - 60, turn_id="manual-turn"),
            ],
        )
        self.assertEqual(helper.sweep(self.clock.time()), 0)
        self.assertIsNone(state.read_pending("sid"))

    def test_ignores_a_cancelled_interruption(self) -> None:
        self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        state.write_cancelled("sid", "user_cancel", self.clock.time() + 1)
        self.assertEqual(helper.sweep(self.clock.time()), 0)
        self.assertIsNone(state.read_pending("sid"))

    def test_ignores_a_stale_interruption(self) -> None:
        at = self.clock.time() - helper.MAX_INTERRUPTION_AGE_SECONDS - 600
        self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=at)],
        )
        self.assertEqual(helper.sweep(self.clock.time()), 0)
        self.assertIsNone(state.read_pending("sid"))

    def test_records_credits_exhaustion_as_not_resumable(self) -> None:
        self.rollout(
            "sid",
            [
                task_complete(
                    message="Your workspace is out of credits. Add credits to continue.",
                    error_info="usage_limit_exceeded",
                    at=self.clock.time(),
                )
            ],
        )
        self.assertEqual(helper.sweep(self.clock.time()), 1)
        self.assertEqual(state.read_pending("sid")["state"], state.STATE_NOT_RESUMABLE)
        self.assertEqual(state.active_pending(), [])

    def test_arms_two_different_sessions(self) -> None:
        for session_id in ("session-a", "session-b"):
            self.rollout(
                session_id,
                [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
            )
        self.assertEqual(helper.sweep(self.clock.time()), 2)
        self.assertEqual(len(state.active_pending()), 2)

    def test_registered_session_without_a_rollout_is_skipped(self) -> None:
        state.write_session(
            "sid", {"session_id": "sid", "transcript_path": str(self.tmp / "missing.jsonl")}
        )
        self.assertEqual(helper.sweep(self.clock.time()), 0)

    def test_unreadable_transcript_is_skipped(self) -> None:
        path = self.rollout("sid", [task_complete(at=self.clock.time())])
        broken = path.with_name("rollout-2026-10-15T05-31-00-broken.jsonl")
        broken.write_bytes(b"\xff\xfe not utf8")
        self.assertEqual(helper.sweep(self.clock.time()), 0)

    def test_old_rollouts_are_not_scanned(self) -> None:
        path = self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        old = self.clock.time() - helper.MAX_INTERRUPTION_AGE_SECONDS - 3600
        import os

        os.utime(path, (old, old))
        self.assertEqual(helper.sweep(self.clock.time()), 0)


class AttachedTests(SweepCase):
    def test_any_session_attached_is_true_for_a_live_process(self) -> None:
        state.write_session(
            "sid",
            {
                "session_id": "sid",
                "attached": True,
                "host_pid": self_ish_pid(),
                "host_pid_start": paths.process_start_time(self_ish_pid()),
            },
        )
        self.assertTrue(helper.any_session_attached())

    def test_any_session_attached_is_false_for_a_dead_process(self) -> None:
        state.write_session(
            "sid",
            {"session_id": "sid", "attached": True, "host_pid": 999_999_999, "host_pid_start": 1.0},
        )
        self.assertFalse(helper.any_session_attached())

    def test_any_session_attached_is_false_after_session_end(self) -> None:
        state.write_session(
            "sid",
            {
                "session_id": "sid",
                "attached": False,
                "host_pid": self_ish_pid(),
                "host_pid_start": paths.process_start_time(self_ish_pid()),
            },
        )
        self.assertFalse(helper.any_session_attached())


def self_ish_pid() -> int:
    import os

    return os.getpid()


class WaitWakeTests(SweepCase):
    def test_waking_while_waiting_never_resumes_early(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        reset_at = self.clock.time() + 3600
        self.arm_pending(reset_at=reset_at, resume_at=reset_at, transcript=str(transcript))

        waiter = helper.Waiter()
        shutdown = helper.Shutdown()
        pending = state.read_pending("sid")
        original_sleep = waiter.sleep_until

        def sleep_with_two_wakes(deadline, should_stop=None):
            waiter.woken = True
            original_sleep(deadline, should_stop=should_stop)

        with mock.patch.object(waiter, "sleep_until", side_effect=sleep_with_two_wakes):
            result = helper.wait_for_resume(pending, waiter, shutdown)
        waiter.close()
        self.assertIsNotNone(result, "the wait must end at the resume time, not earlier")
        self.assertGreaterEqual(self.clock.value, reset_at)

    def test_cancelling_while_waiting_stops_the_recovery(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript))
        self.arm_pending(
            reset_at=self.clock.time() + 3600,
            resume_at=self.clock.time() + 3600,
            transcript=str(transcript),
        )
        waiter = helper.Waiter()

        def cancel_then_sleep(deadline, should_stop=None):  # noqa: ANN001
            state.write_cancelled("sid", "user_cancel", self.clock.time() + 10)
            return None

        with mock.patch.object(waiter, "sleep_until", side_effect=cancel_then_sleep):
            result = helper.wait_for_resume(
                state.read_pending("sid"), waiter, helper.Shutdown()
            )
        waiter.close()
        self.assertIsNone(result)
        self.assertIsNone(state.read_pending("sid"))

    def test_disabling_while_waiting_stops_the_wait_but_keeps_the_recovery(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        reset_at = self.clock.time() + 3600
        self.arm_pending(reset_at=reset_at, resume_at=reset_at, transcript=str(transcript))
        waiter = helper.Waiter()

        def disable_then_sleep(deadline, should_stop=None):  # noqa: ANN001
            state.set_enabled(False)
            return None

        with mock.patch.object(waiter, "sleep_until", side_effect=disable_then_sleep):
            result = helper.wait_for_resume(
                state.read_pending("sid"), waiter, helper.Shutdown()
            )
        waiter.close()
        self.assertIsNone(result, "a disable must cancel the in-flight wait")
        self.assertLess(self.clock.value, reset_at, "it must not wait until the reset")
        self.assertIsNotNone(
            state.read_pending("sid"), "the recovery is kept so enable can continue it"
        )

    def test_disabled_helper_resumes_nothing_and_keeps_state(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        self.arm_pending(
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
        )
        state.set_enabled(False)
        with mock.patch.object(helper, "MAX_ITERATIONS", 2):
            self.assertEqual(helper.main(self.fake_bin), 0)
        self.assertEqual(self.resume_count_value(), 0)
        self.assertIsNotNone(state.read_pending("sid"))


class CliWakeTests(SweepCase):
    def run_cli(self, func, args) -> None:  # noqa: ANN001
        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()):
            func(args)

    def test_disable_wakes_a_running_helper(self) -> None:
        from codex_auto_resume import cli

        with mock.patch.object(cli.helper, "notify_helper") as notify:
            self.run_cli(cli.cmd_disable, mock.Mock())
        notify.assert_called_once()

    def test_cancel_wakes_a_running_helper(self) -> None:
        from codex_auto_resume import cli

        self.arm_pending()
        with mock.patch.object(cli.helper, "notify_helper") as notify:
            self.run_cli(cli.cmd_cancel, mock.Mock(all=True, session_id=None))
        notify.assert_called_once()

    def test_enable_starts_or_wakes_the_helper_when_work_is_pending(self) -> None:
        from codex_auto_resume import cli

        self.arm_pending()
        with mock.patch.object(cli.helper, "notify_helper") as notify, mock.patch.object(
            cli.hooks, "_ensure_helper_running"
        ) as ensure:
            self.run_cli(cli.cmd_enable, mock.Mock())
        notify.assert_called_once()
        ensure.assert_called_once()


class MainLoopTests(SweepCase):
    def test_exits_when_there_is_nothing_to_watch(self) -> None:
        self.write_scenario()
        with mock.patch.object(helper.ratelimit, "query", side_effect=self.query):
            self.assertEqual(helper.main(self.fake_bin), 0)
        self.assertEqual(self.clock.sleeps, [])

    def test_attached_session_does_not_busy_loop(self) -> None:
        """While the user is attached the helper must wait, not spin."""
        import os

        transcript = self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), ordinary_usage_allowed=False)
        state.write_session(
            "sid",
            {
                "session_id": "sid",
                "attached": True,
                "host_pid": os.getpid(),
                "host_pid_start": paths.process_start_time(os.getpid()),
                "transcript_path": str(transcript),
            },
        )
        started = self.clock.time()
        with mock.patch.object(helper.ratelimit, "query", side_effect=self.query):
            with mock.patch.object(helper, "MAX_ITERATIONS", 3):
                self.assertEqual(helper.main(self.fake_bin), 0)
        self.assertEqual(self.resume_count_value(), 0, "nothing may be resumed while attached")
        self.assertGreaterEqual(
            self.clock.value - started,
            helper.MIN_ATTACHED_RECHECK,
            "the helper must wait between checks instead of spinning",
        )
        self.assertIsNotNone(state.read_pending("sid"))

    def test_watch_loop_arms_then_waits_for_the_reset(self) -> None:
        transcript = self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        self.write_scenario(transcript=str(transcript), resume_outcome="completed")
        with mock.patch.object(helper.ratelimit, "query", side_effect=self.query):
            self.assertEqual(helper.main(self.fake_bin), 0)
        self.assertEqual(self.resume_count_value(), 1)
        self.assertIsNone(state.read_pending("sid"))


class DetachTests(SweepCase):
    def test_recently_written_transcript_without_a_registry_entry_is_not_detached(self) -> None:
        path = self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        pending = state.new_pending(
            "sid",
            cwd=None,
            model=None,
            permission_mode=None,
            transcript_path=str(path),
            message=QUOTA_MESSAGE,
            reset_at=self.clock.time() + 60,
            reset_source="transcript_message",
            interruption_at=self.clock.time(),
            now=self.clock.time(),
        )
        settings = {"resume_when_attached": False}
        detached, reason = helper.session_detached(pending, None, settings, self.clock.time())
        self.assertFalse(detached)
        self.assertEqual(reason, "transcript_active")

    def test_idle_transcript_without_a_registry_entry_is_detached(self) -> None:
        import os

        path = self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        old = self.clock.time() - helper.TRANSCRIPT_IDLE_SECONDS - 60
        os.utime(path, (old, old))
        pending = state.new_pending(
            "sid",
            cwd=None,
            model=None,
            permission_mode=None,
            transcript_path=str(path),
            message=QUOTA_MESSAGE,
            reset_at=self.clock.time() + 60,
            reset_source="transcript_message",
            interruption_at=self.clock.time(),
            now=self.clock.time(),
        )
        settings = {"resume_when_attached": False}
        detached, reason = helper.session_detached(pending, None, settings, self.clock.time())
        self.assertTrue(detached)
        self.assertEqual(reason, "no_session_registry_entry")

    def test_resume_when_attached_setting_overrides_everything(self) -> None:
        detached, reason = helper.session_detached({}, {"attached": True, "host_pid": 1}, {"resume_when_attached": True}, 0.0)
        self.assertTrue(detached)
        self.assertEqual(reason, "resume_when_attached")

    def test_session_on_the_shared_daemon_falls_back_to_transcript_idle(self) -> None:
        """An interactive TUI's hook parent is the long-lived daemon."""
        path = self.rollout(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        pending = state.new_pending(
            "sid",
            cwd=None,
            model=None,
            permission_mode=None,
            transcript_path=str(path),
            message=QUOTA_MESSAGE,
            reset_at=self.clock.time() + 60,
            reset_source="transcript_message",
            interruption_at=self.clock.time(),
            now=self.clock.time(),
        )
        session = {"attached": True, "host_pid": 1, "host_pid_start": 1.0}
        settings = {"resume_when_attached": False}
        with mock.patch.object(helper.paths, "process_alive", return_value=True), mock.patch.object(
            helper, "is_shared_daemon", return_value=True
        ):
            detached, reason = helper.session_detached(pending, session, settings, self.clock.time())
            self.assertFalse(detached)
            self.assertEqual(reason, "daemon_hosted_transcript_active")
            detached, reason = helper.session_detached(
                pending, session, settings, self.clock.time() + helper.TRANSCRIPT_IDLE_SECONDS + 1
            )
            self.assertTrue(detached)
            self.assertEqual(reason, "daemon_hosted_transcript_idle")

    def test_real_client_host_is_attached_while_its_process_lives(self) -> None:
        path = self.rollout("sid", [task_complete(at=self.clock.time())])
        pending = state.new_pending(
            "sid",
            cwd=None,
            model=None,
            permission_mode=None,
            transcript_path=str(path),
            message=None,
            reset_at=None,
            reset_source="unknown",
            interruption_at=self.clock.time(),
            now=self.clock.time(),
        )
        session = {"attached": True, "host_pid": 1, "host_pid_start": 1.0}
        with mock.patch.object(helper.paths, "process_alive", return_value=True), mock.patch.object(
            helper, "is_shared_daemon", return_value=False
        ):
            detached, reason = helper.session_detached(pending, session, {}, self.clock.time())
        self.assertFalse(detached)
        self.assertEqual(reason, "session_attached")

    def test_daemon_cmdline_detection(self) -> None:
        with mock.patch.object(
            helper.paths,
            "process_cmdline",
            return_value="/root/.codex/packages/app-server-daemon/releases/0.162.1/bin/codex app-server --listen unix:// --managed-daemon",
        ):
            self.assertTrue(helper.is_shared_daemon(2867170))
        with mock.patch.object(
            helper.paths, "process_cmdline", return_value="/usr/local/bin/node /root/cli-persistence/bin/codex"
        ):
            self.assertFalse(helper.is_shared_daemon(1234))


class RequotaTests(SweepCase):
    def test_app_server_reset_is_preferred_over_a_stale_message(self) -> None:
        transcript = self.write_transcript(
            "sid",
            [task_complete(message=QUOTA_MESSAGE, error_info="usage_limit_exceeded", at=self.clock.time())],
        )
        fresh_reset = self.clock.time() + 4 * 3600
        self.write_scenario(
            transcript=str(transcript),
            resume_outcome="quota",
            reset_epoch=fresh_reset,
            ordinary_usage_allowed=False,
        )
        self.arm_pending(
            reset_at=self.clock.time() - 1,
            resume_at=self.clock.time() - 1,
            transcript=str(transcript),
        )
        with mock.patch.object(helper, "MAX_ITERATIONS", 1):
            with mock.patch.object(helper.ratelimit, "query", side_effect=self.query):
                helper.main(self.fake_bin)
        pending = state.read_pending("sid")
        self.assertEqual(pending["reset_source"], "app_server")
        self.assertEqual(pending["reset_at"], fresh_reset)


if __name__ == "__main__":
    unittest.main()
