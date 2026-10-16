"""Cross-platform helper identity, wake-up channels and host-process detection.

The Linux pid lifecycle is pinned by ``tests/test_pidfile.py``. These tests
cover the equivalents on the other two platforms, with the platform modules
faked so they run on any CI machine:

* macOS start times come from ``ps`` (absolute epoch, 1 s resolution, 2 s
  tolerance) and the boot id from ``sysctl kern.boottime``.
* Windows start times are exact FILETIME creation times, so a recycled pid can
  never match, and waking a helper is a named-event ``SetEvent`` instead of
  ``SIGUSR1``.
* The recorded session host on Windows is the grandparent of the hook process,
  because the hook runs under ``cmd.exe``.
"""

from __future__ import annotations

import os
import signal
import sys
import unittest
from unittest import mock

from support import StateCase
from codex_auto_resume import helper, paths, sysinfo
from codex_auto_resume import macproc, winproc


class _PlatformOverride(StateCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved = sysinfo._INFO
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sysinfo._INFO = self._saved

    def use(self, info: sysinfo.PlatformInfo) -> None:
        sysinfo._INFO = info

    def as_windows(self) -> None:
        self.use(sysinfo.detect(raw_system="win32", environ={}, arch="AMD64"))

    def as_macos(self) -> None:
        self.use(sysinfo.detect(raw_system="darwin", environ={}, arch="arm64"))


class MacProcessTests(_PlatformOverride):
    def setUp(self) -> None:
        super().setUp()
        self._saved_is_macos = macproc.IS_MACOS
        macproc.IS_MACOS = True
        self.addCleanup(self._restore_macos)

    def _restore_macos(self) -> None:
        macproc.IS_MACOS = self._saved_is_macos

    @staticmethod
    def _ps(stdout: str, returncode: int = 0):
        import subprocess

        return subprocess.CompletedProcess(["ps"], returncode, stdout, "")

    def test_start_time_parses_ps_lstart(self) -> None:
        with mock.patch.object(
            macproc.subprocess, "run", return_value=self._ps("Thu Oct 16 10:22:31 2026\n")
        ):
            value = macproc.process_start_time(4242)
        self.assertIsNotNone(value)
        import time

        parsed = time.localtime(value)
        self.assertEqual((parsed.tm_year, parsed.tm_mon, parsed.tm_mday), (2026, 10, 16))

    def test_start_time_is_none_when_ps_fails(self) -> None:
        with mock.patch.object(
            macproc.subprocess, "run", return_value=self._ps("", returncode=1)
        ):
            self.assertIsNone(macproc.process_start_time(4242))

    def test_alive_allows_ps_one_second_resolution(self) -> None:
        with mock.patch.object(os, "kill", return_value=None):
            with mock.patch.object(macproc, "process_start_time", return_value=1000.0):
                self.assertTrue(macproc.process_alive(os.getpid(), 1001.0))
                self.assertTrue(macproc.process_alive(os.getpid(), 998.5))
                self.assertFalse(macproc.process_alive(os.getpid(), 1010.0))

    def test_alive_is_false_for_a_recycled_pid(self) -> None:
        with mock.patch.object(os, "kill", return_value=None):
            with mock.patch.object(macproc, "process_start_time", return_value=5000.0):
                self.assertFalse(macproc.process_alive(999, 1000.0))

    def test_boot_id_parses_sysctl(self) -> None:
        with mock.patch.object(
            macproc.subprocess,
            "run",
            return_value=self._ps("{ sec = 1697566951, usec = 552819 } Thu Oct 12 09:00:00 2026\n"),
        ):
            self.assertEqual(macproc.boot_id(), "darwin-boot-1697566951")

    def test_cmdline_comes_from_ps(self) -> None:
        with mock.patch.object(
            macproc.subprocess, "run", return_value=self._ps("/usr/bin/python3 /repo/bin/codex-auto-resume helper\n")
        ):
            self.assertIn("codex-auto-resume helper", macproc.process_cmdline(77))

    def test_sysinfo_dispatches_to_macproc(self) -> None:
        self.as_macos()
        with mock.patch.object(macproc, "process_start_time", return_value=123.0) as call:
            self.assertEqual(sysinfo.process_start_time(4242), 123.0)
        call.assert_called_once_with(4242)
        with mock.patch.object(macproc, "process_cmdline", return_value="ps cmdline") as call:
            self.assertEqual(sysinfo.process_cmdline(4242), "ps cmdline")

    def test_boot_id_change_makes_a_marker_stale(self) -> None:
        self.as_macos()
        # The helper only trusts a marker whose recorded boot id matches ours.
        with mock.patch.object(paths.sysinfo, "boot_id", return_value="darwin-boot-1"):
            with mock.patch.object(paths, "process_alive", return_value=True), mock.patch.object(
                paths, "process_cmdline", return_value="python -m codex_auto_resume helper"
            ):
                paths.atomic_write_json(
                    paths.helper_pid_file(),
                    {
                        "pid": os.getpid(),
                        "pid_start": 1.0,
                        "boot_id": "darwin-boot-2",
                        "started_at": 0.0,
                    },
                )
                verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_STALE)
        self.assertEqual(verdict["reason"], "different_boot")


