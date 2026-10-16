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

import os
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Optional

from .base import ServiceBackend, ServiceError, run_command

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

    from .. import paths as paths_module

    arguments = [
        python_path,
        launcher_script,
        "helper",
        "--state-dir",
        str(paths_module.state_dir()),
    ]
    codex_home = os.environ.get(paths_module.ENV_CODEX_HOME)
    if codex_home:
        arguments += ["--codex-home", codex_home]
    if codex_bin:
        arguments += ["--codex-bin", codex_bin]
    # Task Scheduler hands `Arguments` to the process verbatim; quote each token
    # so user paths with spaces (C:\Users\Jane Doe\...) survive.
    command_line = " ".join(_quote(item) for item in arguments)

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
    """Write the UTF-16 task definition ``schtasks /create /xml`` requires."""
    path = task_xml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = render_task_xml(codex_bin)
    path.write_text(content, encoding="utf-16-le")
    # schtasks wants a byte-order mark for UTF-16 input.
    raw = path.read_bytes()
    if not raw.startswith(b"\xff\xfe"):
        path.write_bytes(b"\xff\xfe" + raw)
    return path


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
        self._schtasks("/delete", "/tn", TASK_NAME, "/f", check=False, text=False)
        if self.installed():
            raise RuntimeError(f"could not delete scheduled task {TASK_NAME}")
        path = task_xml_path()
        if path.exists():
            path.unlink()

    def start(self) -> None:
        """Start the task now; raise with Task Scheduler's own words on failure."""
        completed = self._schtasks("/run", "/tn", TASK_NAME, check=False, text=False)
        if completed.returncode != 0:
            raise ServiceError(
                f"schtasks /run /tn {TASK_NAME} failed: " + _decode(completed.stdout).strip()
            )

    def stop(self) -> None:
        self._schtasks("/end", "/tn", TASK_NAME, check=False, text=False)

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
