"""Shared service-backend interface.

A backend is responsible only for "how does this platform run a user-owned
background process": install, uninstall, start, stop and status. Quota
detection, session tracking, recovery state, resume execution, retry, cancel
and enable/disable all stay in the portable core and are identical everywhere.

Every backend funnels its subprocess calls through :func:`run_command` so tests
can observe and fake the exact command line without touching a real service
manager.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class ServiceError(RuntimeError):
    """Raised when the platform service manager rejects an operation."""


def helper_argv(codex_bin: Optional[str]) -> list[str]:
    """The exact argv a service manager must run for the helper.

    Always the repository/plugin launcher, which adds ``src/`` to ``sys.path``
    itself, so the service needs no PYTHONPATH environment and the generated
    command line is the same shape on every platform. The recorded process
    command line therefore still contains ``codex-auto-resume helper``, which is
    what the pid identity check requires.

    The state directory (and ``CODEX_HOME`` when it was set explicitly at
    install time) are passed as arguments rather than inherited from the service
    manager's environment, which is minimal on launchd and Task Scheduler. The
    values are exactly the ones the installing user has, so an installation that
    moved its state directory keeps working.
    """
    import os
    import sys

    from .. import launcher, paths

    command = [
        sys.executable,
        launcher.launcher_path(),
        "helper",
        "--state-dir",
        str(paths.state_dir()),
    ]
    codex_home = os.environ.get(paths.ENV_CODEX_HOME)
    if codex_home:
        command += ["--codex-home", codex_home]
    if codex_bin:
        command += ["--codex-bin", codex_bin]
    return command


def run_command(
    argv: list[str],
    *,
    timeout: float = 30.0,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run one platform tool and return the completed process.

    ``check=True`` raises :class:`ServiceError` on a non-zero exit (and on any
    failure to start the tool); ``check=False`` returns the process with its
    actual return code and output so callers can probe state. ``text=False``
    keeps stdout as bytes, which Windows' ``schtasks`` requires.
    """
    try:
        completed = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=text,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        if check:
            raise ServiceError(f"{' '.join(argv)} failed: {error}") from error
        return subprocess.CompletedProcess(list(argv), 127, f"{error}")
    if check and completed.returncode != 0:
        raise ServiceError(
            f"{' '.join(argv)} failed: {(completed.stdout or '').strip()}"
        )
    return completed


class ServiceBackend:
    """The small platform surface the portable core depends on."""

    #: Stable identifier reported by ``doctor``: systemd / launchd / task-scheduler.
    name = "unsupported"
    #: Human-readable backend name.
    display = "unsupported"

    def available(self) -> tuple[bool, str]:
        """Whether this backend can work at all on this machine, plus detail."""
        return False, "unsupported platform"

    def artifact_path(self) -> Optional[Path]:
        """The file that represents the installation, when there is one."""
        return None

    def installed(self) -> bool:
        path = self.artifact_path()
        return bool(path and path.exists())

    def install(self, codex_bin: Optional[str]) -> str:
        """Install the helper integration. Returns a description of it."""
        raise ServiceError(f"{self.name} does not support installation")

    def uninstall(self) -> None:
        raise ServiceError(f"{self.name} does not support uninstallation")

    def start(self) -> None:
        """Start the helper now, without waiting for it."""
        raise ServiceError(f"{self.name} cannot start the helper")

    def stop(self) -> None:
        raise ServiceError(f"{self.name} cannot stop the helper")

    def status(self) -> dict:
        """Normalized status: installed / active / enabled / start_hint."""
        return {
            "backend": self.name,
            "display": self.display,
            "installed": False,
            "active": "unavailable",
            "enabled": "unavailable",
            "artifact": None,
            "detail": "unsupported platform",
            "start_hint": "",
        }

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #

    def helper_command(self, codex_bin: Optional[str]) -> list[str]:
        """The exact command this platform's service manager must run."""
        return helper_argv(codex_bin)

    @staticmethod
    def package_parent() -> str:
        """Directory that must be on PYTHONPATH for ``-m codex_auto_resume``."""
        import os

        import codex_auto_resume

        return os.path.dirname(os.path.dirname(os.path.abspath(codex_auto_resume.__file__)))