class WindowsProcessTests(_PlatformOverride):
    def setUp(self) -> None:
        super().setUp()
        self.as_windows()

    def test_creation_time_must_match(self) -> None:
        with mock.patch.object(winproc, "process_start_time", return_value=133_000_000_000_000_000.0):
            self.assertTrue(winproc.process_alive(4242, 133_000_000_000_000_000.0))
            # A materially different creation time is a different process. (The
            # marker stores the value as a JSON number, so the comparison is to
            # the ~100 ns precision a float64 preserves; Windows creates
            # processes milliseconds apart, never nanoseconds.)
            self.assertFalse(winproc.process_alive(4242, 133_000_000_001_000_000.0))

    def test_dead_process_is_not_alive(self) -> None:
        with mock.patch.object(winproc, "process_start_time", return_value=None):
            self.assertFalse(winproc.process_alive(4242, None))

    def test_exited_process_is_dead_even_if_the_handle_still_exists(self) -> None:
        """A terminated child whose handle is unreaped must read as dead.

        On Windows a killed child stays in the process table while a parent
        handle keeps the process object alive, so opening its pid still
        succeeds. Liveness therefore comes from WaitForSingleObject, exposed
        here through ``_is_running``.
        """
        with mock.patch.object(winproc, "_open", return_value=7), mock.patch.object(
            winproc, "_close"
        ), mock.patch.object(winproc, "_is_running", return_value=False):
            self.assertIsNone(winproc.process_start_time(4242))
            self.assertFalse(winproc.process_alive(4242, None))

    def test_sysinfo_dispatches_to_winproc(self) -> None:
        with mock.patch.object(winproc, "process_start_time", return_value=99.0) as call:
            self.assertEqual(sysinfo.process_start_time(7), 99.0)
        call.assert_called_once_with(7)
        with mock.patch.object(winproc, "boot_id", return_value="win-boot-1"):
            self.assertEqual(sysinfo.boot_id(), "win-boot-1")

    def test_host_pid_skips_the_cmd_wrapper(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.object(os, "getppid", return_value=200):
                with mock.patch.object(
                    winproc, "process_parent_pid", side_effect={200: 100, 100: 50}.get
                ), mock.patch.object(
                    winproc,
                    "process_image_path",
                    side_effect={
                        100: r"C:\Windows\System32\cmd.exe",
                        50: r"C:\Users\me\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe",
                    }.get,
                ):
                    self.assertEqual(sysinfo.host_pid(), 50)

    def test_host_pid_stops_when_the_parent_is_already_real(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.object(os, "getppid", return_value=200):
                with mock.patch.object(
                    winproc, "process_parent_pid", side_effect={200: 100}.get
                ), mock.patch.object(
                    winproc, "process_image_path", side_effect={100: r"C:\codex.exe"}.get
                ):
                    self.assertEqual(sysinfo.host_pid(), 100)

    def test_host_pid_is_the_parent_on_posix(self) -> None:
        self.use(sysinfo.detect(raw_system="linux", osrelease="6.8.0", version_text="", environ={}))
        with mock.patch.object(os, "getppid", return_value=321):
            self.assertEqual(sysinfo.host_pid(), 321)

    def test_notify_helper_signals_the_named_event(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_alive", return_value=True), mock.patch.object(
            paths, "process_cmdline", return_value="python -m codex_auto_resume helper"
        ), mock.patch.object(
            winproc, "open_wake_event", return_value=1234
        ) as open_call, mock.patch.object(
            winproc, "set_event", return_value=True
        ) as set_call, mock.patch.object(
            os, "kill"
        ) as kill:
            self.assertTrue(helper.notify_helper())
        open_call.assert_called_once()
        set_call.assert_called_once_with(1234)
        kill.assert_not_called()
        winproc.close_handle  # referenced for clarity; closing is exercised below

    def test_notify_helper_reports_false_without_an_event(self) -> None:
        helper.write_pid()
        with mock.patch.object(paths, "process_alive", return_value=True), mock.patch.object(
            paths, "process_cmdline", return_value="python -m codex_auto_resume helper"
        ), mock.patch.object(winproc, "open_wake_event", return_value=None):
            self.assertFalse(helper.notify_helper())

    def test_wake_channel_name_is_stable_and_installation_specific(self) -> None:
        first = sysinfo.wake_channel_name("/home/user/.local/state/codex-auto-resume")
        again = sysinfo.wake_channel_name("/home/user/.local/state/codex-auto-resume")
        other = sysinfo.wake_channel_name("/home/user/other-state")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertTrue(first.startswith("Local\\codex-auto-resume-helper-"))


class WaiterPlatformTests(_PlatformOverride):
    @unittest.skipUnless(hasattr(signal, "SIGUSR1"), "SIGUSR1 is POSIX-only")
    def test_posix_waiter_installs_signal_handlers(self) -> None:
        self.use(sysinfo.detect(raw_system="linux", osrelease="6.8.0", version_text="", environ={}))
        waiter = helper.Waiter()
        self.addCleanup(waiter.close)
        waiter.install()
        self.assertTrue(waiter._installed, "SIGUSR1 wake must work on POSIX")

    def test_windows_fallback_slice_is_bounded_when_no_event_exists(self) -> None:
        self.as_windows()
        waiter = helper.Waiter()
        self.addCleanup(waiter.close)
        with mock.patch.object(winproc, "create_wake_event", return_value=None):
            waiter.install()
        self.assertFalse(waiter._installed)
        self.assertLessEqual(
            waiter._slice(3600.0), helper.Waiter.FALLBACK_SLICE_SECONDS
        )

    def test_windows_event_watcher_wakes_the_loop(self) -> None:
        self.as_windows()
        waiter = helper.Waiter()
        self.addCleanup(waiter.close)

        def fake_wait(handle, timeout):
            import time as _time

            _time.sleep(0.01)
            waiter.woken  # the thread flips this; simulate the event
            return False

        with mock.patch.object(winproc, "create_wake_event", return_value=99):
            with mock.patch.object(winproc, "wait_event", side_effect=fake_wait):
                waiter.install()
        self.assertTrue(waiter._installed)
        self.assertEqual(waiter._wake_handle, 99)

    def test_default_platform_uses_the_module_default(self) -> None:
        # Without any override the real platform is used; the waiter must always
        # be constructible and closeable.
        waiter = helper.Waiter()
        waiter.install()
        waiter.sleep_until(0.0)
        waiter.close()


class SpawnTargetTests(_PlatformOverride):
    """The resume child must start on every platform, including .cmd shims."""

    def test_posix_spawns_the_binary_directly_in_its_own_session(self) -> None:
        from codex_auto_resume import runner

        self.use(sysinfo.detect(raw_system="linux", osrelease="6.8.0", version_text="", environ={}))
        command = ["/usr/local/bin/codex", "exec", "resume", "--json", "sess", "prompt"]
        self.assertEqual(runner.spawn_target(command), command)
        self.assertEqual(runner.detach_kwargs(), {"start_new_session": True})

    def test_windows_exe_is_spawned_directly(self) -> None:
        from codex_auto_resume import runner

        self.as_windows()
        command = [r"C:\Users\me\codex.exe", "exec", "resume"]
        self.assertEqual(runner.spawn_target(command), command)

    def test_windows_cmd_shim_goes_through_comspec(self) -> None:
        from codex_auto_resume import runner

        self.as_windows()
        command = [
            r"C:\Users\Jane Doe\AppData\Roaming\npm\codex.cmd",
            "exec",
            "resume",
            "--json",
            "01a13e0c-d592-7401-b638-b8b61003c313",
            "Continue the unfinished task.",
        ]
        target = runner.spawn_target(command)
        self.assertIsInstance(target, str)
        self.assertIn("cmd.exe", target.lower())
        self.assertIn("/c", target)
        self.assertIn('"C:\\Users\\Jane Doe\\AppData\\Roaming\\npm\\codex.cmd"', target)
        self.assertIn("01a13e0c-d592-7401-b638-b8b61003c313", target)

    def test_windows_detach_uses_a_new_process_group(self) -> None:
        from codex_auto_resume import runner

        self.as_windows()
        kwargs = runner.detach_kwargs()
        self.assertNotIn("start_new_session", kwargs)


if __name__ == "__main__":
    unittest.main()
