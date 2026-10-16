"""Platform detection and the portable process-identity surface.

One module answers "which platform is this?" and exposes the primitives the
rest of the package needs, so no other module has to import ``platform``,
``ctypes`` or read ``/proc``:

* :func:`detect` / :func:`info` — Linux, macOS, Windows, WSL1/WSL2, unknown.
* :func:`boot_id`, :func:`process_start_time`, :func:`process_alive`,
  :func:`process_cmdline` — the same shape as the original Linux-only helpers
  in :mod:`codex_auto_resume.paths`, which now delegate here.
* :func:`host_pid` — "the process that is hosting this Codex session", which is
  the hook's parent on POSIX and the hook's *grandparent* on Windows (there the
  hook runs under ``cmd.exe``, which outlives nothing and must not be recorded).
* :func:`wake_channel_name` — the per-user name a helper waits on for wake-ups
  (a named event on Windows; unused elsewhere, where ``SIGUSR1`` is enough).
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

OS_LINUX = "linux"
OS_MACOS = "macos"
OS_WINDOWS = "windows"
OS_UNKNOWN = "unknown"

BACKEND_SYSTEMD = "systemd"
BACKEND_LAUNCHD = "launchd"
BACKEND_TASK_SCHEDULER = "task-scheduler"
BACKEND_UNSUPPORTED = "unsupported"

#: Shells a Windows hook is wrapped in; the recorded session host is the
#: process above them, not the wrapper itself.
_WINDOWS_WRAPPER_IMAGES = ("cmd.exe", "powershell.exe", "pwsh.exe")

#: How far up the process tree the Windows host walk may go.
_WINDOWS_PARENT_WALK = 6


@dataclass(frozen=True)
class PlatformInfo:
    """What platform we are on, and what that implies for the plugin."""

    os: str
    arch: str
    is_wsl: bool = False
    wsl_version: str = ""
    detail: str = ""
    raw_system: str = ""

    @property
    def is_posix(self) -> bool:
        return self.os in (OS_LINUX, OS_MACOS)

    @property
    def service_backend(self) -> str:
        if self.os == OS_LINUX:
            return BACKEND_SYSTEMD
        if self.os == OS_MACOS:
            return BACKEND_LAUNCHD
        if self.os == OS_WINDOWS:
            return BACKEND_TASK_SCHEDULER
        return BACKEND_UNSUPPORTED

    def label(self) -> str:
        """Human-readable platform name for ``status``/``doctor``."""
        names = {OS_LINUX: "Linux", OS_MACOS: "macOS", OS_WINDOWS: "Windows"}
        name = names.get(self.os, self.raw_system or "unknown")
        suffix = ""
        if self.is_wsl:
            suffix = f" (WSL{self.wsl_version})" if self.wsl_version else " (WSL)"
        return f"{name} {self.arch}{suffix}"

    def as_dict(self) -> dict:
        return {
            "os": self.os,
            "arch": self.arch,
            "is_wsl": self.is_wsl,
            "wsl_version": self.wsl_version or None,
            "service_backend": self.service_backend,
            "label": self.label(),
        }


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _wsl_from_text(text: str) -> tuple[bool, str, str]:
    """Classify WSL from an osrelease/version string."""
    lowered = text.lower()
    if "microsoft" not in lowered and "wsl" not in lowered:
        return False, "", ""
    if "wsl2" in lowered or "microsoft-standard" in lowered:
        return True, "2", "osrelease"
    if "wsl" in lowered:
        return True, "", "osrelease"
    # A WSL1 kernel is a Microsoft-branded 4.x kernel.
    if "microsoft" in lowered:
        return True, "1" if "4.4" in lowered or "3.4" in lowered else "", "osrelease"
    return False, "", ""


def detect(
    raw_system: Optional[str] = None,
    osrelease: Optional[str] = None,
    version_text: Optional[str] = None,
    environ: Optional[dict] = None,
    arch: Optional[str] = None,
) -> PlatformInfo:
    """Detect the platform. Every input is injectable for tests."""
    raw = raw_system if raw_system is not None else sys.platform
    env = environ if environ is not None else dict(os.environ)
    machine = arch if arch is not None else (os.uname().machine if hasattr(os, "uname") else "")

    if raw.startswith("win"):
        return PlatformInfo(OS_WINDOWS, machine, detail="native windows", raw_system=raw)

    if raw == "darwin":
        return PlatformInfo(OS_MACOS, machine, detail="native macos", raw_system=raw)

    if raw.startswith("linux"):
        text = osrelease
        if text is None:
            text = _read_text("/proc/sys/kernel/osrelease")
        detail_version = version_text
        if detail_version is None:
            detail_version = _read_text("/proc/version")
        env_wsl = bool(env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"))
        is_wsl, wsl_version, source = _wsl_from_text(f"{text}\n{detail_version}")
        if not is_wsl and env_wsl:
            is_wsl, source = True, "environment"
            lowered = f"{text}\n{detail_version}".lower()
            wsl_version = "2" if "wsl2" in lowered or "microsoft-standard" in lowered else ""
        detail = "wsl" if is_wsl else "native linux"
        return PlatformInfo(
            OS_LINUX,
            machine,
            is_wsl=is_wsl,
            wsl_version=wsl_version,
            detail=f"{detail} ({source})" if source else detail,
            raw_system=raw,
        )

    return PlatformInfo(
        OS_UNKNOWN,
        machine,
        detail="unsupported platform",
        raw_system=raw,
    )


def info(reload: bool = False) -> PlatformInfo:
    """Cached detection result for this process."""
    global _INFO
    if reload or _INFO is None:
        _INFO = detect()
    return _INFO


_INFO: Optional[PlatformInfo] = None


def is_windows() -> bool:
    return info().os == OS_WINDOWS


def is_macos() -> bool:
    return info().os == OS_MACOS


def is_linux() -> bool:
    return info().os == OS_LINUX


# --------------------------------------------------------------------------- #
# Process identity
# --------------------------------------------------------------------------- #


def boot_id() -> Optional[str]:
    """Boot identity, or None where the platform cannot provide one."""
    platform = info().os
    if platform == OS_LINUX:
        text = _read_text("/proc/sys/kernel/random/boot_id").strip()
        return text or None
    if platform == OS_MACOS:
        from . import macproc

        return macproc.boot_id()
    if platform == OS_WINDOWS:
        from . import winproc

        return winproc.boot_id()
    return None


def process_start_time(pid: int) -> Optional[float]:
    """Start time of ``pid``.

    Linux: jiffies since boot (``/proc/<pid>/stat`` field 22), as before.
    macOS: absolute Unix epoch from ``ps``.
    Windows: absolute FILETIME ticks from ``GetProcessTimes``.
    """
    if pid <= 0:
        return None
    platform = info().os
    if platform == OS_LINUX:
        return _linux_process_start_time(pid)
    if platform == OS_MACOS:
        from . import macproc

        return macproc.process_start_time(pid)
    if platform == OS_WINDOWS:
        from . import winproc

        return winproc.process_start_time(pid)
    return None


def _linux_process_start_time(pid: int) -> Optional[float]:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    # The comm field is wrapped in parentheses and may contain spaces.
    close = data.rfind(b")")
    if close == -1:
        return None
    fields = data[close + 2 :].split()
    # Field 22 overall == index 19 here (fields[0] is "state").
    if len(fields) < 20:
        return None
    try:
        return float(fields[19])
    except ValueError:
        return None


def process_alive(pid: int, start_time: Optional[float] = None) -> bool:
    """True when ``pid`` is alive and, if recorded, is the same process.

    Never uses the pid alone: on Linux the jiffy start time is compared, on
    macOS the ps start time within its 1-second resolution, and on Windows the
    exact FILETIME creation time. Where none of that is available the check
    degrades to a ``kill(pid, 0)`` existence probe exactly as before.
    """
    if pid <= 0:
        return False
    platform = info().os
    if platform == OS_LINUX:
        if os.path.isdir("/proc"):
            current = _linux_process_start_time(pid)
            if current is None:
                return False
            if start_time is None:
                return True
            # /proc reports jiffies; allow a small tolerance for rounding.
            try:
                return abs(current - float(start_time)) <= 1.0
            except (TypeError, ValueError, OverflowError):
                return False
    elif platform == OS_MACOS:
        from . import macproc

        return macproc.process_alive(pid, start_time)
    elif platform == OS_WINDOWS:
        from . import winproc

        return winproc.process_alive(pid, start_time)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_cmdline(pid: int) -> str:
    """Command line of ``pid``; empty where the platform does not expose it."""
    if pid <= 0:
        return ""
    platform = info().os
    if platform == OS_LINUX:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return ""
    if platform == OS_MACOS:
        from . import macproc

        return macproc.process_cmdline(pid)
    if platform == OS_WINDOWS:
        from . import winproc

        return winproc.process_command_line(pid)
    return ""


def process_image_path(pid: int) -> str:
    """Executable path of ``pid`` where the platform can report one."""
    if pid <= 0:
        return ""
    if info().os == OS_WINDOWS:
        from . import winproc

        return winproc.process_image_path(pid)
    cmdline = process_cmdline(pid)
    if not cmdline:
        return ""
    return cmdline.split(" ", 1)[0]


def host_pid() -> int:
    """The process that hosts the current Codex session.

    POSIX: the hook's parent. Codex runs a command hook through
    ``/bin/sh -lc "<command>"`` and the shell execs a single command, so the
    parent is the process that owns the session.

    Windows: the hook runs under ``cmd.exe /C``, which stays alive as the
    parent and exits immediately afterwards. Recording it would make every
    session look detached, so the walk skips shell wrappers and records the
    first real ancestor.
    """
    pid = os.getppid()
    if not is_windows() or pid <= 0:
        return pid
    import ntpath

    from . import winproc

    current = pid
    for _ in range(_WINDOWS_PARENT_WALK):
        parent = winproc.process_parent_pid(current)
        if not parent or parent == current:
            break
        image = ntpath.basename(winproc.process_image_path(parent)).lower()
        current = parent
        if image not in _WINDOWS_WRAPPER_IMAGES:
            break
    return current


# --------------------------------------------------------------------------- #
# Wake channel (Windows named event; POSIX uses SIGUSR1)
# --------------------------------------------------------------------------- #


def wake_channel_name(state_dir: str) -> str:
    """Per-installation event name for waking a running helper on Windows."""
    digest = hashlib.sha256(
        os.path.normcase(os.path.abspath(state_dir)).encode("utf-8", "replace")
    ).hexdigest()[:16]
    return f"Local\\codex-auto-resume-helper-{digest}"
