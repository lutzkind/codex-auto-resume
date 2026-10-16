"""Helper pid lifecycle: identity, stale reconciliation, cleanup, real helper.

The pid marker is only a wake-up address. These tests pin the rule that a
marker may never by itself mean "the helper is running": the process must be
positively identified as this helper, stale state must clean itself, and every
exit path (idle, signal, exception, kill) must converge on a healthy state.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from support import SRC, FakeClock, StateCase
from codex_auto_resume import cli, helper, paths, state

HELPER_CMDLINE = "python3 -m codex_auto_resume helper"
LAUNCHER_CMDLINE = "/root/codex-auto-resume/bin/codex-auto-resume helper"
STATUS_CMDLINE = "/root/cli-persistence/bin/codex-auto-resume status --json"
UNRELATED_CMDLINE = "nginx: worker process"


def wait_for(predicate, timeout: float = 15.0) -> bool:  # noqa: ANN001 - test helper
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class PidStateTests(StateCase):
    """The canonical liveness answer, without any real helper process."""

    def test_idle_installation_has_no_helper(self) -> None:
        verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_IDLE)
        self.assertEqual(verdict["reason"], "no_pid_file")
        self.assertFalse(helper.is_helper_running())
        self.assertFalse(cli._helper_running())
        # Reconciling an already-idle installation is a no-op.
        self.assertEqual(helper.reconcile_helper_pid()["state"], helper.PID_STATE_IDLE)
        self.assertFalse(paths.helper_pid_file().exists())

    def test_pid_file_records_owner_identity(self) -> None:
        helper.write_pid()
        record = paths.read_json(paths.helper_pid_file())
        self.assertIsNotNone(record)
        self.assertEqual(record["pid"], os.getpid())
        # /proc is Linux-only; elsewhere the start time is unknown on purpose.
        self.assertTrue(
            record["pid_start"] is None or isinstance(record["pid_start"], float)
        )
        self.assertEqual(record["version"], state.SCHEMA_VERSION)
        self.assertTrue(
            record["boot_id"] is None or isinstance(record["boot_id"], str)
        )

    def test_running_helper_is_detected(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_cmdline", return_value=HELPER_CMDLINE):
            verdict = helper.helper_pid_state()
            self.assertEqual(verdict["state"], helper.PID_STATE_RUNNING)
            self.assertEqual(verdict["pid"], os.getpid())
            self.assertTrue(helper.is_helper_running())
            self.assertTrue(cli._helper_running())

    def test_launcher_form_is_detected(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_cmdline", return_value=LAUNCHER_CMDLINE):
            self.assertTrue(helper.is_helper_running())

    def test_another_subcommand_is_not_the_helper(self) -> None:
        """A recycled pid that is another codex-auto-resume process is not ours."""
        helper.write_pid()
        with mock.patch.object(paths, "process_cmdline", return_value=STATUS_CMDLINE):
            verdict = helper.helper_pid_state()
            self.assertEqual(verdict["state"], helper.PID_STATE_STALE)
            self.assertEqual(verdict["reason"], "not_helper_process")
            self.assertFalse(helper.is_helper_running())
            helper.reconcile_helper_pid()
            self.assertFalse(paths.helper_pid_file().exists())

    def test_unrelated_live_process_is_rejected(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_alive", return_value=True), mock.patch.object(
            paths, "process_cmdline", return_value=UNRELATED_CMDLINE
        ):
            verdict = helper.helper_pid_state()
            self.assertEqual(verdict["state"], helper.PID_STATE_STALE)
            self.assertEqual(verdict["reason"], "not_helper_process")
            self.assertFalse(helper.is_helper_running())
            with mock.patch.object(os, "kill") as kill:
                self.assertFalse(helper.notify_helper())
            # The liveness probe itself may use kill(pid, 0) where /proc is
            # unavailable, so assert on the signal that was never sent.
            self.assertNotIn(
                getattr(signal, "SIGUSR1", None),
                [call[0][1] for call in kill.call_args_list],
                "must not signal an unrelated process",
            )

    def test_dead_pid_is_stale_and_cleaned(self) -> None:
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {"pid": 999_999_999, "pid_start": 1.0, "started_at": 0.0},
        )
        with mock.patch.object(paths, "process_alive", return_value=False), mock.patch.object(
            paths, "process_cmdline", return_value=HELPER_CMDLINE
        ):
            verdict = helper.helper_pid_state()
            self.assertEqual(verdict["state"], helper.PID_STATE_STALE)
            self.assertEqual(verdict["reason"], "dead_process")
            self.assertEqual(helper.reconcile_helper_pid()["state"], helper.PID_STATE_STALE)
            self.assertFalse(paths.helper_pid_file().exists())
        with mock.patch.object(paths, "process_cmdline", return_value=HELPER_CMDLINE):
            self.assertFalse(cli._helper_running())

    @unittest.skipUnless(os.path.isdir("/proc"), "requires /proc command lines")
    def test_reused_pid_of_a_real_live_process_is_rejected(self) -> None:
        """The marker points at a live unrelated process: never call it running."""
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {
                "pid": sleeper.pid,
                "pid_start": paths.process_start_time(sleeper.pid),
                "started_at": time.time(),
                "cmdline": "codex_auto_resume helper",
            },
        )
        verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_STALE)
        self.assertEqual(verdict["reason"], "not_helper_process")
        self.assertFalse(helper.is_helper_running())
        with mock.patch.object(os, "kill") as kill:
            self.assertFalse(helper.notify_helper())
            kill.assert_not_called()
        helper.reconcile_helper_pid()
        self.assertFalse(paths.helper_pid_file().exists())
        self.assertIsNone(sleeper.poll(), "the unrelated process must be left alone")

    def test_malformed_pid_file_is_invalid_and_cleaned(self) -> None:
        paths.helper_pid_file().write_text("this is not json", encoding="utf-8")
        verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_INVALID)
        self.assertEqual(verdict["reason"], "malformed_pid_file")
        self.assertFalse(helper.is_helper_running())
        helper.reconcile_helper_pid()
        self.assertFalse(paths.helper_pid_file().exists())

    def test_pid_record_without_a_usable_pid_is_invalid(self) -> None:
        paths.atomic_write_json(paths.helper_pid_file(), {"started_at": time.time()})
        verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_INVALID)
        self.assertEqual(verdict["reason"], "malformed_pid_record")

    @unittest.skipUnless(paths.boot_id(), "requires a readable /proc boot id")
    def test_marker_from_a_previous_boot_is_stale(self) -> None:
        helper.write_pid()
        record = paths.read_json(paths.helper_pid_file()) or {}
        record["boot_id"] = "00000000-0000-0000-0000-000000000000"
        paths.atomic_write_json(paths.helper_pid_file(), record)
        verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_STALE)
        self.assertEqual(verdict["reason"], "different_boot")

    @unittest.skipUnless(os.path.isdir("/proc"), "requires /proc start times")
    def test_start_time_mismatch_is_stale(self) -> None:
        helper.write_pid()
        record = paths.read_json(paths.helper_pid_file()) or {}
        record["pid_start"] = float(record["pid_start"] or 0) + 1000.0
        paths.atomic_write_json(paths.helper_pid_file(), record)
        verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_STALE)
        self.assertEqual(verdict["reason"], "pid_start_mismatch")

    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "SIGUSR1 is POSIX-only")
    def test_notify_helper_signals_the_identified_helper(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_alive", return_value=True), mock.patch.object(
            paths, "process_cmdline", return_value=HELPER_CMDLINE
        ):
            with mock.patch.object(os, "kill") as kill:
                self.assertTrue(helper.notify_helper())
                kill.assert_called_once()
                self.assertEqual(kill.call_args[0][1], signal.SIGUSR1)

    def test_notify_helper_ignores_a_dead_pid(self) -> None:
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {"pid": 999_999_999, "pid_start": 1.0, "started_at": 0.0},
        )
        with mock.patch.object(paths, "process_alive", return_value=False):
            with mock.patch.object(os, "kill") as kill:
                self.assertFalse(helper.notify_helper())
                kill.assert_not_called()

    def test_remove_own_pid_never_clobbers_another_helper(self) -> None:
        other = os.getpid() + 1
        paths.atomic_write_json(paths.helper_pid_file(), {"pid": other})
        helper.remove_own_pid()
        self.assertTrue(paths.helper_pid_file().exists())

    def test_remove_own_pid_removes_our_marker(self) -> None:
        helper.write_pid()
        helper.remove_own_pid()
        self.assertFalse(paths.helper_pid_file().exists())


class InProcessLifecycleTests(StateCase):
    """Exit paths that must not leave a marker behind, with a fake clock."""

    def setUp(self) -> None:
        super().setUp()
        self.clock = FakeClock(time.time())
        self.clock.apply(helper)

    def test_idle_exit_removes_pid_state(self) -> None:
        self.assertEqual(helper.main(None), 0)
        self.assertFalse(paths.helper_pid_file().exists())
        self.assertEqual(helper.helper_pid_state()["state"], helper.PID_STATE_IDLE)
        events = [record["event"] for record in cli.log.read_records(limit=50)]
        self.assertIn("helper_idle_exit", events)
        self.assertIn("helper_exited", events)

    def test_signal_shutdown_removes_pid_state(self) -> None:
        class ImmediateShutdown:
            def __init__(self) -> None:
                self.requested = False

            def install(self) -> None:
                self.requested = True

            def restore(self) -> None:
                pass

        with mock.patch.object(helper, "Shutdown", ImmediateShutdown):
            self.assertEqual(helper.main(None), 0)
        self.assertFalse(paths.helper_pid_file().exists())

    def test_exception_removes_pid_state(self) -> None:
        with mock.patch.object(helper.state, "is_enabled", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                helper.main(None)
        self.assertFalse(paths.helper_pid_file().exists())

    def test_startup_reconciles_a_stale_marker(self) -> None:
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {"pid": 999_999_999, "pid_start": 1.0, "started_at": 0.0},
        )
        self.assertEqual(helper.main(None), 0)
        # The stale marker was replaced by the live one and removed on exit.
        self.assertFalse(paths.helper_pid_file().exists())
        events = [record["event"] for record in cli.log.read_records(limit=50)]
        self.assertIn("helper_pid_reconciled", events)

    def test_second_helper_does_not_touch_the_first(self) -> None:
        """The advisory lock keeps a second helper from adopting live state."""
        with mock.patch.object(helper, "write_pid") as write:
            paths.atomic_write_json(paths.helper_pid_file(), {"pid": os.getpid()})
            with paths.file_lock(paths.lock_file(), blocking=False):
                self.assertEqual(helper.main(None), 0)
            write.assert_not_called()


class RealHelperProcessTests(StateCase):
    """A genuinely running helper, spawned exactly as the service does."""

    def spawn_helper(self) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        env["CODEX_AUTO_RESUME_STATE_DIR"] = str(paths.state_dir())
        env["CODEX_HOME"] = str(self.codex_home)
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-m", "codex_auto_resume", "helper", "--codex-bin", sys.executable],
            cwd=str(SRC.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.stop_helper, proc)
        return proc

    def stop_helper(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)

    def running_state(self) -> dict:
        return helper.helper_pid_state()

    def test_running_helper_is_detected_and_termination_cleans_up(self) -> None:
        proc = self.spawn_helper()
        self.assertTrue(
            wait_for(lambda: self.running_state()["state"] == helper.PID_STATE_RUNNING),
            f"helper did not publish a live pid marker: {paths.read_json(paths.helper_pid_file())}",
        )
        record = paths.read_json(paths.helper_pid_file()) or {}
        self.assertEqual(record["pid"], proc.pid)
        self.assertTrue(cli._helper_running())
        healthy, detail = cli._helper_health()
        self.assertTrue(healthy)
        self.assertIn("running", detail)
        self.assertIn(str(proc.pid), detail)

        # What `systemctl --user stop` / `launchctl kill SIGTERM` does: a
        # graceful exit that removes its own marker. Windows has no SIGTERM for
        # a plain child process (`terminate()` hard-kills it), so there the
        # leftover marker must reconcile exactly like a killed helper's.
        proc.terminate()
        exit_code = proc.wait(timeout=15)
        helper.reconcile_helper_pid()
        self.assertFalse(paths.helper_pid_file().exists())
        self.assertEqual(helper.helper_pid_state()["state"], helper.PID_STATE_IDLE)
        if os.name != "nt":
            self.assertEqual(exit_code, 0, "SIGTERM must be handled and exit cleanly")

    def test_killed_helper_leaves_a_marker_that_reconciles(self) -> None:
        proc = self.spawn_helper()
        self.assertTrue(wait_for(lambda: cli._helper_running()))
        proc.kill()
        proc.wait(timeout=15)
        # SIGKILL cannot run cleanup: the marker survives.
        self.assertTrue(paths.helper_pid_file().exists())
        verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_STALE)
        self.assertFalse(helper.is_helper_running())
        helper.reconcile_helper_pid()
        self.assertFalse(paths.helper_pid_file().exists())

    def test_restart_after_a_kill_recovers(self) -> None:
        first = self.spawn_helper()
        self.assertTrue(wait_for(lambda: cli._helper_running()))
        first.kill()
        first.wait(timeout=15)
        self.assertTrue(paths.helper_pid_file().exists())

        second = self.spawn_helper()
        self.assertTrue(wait_for(lambda: self.running_state()["pid"] == second.pid))
        record = paths.read_json(paths.helper_pid_file()) or {}
        self.assertEqual(record["pid"], second.pid)
        second.terminate()
        exit_code = second.wait(timeout=15)
        helper.reconcile_helper_pid()
        self.assertFalse(paths.helper_pid_file().exists())
        if os.name != "nt":
            self.assertEqual(exit_code, 0)

    def test_doctor_reconciles_a_stale_marker(self) -> None:
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {"pid": 999_999_999, "pid_start": 1.0, "started_at": 0.0},
        )
        healthy, detail = cli._helper_health()
        self.assertTrue(healthy)
        self.assertIn("stale", detail)
        self.assertFalse(paths.helper_pid_file().exists())

    def test_reconcile_never_deletes_a_newer_marker(self) -> None:
        """A replacement helper that published its marker must keep it."""
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {"pid": 999_999_999, "pid_start": 1.0, "started_at": 0.0},
        )
        stale = helper.helper_pid_state()
        self.assertEqual(stale["state"], helper.PID_STATE_STALE)
        # The replacement helper started and wrote its own marker in the
        # meantime, so reconciliation must re-read before unlinking.
        helper.write_pid()
        with mock.patch.object(paths, "process_cmdline", return_value=HELPER_CMDLINE):
            verdict = helper.reconcile_helper_pid()
            self.assertEqual(verdict["state"], helper.PID_STATE_RUNNING)
        self.assertTrue(paths.helper_pid_file().exists())


if __name__ == "__main__":
    unittest.main()
