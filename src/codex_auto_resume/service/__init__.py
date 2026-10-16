"""Platform service lifecycle: one small adapter per platform.

The portable core (quota detection, exact-session recovery, state, resume
execution, cancellation, enable/disable) never calls a platform tool directly;
it asks this package to install, start, stop and inspect "the thing that runs
the helper". Selection is automatic:

===============  ==========================================================
Detected         Backend
===============  ==========================================================
Linux / WSL      ``systemd --user`` unit (unchanged from the verified setup)
macOS            ``launchd`` LaunchAgent in ``~/Library/LaunchAgents``
Windows native   per-user Task Scheduler task (``schtasks``)
anything else    no backend; ``doctor`` explains instead of pretending
===============  ==========================================================

WSL deliberately selects the Linux backend; ``available()`` reports whether the
WSL distribution actually runs systemd, and ``doctor`` explains the
``/etc/wsl.conf`` remediation when it does not.
"""

from __future__ import annotations

from typing import Optional

from .. import sysinfo
from .base import ServiceBackend, ServiceError, run_command

__all__ = [
    "ServiceBackend",
    "ServiceError",
    "available",
    "backend",
    "install_service",
    "service_status",
    "start_helper",
    "stop_helper",
    "uninstall_service",
]


class UnsupportedBackend(ServiceBackend):
    name = "unsupported"
    display = "no supported service backend"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason


_BACKEND: Optional[ServiceBackend] = None


def backend() -> ServiceBackend:
    """The backend for this platform (cached; call ``reload`` to re-detect)."""
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    platform = sysinfo.info()
    if platform.os == sysinfo.OS_LINUX:
        from .linux import LinuxBackend

        _BACKEND = LinuxBackend()
    elif platform.os == sysinfo.OS_MACOS:
        from .macos import MacOSBackend

        _BACKEND = MacOSBackend()
    elif platform.os == sysinfo.OS_WINDOWS:
        from .windows import WindowsBackend

        _BACKEND = WindowsBackend()
    else:
        _BACKEND = UnsupportedBackend(
            f"unsupported operating system: {platform.raw_system or 'unknown'}"
        )
    return _BACKEND


def reload() -> ServiceBackend:
    """Forget the cached backend and re-detect (used by tests)."""
    global _BACKEND
    _BACKEND = None
    return backend()


def available() -> tuple[bool, str]:
    """Whether the platform service backend can actually run the helper."""
    return backend().available()


def install_service(codex_bin: Optional[str] = None) -> str:
    return backend().install(codex_bin)


def uninstall_service() -> None:
    backend().uninstall()


def start_helper() -> None:
    """Start the helper now (non-blocking); best effort."""
    backend().start()


def stop_helper() -> None:
    backend().stop()


def service_status() -> dict:
    """Normalized service status for ``status``/``doctor``; never raises."""
    adapter = backend()
    try:
        status = adapter.status()
    except (OSError, RuntimeError, ServiceError) as error:  # pragma: no cover - defensive
        status = {
            "backend": adapter.name,
            "display": adapter.display,
            "installed": adapter.installed(),
            "active": "unavailable",
            "enabled": "unavailable",
            "artifact": str(adapter.artifact_path() or ""),
            "detail": str(error),
            "start_hint": "",
        }
    if not status.get("start_hint"):
        status["start_hint"] = f"start the helper with the {adapter.display}"
    return status
