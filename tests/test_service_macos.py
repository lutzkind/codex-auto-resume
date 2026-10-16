"""macOS launchd backend: plist generation and launchctl command sequences.

The real ``launchctl`` lifecycle is exercised in CI on macOS
(``tests/test_platform_integration.py``); these tests pin the generated
artefact and the exact modern verbs used, with a fake command runner so they
run everywhere.
"""

from __future__ import annotations

import subprocess
import unittest
import xml.etree.ElementTree as ElementTree
from unittest import mock

from support import PACKAGE_ROOT, StateCase
from codex_auto_resume import paths, service
from codex_auto_resume.service import macos


def completed(argv, returncode=0, stdout=""):
    return subprocess.CompletedProcess(list(argv), returncode, stdout)


class PlistTests(StateCase):
    def test_program_arguments_are_an_array_without_shell_quoting(self) -> None:
        plist = macos.render_plist(
            "/Users/Jane Doe/bin/codex",
            python="/opt/homebrew/bin/python3",
            launcher="/Users/Jane Doe/repo/bin/codex-auto-resume",
            stdout="/Users/Jane Doe/Library/Logs/aar helper.log",
        )
        root = ElementTree.fromstring(plist)
        self.assertEqual(root.tag, "plist")
        values = [element.text for element in root.iter("string")]
        self.assertIn("/Users/Jane Doe/bin/codex", values)
        self.assertIn("/Users/Jane Doe/repo/bin/codex-auto-resume", values)
        self.assertIn("/Users/Jane Doe/Library/Logs/aar helper.log", values)
        self.assertIn("helper", values)
        # Values are raw array entries: no shell-level quotes around any path.
        self.assertIn("<string>/Users/Jane Doe/bin/codex</string>", plist)
        self.assertIn("<string>/Users/Jane Doe/repo/bin/codex-auto-resume</string>", plist)

    def test_restart_and_login_semantics_match_systemd(self) -> None:
        plist = macos.render_plist("/usr/local/bin/codex")
        self.assertIn("<key>RunAtLoad</key>\n    <true/>", plist)
        self.assertIn("<key>KeepAlive</key>", plist)
        self.assertIn("<key>SuccessfulExit</key>\n        <false/>", plist)
        self.assertIn("<key>AbandonProcessGroup</key>\n    <true/>", plist)
        self.assertIn(f"<integer>{macos.THROTTLE_SECONDS}</integer>", plist)

    def test_label_and_log_paths(self) -> None:
        plist = macos.render_plist(None)
        self.assertIn(f"<string>{macos.LABEL}</string>", plist)
        self.assertIn(str(paths.log_file()), plist)

    def test_non_ascii_binary_path_is_xml_escaped(self) -> None:
        plist = macos.render_plist("/Users/user/josé/Codex & Tools/codex")
        self.assertIn("josé", plist)
        self.assertIn("Codex &amp; Tools", plist)
        ElementTree.fromstring(plist)

    def test_no_deprecated_load_verbs(self) -> None:
        source = (PACKAGE_ROOT / "service" / "macos.py").read_text(encoding="utf-8")
        self.assertNotIn('"load"', source)
        self.assertNotIn('"unload"', source)
        self.assertIn('"bootstrap"', source)
        self.assertIn('"bootout"', source)
        self.assertIn('"kickstart"', source)

    def test_plist_path_is_user_space(self) -> None:
        self.assertTrue(str(macos.plist_path()).endswith("Library/LaunchAgents/" + macos.PLIST_NAME))
        self.assertTrue(macos.domain().startswith("gui/"))


