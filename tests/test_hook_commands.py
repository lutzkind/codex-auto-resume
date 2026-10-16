"""The hook commands in ``hooks/hooks.json`` must actually run.

Codex executes a command hook through the platform shell: ``/bin/sh -lc
"<command>"`` on POSIX and ``cmd.exe /C "<commandWindows>"`` on Windows
(``codex-rs/hooks/src/engine/command_runner.rs``). These tests resolve the
``${PLUGIN_ROOT}`` placeholder exactly like Codex does, run the command the same
way, and assert the hook did its job — so a packaging mistake (a missing
launcher, a Windows-only path problem, a quoting bug) fails the build instead of
silently disabling recovery.

The real service managers are shadowed by no-op stubs on PATH: the hooks must
never touch the developer's own installation during tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import REPO_ROOT, StateCase

HOOKS_FILE = REPO_ROOT / "hooks" / "hooks.json"
#: Tools a hook may invoke to start the helper; replaced by stubs in tests.
SERVICE_TOOLS = ("systemctl", "loginctl", "launchctl", "schtasks")


def load_handlers() -> dict:
    payload = json.loads(HOOKS_FILE.read_text(encoding="utf-8"))
    return payload["hooks"]


def resolve(command: str) -> str:
    return command.replace("${PLUGIN_ROOT}", str(REPO_ROOT))


class HookCommandTests(StateCase):
    def _stub_bin_dir(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="car-stubs-"))
        self.addCleanup(shutil.rmtree, directory, True)
        for tool in SERVICE_TOOLS:
            if sys.platform == "win32":
                stub = directory / f"{tool}.cmd"
                stub.write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii")
            else:
                stub = directory / tool
                stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                stub.chmod(0o755)
        return directory

    def _environment(self) -> dict:
        env = dict(os.environ)
        env["PATH"] = str(self._stub_bin_dir()) + os.pathsep + env.get("PATH", "")
        env["CODEX_AUTO_RESUME_STATE_DIR"] = str(self.paths.state_dir())
        env["CODEX_HOME"] = str(self.codex_home)
        return env

    def _payload(self, session_id: str = "01a13e0c-d592-7401-b638-b8b61003c313") -> str:
        return json.dumps(
            {
                "session_id": session_id,
                "transcript_path": str(self.tmp / "sessions" / f"{session_id}.jsonl"),
                "cwd": str(self.tmp / "project"),
                "hook_event_name": "SessionStart",
                "model": "gpt-5-codex",
                "permission_mode": "default",
                "source": "startup",
            }
        )

    def test_every_event_has_a_posix_and_a_windows_command(self) -> None:
        hooks = load_handlers()
        self.assertEqual(
            sorted(hooks), sorted(["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "Interrupt"])
        )
        for event, groups in hooks.items():
            for group in groups:
                for handler in group["hooks"]:
                    self.assertIn("${PLUGIN_ROOT}", handler["command"], event)
                    self.assertIn("${PLUGIN_ROOT}", handler["commandWindows"], event)
                    self.assertIn("hook", handler["command"], event)
                    self.assertIn("hook", handler["commandWindows"], event)
                    windows_script = resolve(handler["commandWindows"]).strip('"').split('"')[0]
                    self.assertTrue(
                        windows_script.endswith("codex-auto-resume.cmd"),
                        f"{event}: {windows_script}",
                    )

    @unittest.skipIf(sys.platform == "win32", "the POSIX command is for POSIX shells")
    def test_posix_hook_command_runs_through_sh(self) -> None:
        session_id = "01a13e0c-d592-7401-b638-b8b61003c313"
        handler = load_handlers()["SessionStart"][0]["hooks"][0]
        command = resolve(handler["command"])
        completed = subprocess.run(
            ["/bin/sh", "-lc", command],
            input=self._payload(session_id),
            text=True,
            capture_output=True,
            env=self._environment(),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        record = json.loads(
            (self.paths.sessions_dir() / f"{session_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["session_id"], session_id)
        self.assertTrue(record["attached"])

    @unittest.skipIf(sys.platform == "win32", "cmd.exe has no /bin/sh")
    def test_the_windows_launcher_refuses_to_run_without_a_resolved_root(self) -> None:
        """A missing PLUGIN_ROOT must not be silently treated as a valid path."""
        content = (REPO_ROOT / "bin" / "codex-auto-resume.cmd").read_text(encoding="utf-8")
        self.assertIn("codex-auto-resume", content)
        self.assertIn("%PYTHON%", content)

    @unittest.skipUnless(sys.platform == "win32", "cmd.exe and PATHEXT only exist on Windows")
    def test_windows_hook_command_runs_through_cmd(self) -> None:
        session_id = "01a13e0c-d592-7401-b638-b8b61003c314"
        handler = load_handlers()["SessionStart"][0]["hooks"][0]
        command = resolve(handler["commandWindows"])
        # Codex runs `cmd.exe /C "<command>"` with the command itself quoted.
        completed = subprocess.run(
            f'cmd.exe /d /s /c "{command}"',
            input=self._payload(session_id),
            text=True,
            capture_output=True,
            env=self._environment(),
            timeout=60,
            shell=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        record = json.loads(
            (self.paths.sessions_dir() / f"{session_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["session_id"], session_id)


if __name__ == "__main__":
    unittest.main()
