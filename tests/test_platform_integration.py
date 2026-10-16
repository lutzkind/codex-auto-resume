"""Real platform-service lifecycle verification (opt-in).

These tests drive the platform's own service manager — ``systemd --user``,
``launchd`` or the Windows Task Scheduler — exactly as an installed plugin
would, and prove the whole chain works on that machine:

1. the helper integration installs and uninstalls;
2. the helper starts on demand and is positively identified;
3. a bounded wake reaches the running helper;
4. a real pending recovery is resumed end to end by the helper the service
   manager started, with the fake Codex CLI recording the exact
   ``exec resume <SESSION-UUID>`` invocation, and the state cleaned up after.

They are opt-in because they touch the user's real service manager:

.. code-block:: bash

    CODEX_AUTO_RESUME_PLATFORM_IT=1 python -m unittest discover -s tests

CI runs them on Ubuntu, macOS and Windows; ``scripts/check.sh`` does not. When
the platform's user-session facility is missing (for example WSL without
systemd, or a container without a user bus) the test reports that exact
limitation instead of pretending the lifecycle was verified.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import REPO_ROOT, task_complete

GATE = os.environ.get("CODEX_AUTO_RESUME_PLATFORM_IT") == "1"
SESSION_ID = "01a13e0c-d592-7401-b638-b8b61003c399"


def wait_for(predicate, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.5)
    return predicate()


def fake_codex_wrapper(directory: Path, scenario_path: Path) -> Path:
    """An executable fake Codex CLI that carries its own scenario env.

    The service manager does not pass the test's environment to the helper, so
    the wrapper sets ``FAKE_CODEX_SCENARIO`` itself. On Windows the wrapper is a
    ``.cmd`` shim, which is also what an npm-managed Codex installs — so a pass
    here proves the ``.cmd`` spawn path works for real.
    """
    script = REPO_ROOT / "tests" / "fake_codex.py"
    if sys.platform == "win32":
        target = directory / "codex.cmd"
        target.write_text(
            "@echo off\r\n"
            f'set "FAKE_CODEX_SCENARIO={scenario_path}"\r\n'
            f'"{sys.executable}" "{script}" %*\r\n',
            encoding="ascii",
        )
        return target
    target = directory / "codex"
    target.write_text(
        "#!/bin/sh\n"
        f"export FAKE_CODEX_SCENARIO={shell_quote(scenario_path)}\n"
        f"exec {shell_quote(sys.executable)} {shell_quote(script)} \"$@\"\n",
        encoding="utf-8",
    )
    target.chmod(0o755)
    return target


def shell_quote(path: Path) -> str:
    import shlex

    return shlex.quote(str(path))


@unittest.skipUnless(GATE, "set CODEX_AUTO_RESUME_PLATFORM_IT=1 to exercise the real service manager")
class PlatformLifecycleTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        from codex_auto_resume import paths, service, sysinfo

        self.paths = paths
        self.service = service
        self.sysinfo = sysinfo
        available, detail = service.available()
        if not available:
            self.skipTest(f"{service.backend().display} is not usable here: {detail}")

        self.tmp = Path(tempfile.mkdtemp(prefix="car-platform-it-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.codex_home = self.tmp / "codex-home"
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.transcript = self.tmp / "sessions" / f"{SESSION_ID}.jsonl"
        self.argv_log = self.tmp / "resume-argv.jsonl"
        self.scenario_path = self.tmp / "scenario.json"
        self.resume_count = self.tmp / "resume-count"
        self.fake_bin = fake_codex_wrapper(self.tmp, self.scenario_path)

        previous = {
            "CODEX_AUTO_RESUME_STATE_DIR": os.environ.get("CODEX_AUTO_RESUME_STATE_DIR"),
            "CODEX_HOME": os.environ.get("CODEX_HOME"),
        }
        os.environ["CODEX_AUTO_RESUME_STATE_DIR"] = str(self.state)
        os.environ["CODEX_HOME"] = str(self.codex_home)
        self.addCleanup(self._restore_env, previous)

        paths.ensure_dirs()
        self.addCleanup(self._remove_service)
        self._write_scenario()
        self._write_transcript()

    def _restore_env(self, previous: dict) -> None:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _remove_service(self) -> None:
        try:
            self.service.uninstall_service()
        except Exception as error:  # noqa: BLE001 - cleanup must not mask results
            print(f"warning: could not remove the helper integration: {error}", file=sys.stderr)

    def _write_scenario(self) -> None:
        self.scenario_path.write_text(
            json.dumps(
                {
                    "transcript": str(self.transcript),
                    "resume_outcome": "completed",
                    "ordinary_usage_allowed": True,
                    "windows": [],
                    "resume_count_file": str(self.resume_count),
                    "argv_log": str(self.argv_log),
                }
            ),
            encoding="utf-8",
        )

    def _write_transcript(self) -> None:
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        lines = [
            json.dumps(
                {
                    "timestamp": "2026-10-15T05:30:21.864Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": SESSION_ID,
                        "id": SESSION_ID,
                        "cwd": str(self.tmp / "project"),
                        "cli_version": "0.162.1",
                        "source": "cli",
                    },
                }
            ),
            json.dumps(
                task_complete(
                    message="You've hit your usage limit. Try again at 7:51 AM.",
                    error_info="usage_limit_exceeded",
                    at=now - 900,
                )
            ),
        ]
        self.transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # An old transcript means no Codex client can still be attached to it.
        old = now - 900
        os.utime(self.transcript, (old, old))

    def _write_pending(self) -> None:
        now = time.time()
        record = {
            "version": 1,
            "session_id": SESSION_ID,
            "state": "waiting_for_quota",
            "created_at": now - 900,
            "updated_at": now - 900,
            "interruption_at": now - 900,
            "cwd": str(self.tmp / "project"),
            "model": None,
            "permission_mode": None,
            "transcript_path": str(self.transcript),
            "quota_message": "You've hit your usage limit. Try again at 7:51 AM.",
            "reset_at": now - 600,
            "reset_source": "transcript_message",
            "resume_at": now - 30,
            "attempts": 0,
            "last_attempt_at": None,
            "last_result": None,
        }
        self.paths.atomic_write_json(
            self.paths.pending_dir() / f"{SESSION_ID}.json", record
        )

    # ------------------------------------------------------------------ #

    def test_install_uninstall_round_trip(self) -> None:
        self.service.install_service(str(self.fake_bin))
        status = self.service.service_status()
        self.assertTrue(status["installed"], status)
        self.assertEqual(status["backend"], self.service.backend().name)

        self.service.uninstall_service()
        self.assertFalse(self.service.backend().installed())
        self.assertFalse(self.service.service_status()["installed"])

    def test_helper_starts_on_demand_and_is_identified(self) -> None:
        from codex_auto_resume import helper, log

        self.service.install_service(str(self.fake_bin))
        try:
            self.service.start_helper()
        except Exception as error:  # noqa: BLE001 - report the platform's own words
            self.fail(f"the {self.service.backend().display} refused to start the helper: {error}")
        started = wait_for(helper.is_helper_running, timeout=90)
        if not started:
            records = "\n".join(
                json.dumps(record, ensure_ascii=False) for record in log.read_records(20)
            )
            self.fail(
                "the service manager started nothing that could be identified as the helper; "
                f"status={json.dumps(self.service.service_status(), default=str)}\nlogs:\n{records}"
            )
        verdict = helper.helper_pid_state()
        self.assertEqual(verdict["state"], helper.PID_STATE_RUNNING)
        self.assertIn("helper", self.paths.process_cmdline(verdict["pid"]))
        # A bounded wake must reach the running helper on this platform.
        self.assertTrue(helper.notify_helper())

        self.service.stop_helper()
        stopped = wait_for(lambda: not helper.is_helper_running(), timeout=60)
        self.assertTrue(stopped, "the helper did not stop when the service stopped")

    def test_exact_session_resume_through_the_real_service(self) -> None:
        self._write_pending()
        self.service.install_service(str(self.fake_bin))
        self.service.start_helper()

        resumed = wait_for(lambda: self.argv_log.exists(), timeout=180)
        if not resumed:
            from codex_auto_resume import log

            tail = "\n".join(
                json.dumps(record, ensure_ascii=False) for record in log.read_records(30)
            )
            self.fail(
                "the helper started by the service manager never resumed the session\n"
                f"argv log: {self.argv_log}\nlog:\n{tail}"
            )

        argv = json.loads(self.argv_log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(argv[0], "exec")
        self.assertEqual(argv[1], "resume")
        self.assertIn(SESSION_ID, argv, "the exact session id must be resumed")
        self.assertNotIn("--last", argv, "the most recent session must never be used")

        cleared = wait_for(
            lambda: not (self.paths.pending_dir() / f"{SESSION_ID}.json").exists(),
            timeout=90,
        )
        self.assertTrue(cleared, "the pending recovery was not cleaned up after the resume")


if __name__ == "__main__":
    unittest.main()
