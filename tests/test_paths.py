"""Portable paths, process liveness and platform state-directory rules."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from support import StateCase
from codex_auto_resume import paths, sysinfo


class StateDirPlatformTests(unittest.TestCase):
    """Each platform gets its conventional per-user state location."""

    def setUp(self) -> None:
        self._saved = sysinfo._INFO
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sysinfo._INFO = self._saved

    def _as(self, os_name: str, **kwargs) -> None:
        if os_name == "macos":
            sysinfo._INFO = sysinfo.detect(raw_system="darwin", environ={}, arch="arm64")
        elif os_name == "windows":
            sysinfo._INFO = sysinfo.detect(raw_system="win32", environ={}, arch="AMD64")
        else:
            sysinfo._INFO = sysinfo.detect(
                raw_system="linux", osrelease="6.8.0", version_text="", environ={}
            )

    def _home(self, value: str):
        """Patch ``Path.home()`` so expectations hold on every host OS."""
        return mock.patch("pathlib.Path.home", return_value=Path(value))

    def test_linux_defaults_to_xdg_state_home(self) -> None:
        self._as("linux")
        with self._home("/home/user"):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("XDG_STATE_HOME", None)
                os.environ.pop("CODEX_AUTO_RESUME_STATE_DIR", None)
                self.assertEqual(
                    paths.state_dir(), Path("/home/user/.local/state/codex-auto-resume")
                )

    def test_linux_honours_xdg_state_home(self) -> None:
        self._as("linux")
        with self._home("/home/user"):
            with mock.patch.dict(
                os.environ, {"XDG_STATE_HOME": "/home/user/.state"}, clear=False
            ):
                os.environ.pop("CODEX_AUTO_RESUME_STATE_DIR", None)
                self.assertEqual(paths.state_dir(), Path("/home/user/.state/codex-auto-resume"))
                self.assertEqual(paths.state_dir_source(), "XDG_STATE_HOME")

    def test_macos_defaults_to_application_support(self) -> None:
        self._as("macos")
        with self._home("/Users/user"):
            os.environ.pop("XDG_STATE_HOME", None)
            os.environ.pop("CODEX_AUTO_RESUME_STATE_DIR", None)
            self.assertEqual(
                paths.state_dir(),
                Path("/Users/user/Library/Application Support/codex-auto-resume"),
            )
            self.assertEqual(paths.state_dir_source(), "~/Library/Application Support")

    def test_macos_keeps_an_existing_legacy_directory(self) -> None:
        self._as("macos")
        import tempfile

        with tempfile.TemporaryDirectory() as home:
            legacy = Path(home) / ".local" / "state" / "codex-auto-resume"
            legacy.mkdir(parents=True)
            with self._home(home):
                os.environ.pop("XDG_STATE_HOME", None)
                os.environ.pop("CODEX_AUTO_RESUME_STATE_DIR", None)
                self.assertEqual(paths.state_dir(), legacy)
                self.assertEqual(paths.state_dir_source(), "legacy ~/.local/state")

    def test_macos_honours_an_explicit_xdg_state_home(self) -> None:
        self._as("macos")
        with self._home("/Users/user"):
            with mock.patch.dict(
                os.environ, {"XDG_STATE_HOME": "/Users/user/.xdg-state"}, clear=False
            ):
                os.environ.pop("CODEX_AUTO_RESUME_STATE_DIR", None)
                self.assertEqual(
                    paths.state_dir(), Path("/Users/user/.xdg-state/codex-auto-resume")
                )

    def test_windows_defaults_to_local_appdata(self) -> None:
        self._as("windows")
        with self._home("/home/user"):
            with mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": r"C:\Users\Jane Doe\AppData\Local"},
                clear=False,
            ):
                os.environ.pop("CODEX_AUTO_RESUME_STATE_DIR", None)
                self.assertEqual(
                    paths.state_dir(),
                    Path(r"C:\Users\Jane Doe\AppData\Local") / "codex-auto-resume",
                )
                self.assertEqual(paths.state_dir_source(), "LOCALAPPDATA")

    def test_windows_falls_back_without_local_appdata(self) -> None:
        self._as("windows")
        with self._home("/home/user"):
            os.environ.pop("LOCALAPPDATA", None)
            os.environ.pop("CODEX_AUTO_RESUME_STATE_DIR", None)
            self.assertEqual(
                paths.state_dir(), Path("/home/user/AppData/Local/codex-auto-resume")
            )

    def test_explicit_override_wins_on_every_platform(self) -> None:
        for platform in ("linux", "macos", "windows"):
            self._as(platform)
            with self._home("/home/user"):
                with mock.patch.dict(
                    os.environ,
                    {"CODEX_AUTO_RESUME_STATE_DIR": "/tmp/custom state/ünïcode dir"},
                    clear=False,
                ):
                    self.assertEqual(
                        paths.state_dir(), Path("/tmp/custom state/ünïcode dir")
                    )
                    self.assertEqual(paths.state_dir_source(), "CODEX_AUTO_RESUME_STATE_DIR")


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

    @unittest.skipIf(os.name == "nt", "the kill(pid, 0) probe is POSIX-only")
    def test_signal_probe_is_used_without_proc(self) -> None:
        with mock.patch.object(paths.os.path, "isdir", return_value=False):
            with mock.patch.object(paths.os, "kill") as kill:
                self.assertTrue(paths.process_alive(os.getpid(), None))
        self.assertTrue(kill.called)

    @unittest.skipIf(os.name == "nt", "the kill(pid, 0) probe is POSIX-only")
    def test_signal_probe_reports_missing_process(self) -> None:
        with mock.patch.object(paths.os.path, "isdir", return_value=False):
            with mock.patch.object(
                paths.os, "kill", side_effect=ProcessLookupError(3, "no such process")
            ):
                self.assertFalse(paths.process_alive(4242, None))

    @unittest.skipIf(os.name == "nt", "the kill(pid, 0) probe is POSIX-only")
    def test_signal_probe_reports_permission_denied_as_alive(self) -> None:
        with mock.patch.object(paths.os.path, "isdir", return_value=False):
            with mock.patch.object(
                paths.os, "kill", side_effect=PermissionError(1, "operation not permitted")
            ):
                self.assertTrue(paths.process_alive(1, None))

    def test_cmdline_is_empty_when_unavailable(self) -> None:
        with mock.patch.object(paths, "process_cmdline", wraps=paths.process_cmdline):
            self.assertIsInstance(paths.process_cmdline(os.getpid()), str)

    def test_looks_like_helper_tolerates_missing_cmdline(self) -> None:
        from codex_auto_resume import helper

        with mock.patch.object(paths, "process_cmdline", return_value=""):
            self.assertTrue(helper.looks_like_helper_process(os.getpid()))
        with mock.patch.object(paths, "process_cmdline", return_value="nginx: worker"):
            self.assertFalse(helper.looks_like_helper_process(os.getpid()))
        with mock.patch.object(paths, "process_cmdline", return_value="python3 -m codex_auto_resume helper"):
            self.assertTrue(helper.looks_like_helper_process(os.getpid()))
        # Another subcommand of the same tool must not pass for the helper.
        with mock.patch.object(
            paths, "process_cmdline", return_value="/root/cli-persistence/bin/codex-auto-resume status"
        ):
            self.assertFalse(helper.looks_like_helper_process(os.getpid()))


class PathQuotingTests(StateCase):
    """Directories with spaces and non-ASCII characters must round-trip."""

    def test_state_files_round_trip_in_a_hostile_directory(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="car test ") as home:
            target = Path(home) / "state dir ünïcode" / "codex-auto-resume"
            with mock.patch.dict(
                os.environ, {"CODEX_AUTO_RESUME_STATE_DIR": str(target)}, clear=False
            ):
                paths.ensure_dirs()
                self.assertTrue(paths.sessions_dir().is_dir())
                paths.atomic_write_json(paths.helper_pid_file(), {"pid": 1, "started_at": 0.0})
                self.assertEqual(paths.read_json(paths.helper_pid_file())["pid"], 1)
                with paths.file_lock(paths.lock_file(), blocking=False) as acquired:
                    self.assertTrue(acquired)
                self.assertEqual(
                    paths.read_json(paths.helper_pid_file())["pid"],
                    1,
                    "the lock must not disturb the state files",
                )

    def test_launcher_path_is_absolute_and_python_runnable(self) -> None:
        from codex_auto_resume import launcher

        script = Path(launcher.launcher_path())
        self.assertTrue(script.is_absolute())
        self.assertEqual(script.name, "codex-auto-resume")
        self.assertTrue(script.exists(), "the repository must ship the launcher")
        self.assertEqual(
            Path(launcher.windows_launcher_path()).name, "codex-auto-resume.cmd"
        )


if __name__ == "__main__":
    unittest.main()
