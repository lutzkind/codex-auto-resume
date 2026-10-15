"""Portable process liveness (Linux /proc, elsewhere a signal probe)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from support import StateCase
from codex_auto_resume import paths


class ProcessAliveTests(StateCase):
    def test_this_process_is_alive(self) -> None:
        pid = os.getpid()
        self.assertTrue(paths.process_alive(pid, paths.process_start_time(pid)))

    def test_missing_pid_is_not_alive(self) -> None:
        self.assertFalse(paths.process_alive(999_999_999, None))

    def test_non_positive_pid_is_not_alive(self) -> None:
        self.assertFalse(paths.process_alive(0, None))
        self.assertFalse(paths.process_alive(-1, None))

    def test_wrong_start_time_is_not_alive(self) -> None:
        pid = os.getpid()
        start = paths.process_start_time(pid)
        if start is None:
            self.skipTest("no /proc start time on this platform")
        self.assertFalse(paths.process_alive(pid, start + 10_000))

    def test_signal_probe_is_used_without_proc(self) -> None:
        with mock.patch.object(paths.os.path, "isdir", return_value=False):
            with mock.patch.object(paths.os, "kill") as kill:
                self.assertTrue(paths.process_alive(os.getpid(), None))
        self.assertTrue(kill.called)

    def test_signal_probe_reports_missing_process(self) -> None:
        with mock.patch.object(paths.os.path, "isdir", return_value=False):
            with mock.patch.object(
                paths.os, "kill", side_effect=ProcessLookupError(3, "no such process")
            ):
                self.assertFalse(paths.process_alive(4242, None))

    def test_signal_probe_reports_permission_denied_as_alive(self) -> None:
        with mock.patch.object(paths.os.path, "isdir", return_value=False):
            with mock.patch.object(
                paths.os, "kill", side_effect=PermissionError(1, "operation not permitted")
            ):
                self.assertTrue(paths.process_alive(1, None))

    def test_cmdline_is_empty_when_unavailable(self) -> None:
        with mock.patch.object(paths, "process_cmdline", wraps=paths.process_cmdline):
            self.assertIsInstance(paths.process_cmdline(os.getpid()), str)

    def test_looks_like_our_process_tolerates_missing_cmdline(self) -> None:
        from codex_auto_resume import helper

        with mock.patch.object(paths, "process_cmdline", return_value=""):
            self.assertTrue(helper._looks_like_our_process(os.getpid()))
        with mock.patch.object(paths, "process_cmdline", return_value="nginx: worker"):
            self.assertFalse(helper._looks_like_our_process(os.getpid()))
        with mock.patch.object(paths, "process_cmdline", return_value="python3 -m codex_auto_resume helper"):
            self.assertTrue(helper._looks_like_our_process(os.getpid()))


if __name__ == "__main__":
    unittest.main()