class LaunchctlSequenceTests(StateCase):
    def _record(self):
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return completed(argv)

        return calls, fake_run

    def test_install_bootstraps_into_the_user_domain(self) -> None:
        calls, fake_run = self._record()
        with mock.patch.object(macos, "run_command", side_effect=fake_run):
            target = macos.MacOSBackend().install("/usr/local/bin/codex")
        self.assertEqual(target, str(macos.plist_path()))
        plist = macos.plist_path().read_text(encoding="utf-8")
        self.assertIn("codex", plist)
        verbs = [call[1] for call in calls]
        self.assertIn("bootout", verbs)
        self.assertIn("bootstrap", verbs)
        bootstrap = next(call for call in calls if call[1] == "bootstrap")
        self.assertEqual(bootstrap[0], "launchctl")
        self.assertEqual(bootstrap[2], macos.domain())
        self.assertEqual(bootstrap[-1], str(macos.plist_path()))
        self.addCleanup(macos.plist_path().unlink, missing_ok=True)

    def test_install_failure_is_reported_and_keeps_the_plist(self) -> None:
        def fake_run(argv, **kwargs):
            if "bootstrap" in argv:
                return completed(argv, returncode=5, stdout="Bootstrap failed: 5: Input/output error")
            return completed(argv)

        with mock.patch.object(macos, "run_command", side_effect=fake_run):
            with self.assertRaises(service.ServiceError) as caught:
                macos.MacOSBackend().install(None)
        self.assertIn("bootstrap failed", str(caught.exception))
        self.assertTrue(macos.plist_path().exists(), "the plist must be kept for inspection")
        self.addCleanup(macos.plist_path().unlink, missing_ok=True)

    def test_start_uses_kickstart_without_killing_a_running_helper(self) -> None:
        calls, fake_run = self._record()
        with mock.patch.object(macos, "run_command", side_effect=fake_run):
            macos.MacOSBackend().start()
        self.assertEqual(calls[0][1:3], ["kickstart", macos.service_target()])
        self.assertNotIn("-k", calls[0])

    def test_stop_signals_the_launchd_job(self) -> None:
        calls, fake_run = self._record()
        with mock.patch.object(macos, "run_command", side_effect=fake_run):
            macos.MacOSBackend().stop()
        self.assertEqual(calls[0][1:], ["kill", "SIGTERM", macos.service_target()])

    def test_uninstall_boots_out_and_removes_the_plist(self) -> None:
        macos.launch_agents_dir().mkdir(parents=True, exist_ok=True)
        macos.plist_path().write_text("<plist/>", encoding="utf-8")
        calls, fake_run = self._record()
        with mock.patch.object(macos, "run_command", side_effect=fake_run):
            macos.MacOSBackend().uninstall()
        self.assertFalse(macos.plist_path().exists())
        self.assertEqual(calls[0][1], "bootout")

    def test_status_reports_loaded_and_enabled(self) -> None:
        macos.launch_agents_dir().mkdir(parents=True, exist_ok=True)
        macos.plist_path().write_text("<plist/>", encoding="utf-8")
        self.addCleanup(macos.plist_path().unlink, missing_ok=True)

        def fake_run(argv, **kwargs):
            if argv[1] == "print":
                return completed(argv)
            if argv[1] == "print-disabled":
                return completed(argv, stdout="disabled services = {\n}")
            return completed(argv)

        with mock.patch.object(macos, "run_command", side_effect=fake_run):
            status = macos.MacOSBackend().status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["active"], "loaded")
        self.assertEqual(status["enabled"], "enabled")
        self.assertIn("kickstart", status["start_hint"])

    def test_status_reports_a_disabled_label(self) -> None:
        macos.launch_agents_dir().mkdir(parents=True, exist_ok=True)
        macos.plist_path().write_text("<plist/>", encoding="utf-8")
        self.addCleanup(macos.plist_path().unlink, missing_ok=True)

        def fake_run(argv, **kwargs):
            if argv[1] == "print-disabled":
                return completed(argv, stdout=f'disabled services = {{\n\t"{macos.LABEL}" => true\n}}')
            return completed(argv)

        with mock.patch.object(macos, "run_command", side_effect=fake_run):
            status = macos.MacOSBackend().status()
        self.assertEqual(status["enabled"], "disabled")


if __name__ == "__main__":
    unittest.main()
