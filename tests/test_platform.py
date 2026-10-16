"""Platform detection and service-backend selection.

The detection must be explicit and tested: Linux, macOS, Windows native,
WSL1/WSL2 and unknown platforms, and WSL must never select a Windows backend.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from support import StateCase
from codex_auto_resume import service, sysinfo


class DetectionTests(unittest.TestCase):
    def test_native_linux(self) -> None:
        info = sysinfo.detect(
            raw_system="linux",
            osrelease="6.8.0-45-generic",
            version_text="Linux version 6.8.0-45-generic (buildd@lcy02)",
            environ={},
            arch="x86_64",
        )
        self.assertEqual(info.os, sysinfo.OS_LINUX)
        self.assertFalse(info.is_wsl)
        self.assertEqual(info.service_backend, sysinfo.BACKEND_SYSTEMD)
        self.assertEqual(info.label(), "Linux x86_64")

    def test_wsl2_from_osrelease(self) -> None:
        info = sysinfo.detect(
            raw_system="linux",
            osrelease="5.15.90.1-microsoft-standard-WSL2",
            version_text="Linux version 5.15.90.1-microsoft-standard-WSL2",
            environ={},
            arch="x86_64",
        )
        self.assertEqual(info.os, sysinfo.OS_LINUX)
        self.assertTrue(info.is_wsl)
        self.assertEqual(info.wsl_version, "2")
        # WSL must use the Linux backend, never the Windows one.
        self.assertEqual(info.service_backend, sysinfo.BACKEND_SYSTEMD)
        self.assertIn("WSL2", info.label())

    def test_wsl1_from_osrelease(self) -> None:
        info = sysinfo.detect(
            raw_system="linux",
            osrelease="4.4.0-19041-Microsoft",
            version_text="Linux version 4.4.0-19041-Microsoft",
            environ={},
            arch="x86_64",
        )
        self.assertTrue(info.is_wsl)
        self.assertEqual(info.wsl_version, "1")
        self.assertEqual(info.service_backend, sysinfo.BACKEND_SYSTEMD)

    def test_wsl_from_environment_only(self) -> None:
        info = sysinfo.detect(
            raw_system="linux",
            osrelease="5.15.0-91-generic",
            version_text="Linux version 5.15.0-91-generic",
            environ={"WSL_DISTRO_NAME": "Ubuntu"},
            arch="x86_64",
        )
        self.assertTrue(info.is_wsl)
        self.assertIn("WSL", info.label())

    def test_macos(self) -> None:
        info = sysinfo.detect(raw_system="darwin", environ={}, arch="arm64")
        self.assertEqual(info.os, sysinfo.OS_MACOS)
        self.assertEqual(info.service_backend, sysinfo.BACKEND_LAUNCHD)
        self.assertEqual(info.label(), "macOS arm64")

    def test_windows(self) -> None:
        info = sysinfo.detect(raw_system="win32", environ={}, arch="AMD64")
        self.assertEqual(info.os, sysinfo.OS_WINDOWS)
        self.assertEqual(info.service_backend, sysinfo.BACKEND_TASK_SCHEDULER)
        self.assertEqual(info.label(), "Windows AMD64")

    def test_unknown_platform(self) -> None:
        info = sysinfo.detect(raw_system="freebsd13", environ={}, arch="amd64")
        self.assertEqual(info.os, sysinfo.OS_UNKNOWN)
        self.assertEqual(info.service_backend, sysinfo.BACKEND_UNSUPPORTED)
        self.assertIn("freebsd13", info.label())

    def test_as_dict_is_json_safe(self) -> None:
        info = sysinfo.detect(raw_system="darwin", environ={}, arch="arm64")
        payload = info.as_dict()
        self.assertEqual(payload["os"], "macos")
        self.assertEqual(payload["service_backend"], "launchd")


class BackendSelectionTests(StateCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved_info = sysinfo._INFO
        self._saved_backend = service._BACKEND
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sysinfo._INFO = self._saved_info
        service._BACKEND = self._saved_backend

    def _select(self, platform: sysinfo.PlatformInfo):
        sysinfo._INFO = platform
        return service.reload()

    def test_linux_selects_systemd(self) -> None:
        adapter = self._select(sysinfo.detect(raw_system="linux", osrelease="6.8.0", version_text="", environ={}))
        self.assertEqual(adapter.name, "systemd")

    def test_wsl_selects_systemd(self) -> None:
        adapter = self._select(
            sysinfo.detect(
                raw_system="linux",
                osrelease="5.15.90.1-microsoft-standard-WSL2",
                version_text="",
                environ={},
            )
        )
        self.assertEqual(adapter.name, "systemd")

    def test_macos_selects_launchd(self) -> None:
        adapter = self._select(sysinfo.detect(raw_system="darwin", environ={}))
        self.assertEqual(adapter.name, "launchd")

    def test_windows_selects_task_scheduler(self) -> None:
        adapter = self._select(sysinfo.detect(raw_system="win32", environ={}))
        self.assertEqual(adapter.name, "task-scheduler")

    def test_unknown_selects_unsupported(self) -> None:
        adapter = self._select(sysinfo.detect(raw_system="freebsd13", environ={}))
        self.assertEqual(adapter.name, "unsupported")
        available, detail = adapter.available()
        self.assertFalse(available)
        self.assertIn("freebsd13", detail)

    def test_remote_looking_platform_is_not_detected_as_windows_locally(self) -> None:
        """Native detection is local: running on Linux stays linux even with Windows env."""
        with mock.patch.dict("os.environ", {"OS": "Windows_NT"}, clear=False):
            info = sysinfo.detect(
                raw_system="linux", osrelease="6.8.0", version_text="", environ={"OS": "Windows_NT"}
            )
        self.assertEqual(info.os, sysinfo.OS_LINUX)


if __name__ == "__main__":
    unittest.main()
