"""Shared test scaffolding: isolated state dir, fake clock, transcript builder."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
PACKAGE_ROOT = SRC / "codex_auto_resume"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

FAKE_CODEX = REPO_ROOT / "tests" / "fake_codex.py"
REAL_CODEX_VERSION = os.environ.get("CAR_REAL_CODEX", "")


class StateCase(unittest.TestCase):
    """A test case with an isolated state dir and CODEX_HOME."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="car-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.environ["CODEX_AUTO_RESUME_STATE_DIR"] = str(self.tmp / "state")
        self.codex_home = self.tmp / "codex-home"
        self.codex_home.mkdir(parents=True, exist_ok=True)
        os.environ["CODEX_HOME"] = str(self.codex_home)
        from codex_auto_resume import paths

        paths.ensure_dirs()
        self.paths = paths
        self.state = self._fresh("codex_auto_resume.state")

    def _fresh(self, name: str):
        import importlib

        module = importlib.import_module(name)
        return importlib.reload(module)

    def write_transcript(self, session_id: str, events: list[dict], cwd: str = "/tmp/project") -> Path:
        path = self.tmp / "sessions" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "timestamp": "2026-10-15T05:30:21.864Z",
                    "type": "session_meta",
                    "payload": {
                        "session_id": session_id,
                        "id": session_id,
                        "cwd": cwd,
                        "cli_version": "0.162.1",
                        "source": "cli",
                    },
                }
            )
        ]
        for event in events:
            lines.append(json.dumps(event))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def task_complete(
    *,
    message: str | None = None,
    error_info: str | None = None,
    at: float | None = None,
    turn_id: str = "turn-1",
) -> dict:
    stamp = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.gmtime(at if at is not None else time.time())
    ) + "Z"
    payload: dict = {"type": "task_complete", "turn_id": turn_id}
    if error_info is not None or message is not None:
        payload["error"] = {"message": message, "codex_error_info": error_info}
    return {"timestamp": stamp, "type": "event_msg", "payload": payload}


def task_started(*, at: float | None = None, turn_id: str = "turn-1") -> dict:
    stamp = time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.gmtime(at if at is not None else time.time())
    ) + "Z"
    return {
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": turn_id},
    }


QUOTA_MESSAGE = (
    "You\u2019ve hit your usage limit. Upgrade to Pro "
    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/usage "
    "to purchase more credits or try again at 7:51 AM."
)


class FakeClock:
    """Deterministic clock used to accelerate reset waits in tests."""

    def __init__(self, start: float) -> None:
        self.value = float(start)
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += max(0.0, float(seconds))

    def apply(self, module) -> "FakeClock":
        clock = self

        class _Time:
            @staticmethod
            def time() -> float:
                return clock.value

            @staticmethod
            def sleep(seconds: float) -> None:
                clock.sleeps.append(seconds)
                clock.advance(seconds)

            @staticmethod
            def localtime(*args, **kwargs):
                return time.localtime(*args, **kwargs)

            @staticmethod
            def gmtime(*args, **kwargs):
                return time.gmtime(*args, **kwargs)

            @staticmethod
            def strftime(*args, **kwargs):
                return time.strftime(*args, **kwargs)

        module.time = _Time  # type: ignore[attr-defined]

        class _Select:
            @staticmethod
            def select(readables, writables, errors, timeout=None):  # noqa: ANN001
                if timeout:
                    clock.advance(min(float(timeout), 60.0))
                return ([], [], [])

        module.select = _Select  # type: ignore[attr-defined]
        return self
