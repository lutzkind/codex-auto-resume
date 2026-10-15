"""Helper process bookkeeping: the pid file hooks use to wake the helper."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from support import StateCase
from codex_auto_resume import cli, helper, paths, state

HELPER_CMDLINE = "python3 -m codex_auto_resume helper"


class PidFileTests(StateCase):
    def test_pid_file_is_json_and_reports_this_process(self) -> None:
        helper.write_pid()
        record = paths.read_json(paths.helper_pid_file())
        self.assertIsNotNone(record)
        self.assertEqual(record["pid"], os.getpid())
        # /proc is Linux-only; elsewhere the start time is unknown on purpose.
        self.assertTrue(
            record["pid_start"] is None or isinstance(record["pid_start"], float)
        )
        self.assertEqual(record["version"], state.SCHEMA_VERSION)

    def test_status_reports_a_running_helper(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_cmdline", return_value=HELPER_CMDLINE):
            self.assertTrue(cli._helper_running())

    def test_stale_pid_file_is_not_running(self) -> None:
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {"pid": 999_999_999, "pid_start": 1.0, "started_at": 0.0},
        )
        with mock.patch.object(paths, "process_cmdline", return_value=HELPER_CMDLINE):
            self.assertFalse(cli._helper_running())

    def test_pid_of_another_program_is_not_running(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_cmdline", return_value="nginx: worker process"):
            self.assertFalse(cli._helper_running())

    def test_missing_pid_file(self) -> None:
        paths.remove_file(paths.helper_pid_file())
        self.assertFalse(cli._helper_running())

    def test_notify_helper_requires_a_matching_process(self) -> None:
        self.assertFalse(helper.notify_helper())
        helper.write_pid()
        with mock.patch.object(paths, "process_cmdline", return_value="nginx: worker process"):
            with mock.patch.object(os, "kill") as kill:
                self.assertFalse(helper.notify_helper(), "must not signal an unrelated process")
                kill.assert_not_called()

    def test_notify_helper_signals_the_helper(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_cmdline", return_value=HELPER_CMDLINE):
            with mock.patch.object(os, "kill") as kill:
                self.assertTrue(helper.notify_helper())
                kill.assert_called_once()

    def test_notify_helper_ignores_a_stale_pid(self) -> None:
        paths.atomic_write_json(
            paths.helper_pid_file(), {"pid": 999_999_999, "pid_start": 1.0, "started_at": 0.0}
        )
        with mock.patch.object(paths, "process_cmdline", return_value=HELPER_CMDLINE):
            with mock.patch.object(os, "kill") as kill:
                self.assertFalse(helper.notify_helper())
                kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
