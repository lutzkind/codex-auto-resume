"""Windows backend: a per-user Task Scheduler task.

Windows has no user-level equivalent of ``systemd --user``. The lightest
reliable user-space mechanism that provides all four required properties is a
Task Scheduler task created from XML in the current user's context:

==============================  ==============================================
systemd --user (Linux)          Task Scheduler (Windows)
==============================  ==============================================
``WantedBy=default.target``     ``LogonTrigger`` for the current user
``Restart=on-failure``          ``RestartOnFailure`` (PT1M, 10 attempts)
``KillMode=process``            Job-object-free task process
``systemctl --user start``      ``schtasks /run``
``systemctl --user stop``       ``schtasks /end``
==============================  ==============================================

No administrator rights are required: the task runs as the current user, with
``InteractiveToken`` and ``LeastPrivilege``, so it starts at logon and can be
started on demand. A service (``sc.exe``) is deliberately not used — it would
require elevation and would run outside the user's session, which is not needed
for a task that only waits and then resumes a user-owned Codex session.

The task XML is generated from :func:`render_task_xml` and kept under the state
directory so it can be inspected and diffed. ``schtasks`` copies it into the
task store on install.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Optional

from .base import ServiceBackend, ServiceError, helper_argv, run_command

TASK_NAME = "codex-auto-resume-helper"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"

#: How many times the task restarts after a crash, one minute apart.
RESTART_COUNT = 10
RESTART_INTERVAL_MINUTES = 1


def task_xml_path() -> Path:
    from .. import paths

    return paths.state_dir() / "service" / f"{TASK_NAME}.xml"


def current_user_id() -> str:
    """``DOMAIN\\user`` for the interactive-token principal.

    ``whoami`` is authoritative (it resolves the account the way Windows does);
    the environment variables are the fallback, and ``getpass`` the last resort.
    """
    try:
        completed = subprocess.run(
            ["whoami"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10
        )
        value = (completed.stdout or "").strip().splitlines()
        if completed.returncode == 0 and value and "\\" in value[0]:
            return value[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    domain = os.environ.get("USERDOMAIN") or ""
    name = os.environ.get("USERNAME") or ""
    if domain and name:
        return f"{domain}\\{name}"
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - never break installation
        return name or "user"


def _quote(argument: str) -> str:
    """Quote one argument for the Windows command line when needed."""
    if argument and not any(character.isspace() for character in argument) and '"' not in argument:
        return argument
    escaped = argument.replace('"', '\\"')
    return f'"{escaped}"'


def render_task_xml(
    codex_bin: Optional[str],
    *,
    python: Optional[str] = None,
    launcher: Optional[str] = None,
    user_id: Optional[str] = None,
    task_name: str = TASK_NAME,
) -> str:
    """The exact task definition ``install-service`` writes on Windows."""
    from .. import launcher as launcher_module

    python_path = python or sys.executable
    launcher_script = launcher or launcher_module.launcher_path()
    principal_user = user_id or current_user_id()

    # One definition of the helper argv (base.helper_argv); the explicit
    # python/launcher parameters only exist so tests can pin them.
    arguments = helper_argv(codex_bin)
    arguments[0] = python_path
    arguments[1] = launcher_script
    # Task Scheduler runs `<Command>` with `<Arguments>` appended, so the
    # executable appears exactly once: python is the command, everything else
    # is arguments. Quoting each token keeps user paths with spaces intact.
    command_line = " ".join(_quote(item) for item in arguments[1:])

    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-16"?>',
            f'<Task version="1.2" xmlns="{TASK_NAMESPACE}">',
            "  <RegistrationInfo>",
            "    <Description>codex-auto-resume helper: wait for the Codex quota reset "
            "and resume the exact session.</Description>",
            f"    <URI>\\{task_name}</URI>",
            "  </RegistrationInfo>",
            "  <Triggers>",
            "    <LogonTrigger>",
            "      <Enabled>true</Enabled>",
            f"      <UserId>{_escape(principal_user)}</UserId>",
            "    </LogonTrigger>",
            "  </Triggers>",
            "  <Principals>",
            '    <Principal id="Author">',
            f"      <UserId>{_escape(principal_user)}</UserId>",
            "      <LogonType>InteractiveToken</LogonType>",
            "      <RunLevel>LeastPrivilege</RunLevel>",
            "    </Principal>",
            "  </Principals>",
            "  <Settings>",
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
            "    <AllowHardTerminate>true</AllowHardTerminate>",
            "    <StartWhenAvailable>true</StartWhenAvailable>",
            "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
            "    <AllowStartOnDemand>true</AllowStartOnDemand>",
            "    <Enabled>true</Enabled>",
            "    <Hidden>false</Hidden>",
            "    <RunOnlyIfIdle>false</RunOnlyIfIdle>",
            "    <WakeToRun>false</WakeToRun>",
            "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
            "    <Priority>7</Priority>",
            "    <RestartOnFailure>",
            f"      <Interval>PT{RESTART_INTERVAL_MINUTES}M</Interval>",
            f"      <Count>{RESTART_COUNT}</Count>",
            "    </RestartOnFailure>",
            "  </Settings>",
            '  <Actions Context="Author">',
            "    <Exec>",
            f"      <Command>{_escape(python_path)}</Command>",
            f"      <Arguments>{_escape(command_line)}</Arguments>",
            "    </Exec>",
            "  </Actions>",
            "</Task>",
            "",
        ]
    )


def _escape(value: str) -> str:
    from xml.sax.saxutils import escape

    return escape(value)


def _decode(data) -> str:
    """Decode ``schtasks`` output, which is UTF-16 on Windows."""
    if isinstance(data, str):
        return data
    if not data:
        return ""
    if b"\x00" in data[:64]:
        try:
            return data.decode("utf-16-le", "replace").lstrip("\ufeff")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            pass
    return data.decode("utf-8", "replace")


def write_task_xml(codex_bin: Optional[str]) -> Path:
    """Write the UTF-16 task definition ``schtasks /create /xml`` requires.

    The helper's exact argv is written next to it as JSON so the on-demand
    start path (which must work even when Task Scheduler refuses to start a
    per-user task in a non-interactive session) runs precisely the same command
    the task does.
    """
    path = task_xml_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    argv = helper_argv(codex_bin)
    invocation = path.with_name("helper-command.json")
    invocation.write_text(
        json.dumps({"argv": argv}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    content = render_task_xml(codex_bin)
    path.write_text(content, encoding="utf-16-le")
    # schtasks wants a byte-order mark for UTF-16 input.
    raw = path.read_bytes()
    if not raw.startswith(b"\xff\xfe"):
        path.write_bytes(b"\xff\xfe" + raw)
    return path


def task_argv() -> Optional[list[str]]:
    """The argv recorded by :func:`write_task_xml`, or None."""
    invocation = task_xml_path().with_name("helper-command.json")
    record = None
    try:
        record = json.loads(invocation.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    argv = record.get("argv") if isinstance(record, dict) else None
    if isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv):
        return argv
    return None


class WindowsBackend(ServiceBackend):
    name = "task-scheduler"
    display = "Windows Task Scheduler (per-user task)"

    def _schtasks(self, *args: str, check: bool = True, text: bool = True):
        return run_command(["schtasks", *args], check=check, text=text)

    def available(self) -> tuple[bool, str]:
        completed = self._schtasks("/query", "/?", check=False)
        if completed.returncode != 0:
            return False, "schtasks is unavailable"
        return True, "Task Scheduler available for the current user"

    def artifact_path(self) -> Optional[Path]:
        return task_xml_path()

    def installed(self) -> bool:
        completed = self._schtasks("/query", "/tn", TASK_NAME, "/xml", check=False, text=False)
        return completed.returncode == 0

    def install(self, codex_bin: Optional[str]) -> str:
        path = write_task_xml(codex_bin)
        completed = run_command(
            ["schtasks", "/create", "/tn", TASK_NAME, "/xml", str(path), "/f"],
            check=False,
            text=False,
        )
        if completed.returncode != 0:
            raise ServiceError(
                "schtasks /create failed: " + _decode(completed.stdout).strip()
            )
        return f"task {TASK_NAME} (definition kept at {path})"

    def uninstall(self) -> None:
        # Stop whatever is serving a recovery before the integration goes away;
        # a helper started detached is not owned by the task, so it is
        # terminated explicitly (see stop()).
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - removal must continue
            pass
        self._schtasks("/delete", "/tn", TASK_NAME, "/f", check=False, text=False)
        if self.installed():
            raise RuntimeError(f"could not delete scheduled task {TASK_NAME}")
        path = task_xml_path()
        if path.exists():
            path.unlink()
        invocation = path.with_name("helper-command.json")
        if invocation.exists():
            invocation.unlink()

    def start(self) -> None:
        """Start the helper now, and never silently fail to.

        ``schtasks /run`` is the primary path. Task Scheduler can refuse to
        start a per-user interactive-token task when the caller is not in an
        interactive logon session (headless CI, SSH-only sessions, some
        enterprise policies). The helper is a user process, so the fallback is
        to start the very same command detached — the way Codex starts its own
        background app-server daemon — which keeps "the helper starts when it is
        needed" true everywhere, while the scheduled task still provides logon
        persistence and restart-on-failure.
        """
        completed = self._schtasks("/run", "/tn", TASK_NAME, check=False, text=False)
        if completed.returncode == 0:
            return
        reason = _decode(completed.stdout).strip()
        if self._spawn_detached():
            from .. import log

            log.log("helper_start_detached", reason=reason)
            return
        raise ServiceError(f"schtasks /run /tn {TASK_NAME} failed: {reason}")

    def _spawn_detached(self) -> bool:
        argv = task_argv()
        if not argv:
            return False
        base = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        for flags in (base | group | breakaway, base | group):
            try:
                subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                    close_fds=True,
                    cwd=str(Path.home()),
                )
                return True
            except OSError:
                continue
        return False

    def stop(self) -> None:
        """End the task and terminate a detached helper, if one is identified.

        The marker's pid is used directly: it is checked against the recorded
        process identity, which is exactly the promise the marker makes, and it
        also covers a marker whose command line could not be read. On Windows a
        terminated process cannot clean up after itself, so the leftover marker
        is reconciled by the next ``status``/``doctor``/helper start.
        """
        self._schtasks("/end", "/tn", TASK_NAME, check=False, text=False)
        from .. import helper, paths

        record = paths.read_json(paths.helper_pid_file()) or {}
        pid = record.get("pid")
        pid_start = record.get("pid_start")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            verdict = helper.helper_pid_state()
            pid = verdict.get("pid")
            pid_start = verdict.get("pid_start")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return
        if not paths.process_alive(pid, pid_start if isinstance(pid_start, (int, float)) else None):
            return
        import signal

        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            return

    def status(self) -> dict:
        completed = self._schtasks("/query", "/tn", TASK_NAME, "/xml", check=False, text=False)
        installed = completed.returncode == 0
        detail = ""
        enabled = "not installed"
        if installed:
            enabled = "enabled"
            try:
                root = ElementTree.fromstring(_decode(completed.stdout))
                triggers = root.findall(f".//{{{TASK_NAMESPACE}}}LogonTrigger")
                enabled = "enabled" if triggers else "no logon trigger"
            except ElementTree.ParseError as error:
                detail = f"could not parse task definition: {error}"
        elif completed.returncode not in (0, 1):
            detail = _decode(completed.stdout).strip()
        return {
            "backend": self.name,
            "display": self.display,
            "installed": installed,
            "active": "task registered" if installed else "not installed",
            "enabled": enabled,
            "artifact": str(task_xml_path()),
            "detail": detail,
            "start_hint": f"schtasks /run /tn {TASK_NAME}",
        }
