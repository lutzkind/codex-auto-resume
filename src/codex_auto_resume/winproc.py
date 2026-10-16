"""Windows process and wake primitives, implemented with :mod:`ctypes`.

Everything here is best effort and never raises: when a Windows API is
unavailable the function returns ``None``/``False`` so the caller degrades to
the same behaviour it has on platforms without ``/proc``. The module imports
safely on every platform; only the Windows code paths touch ``ctypes.WinDLL``.

The primitives mirror what the Linux path gets from ``/proc``:

==============================  ===============================================
Linux source                    Windows source
==============================  ===============================================
``/proc/<pid>/stat`` field 22   ``GetProcessTimes`` creation time (FILETIME)
``/proc/<pid>/cmdline``         ``NtQueryInformationProcess`` class 60
``/proc/sys/.../boot_id``       ``NtQuerySystemInformation`` boot time
``kill(pid, SIGUSR1)``          a per-user named event (``Local\\…``)
==============================  ===============================================

That last one matters: Windows has no ``SIGUSR1``, so the helper waits on a
named event object and the CLI/hooks wake it with ``SetEvent``. The event is
created in the caller's session namespace (``Local\\``) and only holds the
name; no data crosses it.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

#: PROCESS_QUERY_LIMITED_INFORMATION (Windows Vista+).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: Maximum command line we are willing to read from another process.
_MAX_COMMAND_LINE = 32768
#: SystemTimeOfDayInformation.
_SYSTEM_TIME_OF_DAY_INFORMATION = 3
#: ProcessCommandLineInformation.
_PROCESS_COMMAND_LINE_INFORMATION = 60
#: ProcessBasicInformation.
_PROCESS_BASIC_INFORMATION_CLASS = 0

_kernel32 = None
_ntdll = None
_load_error: Optional[str] = None


def _load() -> bool:
    """Load kernel32/ntdll once. Returns False where unavailable."""
    global _kernel32, _ntdll, _load_error
    if _kernel32 is not None:
        return True
    if not IS_WINDOWS:
        _load_error = "not windows"
        return False
    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

        _kernel32.OpenProcess.restype = wintypes.HANDLE
        _kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.GetProcessTimes.restype = wintypes.BOOL
        _kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        _kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        _kernel32.CreateEventW.restype = wintypes.HANDLE
        _kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        _kernel32.OpenEventW.restype = wintypes.HANDLE
        _kernel32.OpenEventW.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        _kernel32.SetEvent.restype = wintypes.BOOL
        _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        _kernel32.WaitForSingleObject.restype = wintypes.DWORD
        _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        _ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        _ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.c_ulong,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        _ntdll.NtQuerySystemInformation.restype = ctypes.c_long
        _ntdll.NtQuerySystemInformation.argtypes = [
            ctypes.c_ulong,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        return True
    except Exception as error:  # noqa: BLE001 - never break the caller
        _load_error = f"{type(error).__name__}: {error}"
        _kernel32 = None
        _ntdll = None
        return False


def status() -> dict:
    """Diagnostic summary for ``doctor``; never raises."""
    return {
        "available": _load(),
        "error": _load_error,
    }


def _filestime_to_int(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _open(pid: int) -> Optional[int]:
    if not _load() or pid <= 0:
        return None
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return int(handle) if handle else None


def _close(handle: int) -> None:
    try:
        _kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:  # noqa: BLE001
        return


def _is_running(handle: int) -> bool:
    """True while the process object has not been signalled (still running).

    Opening a pid succeeds for a terminated process too, as long as some handle
    keeps the process object alive (a parent that has not reaped it). Waiting on
    the handle with a zero timeout is the reliable "has it exited?" test, and it
    is what makes a killed helper immediately stale instead of looking alive.
    """
    try:
        result = _kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0)
    except Exception:  # noqa: BLE001
        return False
    return int(result) == _WAIT_TIMEOUT


def process_start_time(pid: int) -> Optional[float]:
    """Creation time of ``pid`` as Windows FILETIME ticks (absolute, not jiffies)."""
    handle = _open(pid)
    if handle is None:
        return None
    try:
        if not _is_running(handle):
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = _kernel32.GetProcessTimes(
            wintypes.HANDLE(handle),
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        return float(_filestime_to_int(created))
    finally:
        _close(handle)


def process_alive(pid: int, start_time: Optional[float] = None) -> bool:
    """True when ``pid`` is alive and (if recorded) has the recorded start time.

    The creation time is absolute on Windows, so a recycled pid can never match
    the value recorded by the original process; a mismatch therefore means
    "not our process" rather than "different boot".
    """
    current = process_start_time(pid)
    if current is None:
        return False
    if start_time is None:
        return True
    try:
        # Exact comparison on purpose: the value is a stable FILETIME, not a
        # rounding-prone jiffy count.
        return int(current) == int(float(start_time))
    except (TypeError, ValueError, OverflowError):
        return False


def process_image_path(pid: int) -> str:
    """Full executable path of ``pid``, or "" when it cannot be read."""
    handle = _open(pid)
    if handle is None:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(
            wintypes.HANDLE(handle), 0, buffer, ctypes.byref(size)
        ):
            return ""
        return buffer.value or ""
    finally:
        _close(handle)


def process_command_line(pid: int) -> str:
    """Command line of ``pid`` via ``NtQueryInformationProcess`` (best effort)."""
    if not _load():
        return ""
    handle = _open(pid)
    if handle is None:
        return ""
    try:
        size = _MAX_COMMAND_LINE
        buffer = ctypes.create_string_buffer(size)
        returned = wintypes.ULONG(0)
        status = _ntdll.NtQueryInformationProcess(
            wintypes.HANDLE(handle),
            _PROCESS_COMMAND_LINE_INFORMATION,
            buffer,
            size,
            ctypes.byref(returned),
        )
        if status != 0:
            return ""
        # UNICODE_STRING { USHORT Length; USHORT MaximumLength; PWSTR Buffer; }
        # with Buffer pointing into the same buffer, right after the header.
        length = int.from_bytes(buffer.raw[0:2], "little")
        if length <= 0 or 16 + length > size:
            return ""
        text = buffer.raw[16 : 16 + length].decode("utf-16-le", "replace")
        return text
    except Exception:  # noqa: BLE001
        return ""
    finally:
        _close(handle)


class _ProcessBasicInformation(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", ctypes.c_long),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_size_t),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_size_t),
        ("InheritedFromUniqueProcessId", ctypes.c_size_t),
    ]


def process_parent_pid(pid: int) -> Optional[int]:
    """Parent process id of ``pid``, or None when unreadable."""
    if not _load():
        return None
    handle = _open(pid)
    if handle is None:
        return None
    try:
        info = _ProcessBasicInformation()
        returned = wintypes.ULONG(0)
        status = _ntdll.NtQueryInformationProcess(
            wintypes.HANDLE(handle),
            _PROCESS_BASIC_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if status != 0:
            return None
        parent = int(info.InheritedFromUniqueProcessId or 0)
        return parent if parent > 0 else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        _close(handle)


def boot_id() -> Optional[str]:
    """A stable identity of the current Windows boot, or None.

    Uses ``NtQuerySystemInformation(SystemTimeOfDayInformation)`` whose first
    field is the boot time. Unlike a wall-clock/uptime difference this is exact,
    so two processes in the same boot always agree bit for bit.
    """
    if not _load():
        return None
    try:
        buffer = ctypes.create_string_buffer(64)
        returned = wintypes.ULONG(0)
        status = _ntdll.NtQuerySystemInformation(
            _SYSTEM_TIME_OF_DAY_INFORMATION,
            buffer,
            64,
            ctypes.byref(returned),
        )
        if status != 0:
            return None
        boot_ticks = int.from_bytes(buffer.raw[0:8], "little", signed=False)
        if boot_ticks <= 0:
            return None
        return f"win-boot-{boot_ticks}"
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Named wake event
# --------------------------------------------------------------------------- #

_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102


def create_wake_event(name: str) -> Optional[int]:
    """Create (or open) the auto-reset wake event for this user."""
    if not _load():
        return None
    try:
        handle = _kernel32.CreateEventW(None, False, False, name)
        return int(handle) if handle else None
    except Exception:  # noqa: BLE001
        return None


def open_wake_event(name: str) -> Optional[int]:
    """Open the wake event for signalling; None when no helper published one."""
    if not _load():
        return None
    try:
        handle = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        return int(handle) if handle else None
    except Exception:  # noqa: BLE001
        return None


def set_event(handle: int) -> bool:
    if not _load() or not handle:
        return False
    try:
        return bool(_kernel32.SetEvent(wintypes.HANDLE(handle)))
    except Exception:  # noqa: BLE001
        return False


def wait_event(handle: int, timeout_seconds: float) -> bool:
    """True when the event was signalled, False on timeout (or any failure)."""
    if not _load() or not handle:
        return False
    try:
        milliseconds = max(0, int(timeout_seconds * 1000.0))
        result = _kernel32.WaitForSingleObject(
            wintypes.HANDLE(handle), wintypes.DWORD(milliseconds)
        )
        return int(result) in (_WAIT_OBJECT_0,)
    except Exception:  # noqa: BLE001
        return False


def close_handle(handle: Optional[int]) -> None:
    if handle:
        _close(handle)


def now_ticks() -> int:
    """Wall clock as FILETIME ticks; only used for diagnostics."""
    return int(time.time() * 10_000_000) + 116444736000000000
