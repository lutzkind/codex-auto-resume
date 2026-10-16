"""Windows Task Scheduler backend: task XML and schtasks command sequences.

The real ``schtasks`` lifecycle runs in CI on Windows
(``tests/test_platform_integration.py``); these tests pin the generated XML,
the argument quoting for user paths with spaces, and the exact schtasks verbs,
with a fake command runner so they run everywhere.
"""

from __future__ import annotations

import subprocess
import unittest
import xml.etree.ElementTree as ElementTree
from unittest import mock

from support import StateCase
from codex_auto_resume.service import windows


def completed(argv, returncode=0, stdout=b""):
    return subprocess.CompletedProcess(list(argv), returncode, stdout)


class TaskXmlTests(StateCase):
    def test_task_xml_is_wellformed_and_per_user(self) -> None:
        xml_text = windows.render_task_xml(
            r"C:\Users\Jane Doe\bin\codex.exe",
            python=r"C:\Program Files\Python312\python.exe",
            launcher=r"C:\Users\Jane Doe\.codex\plugins\cache\codex-auto-resume\0.2.0\bin\codex-auto-resume",
            user_id=r"DESKTOP\jane",
        )
        root = ElementTree.fromstring(xml_text)
        namespace = f"{{{windows.TASK_NAMESPACE}}}"
        principal = root.find(f".//{namespace}Principal")
        self.assertIsNotNone(principal)
        self.assertEqual(principal.find(f"{namespace}LogonType").text, "InteractiveToken")
        self.assertEqual(principal.find(f"{namespace}RunLevel").text, "LeastPrivilege")
        self.assertEqual(principal.find(f"{namespace}UserId").text, r"DESKTOP\jane")
        triggers = root.findall(f".//{namespace}LogonTrigger")
        self.assertEqual(len(triggers), 1)

    def test_restart_on_failure_and_no_time_limit(self) -> None:
        root = ElementTree.fromstring(windows.render_task_xml(None))
        namespace = f"{{{windows.TASK_NAMESPACE}}}"
        settings = root.find(f".//{namespace}Settings")
        self.assertEqual(settings.find(f"{namespace}ExecutionTimeLimit").text, "PT0S")
        restart = settings.find(f"{namespace}RestartOnFailure")
        self.assertIsNotNone(restart, "a crashed helper must come back by itself")
        self.assertEqual(restart.find(f"{namespace}Count").text, str(windows.RESTART_COUNT))
        self.assertEqual(settings.find(f"{namespace}MultipleInstancesPolicy").text, "IgnoreNew")

    def test_paths_with_spaces_are_quoted_in_the_action(self) -> None:
        root = ElementTree.fromstring(
            windows.render_task_xml(
                r"C:\Users\Jane Doe\bin\codex.exe",
                python=r"C:\Program Files\Python312\python.exe",
                launcher=r"C:\Users\Jane Doe\repo\bin\codex-auto-resume",
            )
        )
        namespace = f"{{{windows.TASK_NAMESPACE}}}"
        action = root.find(f".//{namespace}Exec")
        self.assertEqual(action.find(f"{namespace}Command").text, r"C:\Program Files\Python312\python.exe")
        arguments = action.find(f"{namespace}Arguments").text
        self.assertIn(r'"C:\Users\Jane Doe\repo\bin\codex-auto-resume"', arguments)
        self.assertIn("helper", arguments)
        self.assertIn('--codex-bin "C:\\Users\\Jane Doe\\bin\\codex.exe"', arguments)

    def test_non_ascii_paths_survive_the_xml_round_trip(self) -> None:
        xml_text = windows.render_task_xml(r"C:\Users\josé\codex.exe")
        self.assertIn("josé", xml_text)
        ElementTree.fromstring(xml_text)

    def test_write_task_xml_is_utf16_for_schtasks(self) -> None:
        path = windows.write_task_xml(r"C:\codex.exe")
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xff\xfe"), "schtasks /xml requires UTF-16 input")
        decoded = raw.decode("utf-16-le")
        self.assertIn("<Task", decoded)

    def test_current_user_id_uses_the_interactive_identity(self) -> None:
        with mock.patch.dict("os.environ", {"USERDOMAIN": "DESKTOP", "USERNAME": "jane"}):
            self.assertEqual(windows.current_user_id(), r"DESKTOP\jane")


class SchtasksSequenceTests(StateCase):
    def _record(self, returncode=0, stdout=b"<Task/>"):
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return completed(argv, returncode=returncode, stdout=stdout)

        return calls, fake_run

    def test_available_probes_schtasks(self) -> None:
        calls, fake_run = self._record()
        with mock.patch.object(windows, "run_command", side_effect=fake_run):
            available, _ = windows.WindowsBackend().available()
        self.assertTrue(available)
        self.assertEqual(calls[0][:2], ["schtasks", "/query"])

    def test_install_creates_the_task_from_the_utf16_xml(self) -> None:
        calls, fake_run = self._record()
        with mock.patch.object(windows, "run_command", side_effect=fake_run):
            description = windows.WindowsBackend().install(r"C:\codex.exe")
        self.assertIn(windows.TASK_NAME, description)
        create = calls[0]
        self.assertEqual(create[:4], ["schtasks", "/create", "/tn", windows.TASK_NAME])
        self.assertIn("/xml", create)
        self.assertIn("/f", create)
        self.assertTrue(windows.task_xml_path().exists())

    def test_install_failure_is_reported(self) -> None:
        def fake_run(argv, **kwargs):
            return completed(argv, returncode=1, stdout="ERROR: Access is denied.".encode("utf-16-le"))

        with mock.patch.object(windows, "run_command", side_effect=fake_run):
            with self.assertRaises(Exception) as caught:
                windows.WindowsBackend().install(None)
        self.assertIn("Access is denied", str(caught.exception))

    def test_start_runs_the_task_without_waiting(self) -> None:
        calls, fake_run = self._record()
        with mock.patch.object(windows, "run_command", side_effect=fake_run):
            windows.WindowsBackend().start()
        self.assertEqual(calls[0][:4], ["schtasks", "/run", "/tn", windows.TASK_NAME])

    def test_task_argv_matches_the_scheduled_action(self) -> None:
        windows.write_task_xml(r"C:\codex\codex.exe")
        argv = windows.task_argv()
        self.assertIsNotNone(argv)
        self.assertEqual(argv[2:4], ["helper", "--state-dir"])
        self.assertIn("--codex-bin", argv)

        import xml.etree.ElementTree as ElementTree

        root = ElementTree.parse(str(windows.task_xml_path())).getroot()
        namespace = f"{{{windows.TASK_NAMESPACE}}}"
        action = root.find(f".//{namespace}Exec")
        self.assertIn("helper", action.findtext(f"{namespace}Arguments"))

    def test_start_falls_back_to_a_detached_spawn(self) -> None:
        """Task Scheduler refusing a per-user start must not stop the helper."""
        windows.write_task_xml(None)

        def failing_run(argv, **kwargs):
            return completed(argv, returncode=1, stdout=b"ERROR: the user is not logged on")

        started: list[list[str]] = []

        def fake_popen(argv, **kwargs):
            started.append(list(argv))
            self.assertIn("helper", argv)
            self.assertTrue(kwargs.get("creationflags"))
            return mock.MagicMock()

        with mock.patch.object(windows, "run_command", side_effect=failing_run):
            with mock.patch.object(windows.subprocess, "Popen", side_effect=fake_popen):
                windows.WindowsBackend().start()
        self.assertEqual(len(started), 1, "the helper must still be started")

    def test_start_reports_the_scheduler_error_when_nothing_can_start(self) -> None:
        def failing_run(argv, **kwargs):
            return completed(argv, returncode=1, stdout=b"ERROR: the user is not logged on")

        with mock.patch.object(windows, "run_command", side_effect=failing_run):
            with mock.patch.object(windows, "task_argv", return_value=None):
                with self.assertRaises(windows.ServiceError) as caught:
                    windows.WindowsBackend().start()
        self.assertIn("not logged on", str(caught.exception))

    def test_stop_ends_the_task(self) -> None:
        calls, fake_run = self._record()
        with mock.patch.object(windows, "run_command", side_effect=fake_run):
            with mock.patch.object(windows, "task_argv", return_value=None):
                windows.WindowsBackend().stop()
        self.assertEqual(calls[0][:4], ["schtasks", "/end", "/tn", windows.TASK_NAME])

    def test_stop_terminates_a_detached_helper(self) -> None:
        """A helper started without the task must still be stoppable."""
        from codex_auto_resume import helper

        with mock.patch.object(windows, "run_command") as run:
            run.return_value = completed(["schtasks"], returncode=0)
            with mock.patch.object(
                helper, "helper_pid_state", return_value={"state": helper.PID_STATE_RUNNING, "pid": 4242}
            ):
                with mock.patch.object(windows.os, "kill") as kill:
                    windows.WindowsBackend().stop()
        kill.assert_called_once()
        self.assertEqual(kill.call_args[0][0], 4242)

    def test_uninstall_deletes_the_task_and_the_definition(self) -> None:
        windows.write_task_xml(None)
        calls, fake_run = self._record()

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            # After /delete, /query must report "not found" for uninstall to finish.
            if "/query" in argv:
                return completed(argv, returncode=1, stdout=b"ERROR: not found")
            return completed(argv)

        with mock.patch.object(windows, "run_command", side_effect=fake_run):
            with mock.patch.object(windows, "task_argv", return_value=None):
                windows.WindowsBackend().uninstall()
        verbs = [call[1] for call in calls]
        self.assertIn("/end", verbs, "uninstall must stop a running helper first")
        self.assertIn("/delete", verbs)
        self.assertFalse(windows.task_xml_path().exists())

    def test_status_reports_the_registered_task(self) -> None:
        xml = windows.render_task_xml(None).encode("utf-16-le")

        def fake_run(argv, **kwargs):
            return completed(argv, returncode=0, stdout=xml)

        with mock.patch.object(windows, "run_command", side_effect=fake_run):
            status = windows.WindowsBackend().status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["enabled"], "enabled")
        self.assertIn("schtasks /run", status["start_hint"])

    def test_status_when_not_installed(self) -> None:
        def fake_run(argv, **kwargs):
            return completed(argv, returncode=1, stdout="ERROR: The system cannot find the file specified.".encode())

        with mock.patch.object(windows, "run_command", side_effect=fake_run):
            status = windows.WindowsBackend().status()
        self.assertFalse(status["installed"])
        self.assertEqual(status["enabled"], "not installed")


if __name__ == "__main__":
    unittest.main()
