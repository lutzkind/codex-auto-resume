"""Filesystem locations, atomic writes, advisory locking, process identity.

The state directory follows each platform's convention:

===========================  ==================================================
Platform                     Default state directory
===========================  ==================================================
Linux / WSL                  ``${XDG_STATE_HOME:-~/.local/state}/codex-auto-resume``
macOS                        ``~/Library/Application Support/codex-auto-resume``
Windows                      ``%LOCALAPPDATA%\\codex-auto-resume``
===========================  ==================================================

``CODEX_AUTO_RESUME_STATE_DIR`` always wins, and an explicitly set
``XDG_STATE_HOME`` is honoured everywhere (it is an intentional choice when
present). On macOS an existing ``~/.local/state/codex-auto-resume`` is kept in
use rather than silently abandoned, so pre-existing installs continue with
their pending recoveries; the move is one directory rename either way.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from . import sysinfo

ENV_STATE_DIR = "CODEX_AUTO_RESUME_STATE_DIR"
ENV_CODEX_HOME = "CODEX_HOME"

DIR_MODE = 0o700
FILE_MODE = 0o600

#: How long a blocking lock waits before giving up (Windows retry loop).
LOCK_TIMEOUT_SECONDS = 60.0


def codex_home() -> Path:
    """Return the Codex home directory Codex itself would use.

    ``find_codex_home`` in Codex is ``$CODEX_HOME`` or ``<home>/.codex`` on
    every platform (Windows included), which is exactly what this returns.
    """
    override = os.environ.get(ENV_CODEX_HOME)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def _xdg_state_home() -> Path:
    override = os.environ.get("XDG_STATE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state"


def _windows_local_appdata() -> Path:
    override = os.environ.get("LOCALAPPDATA")
    if override:
        return Path(override).expanduser()
    return Path.home() / "AppData" / "Local"


def _macos_state_home() -> Path:
    """``~/Library/Application Support`` unless XDG or a legacy dir applies."""
    if os.environ.get("XDG_STATE_HOME"):
        # An explicit XDG_STATE_HOME is an intentional choice; honour it.
        return _xdg_state_home() / "codex-auto-resume"
    legacy = Path.home() / ".local" / "state" / "codex-auto-resume"
    if legacy.is_dir():
        return legacy
    return Path.home() / "Library" / "Application Support" / "codex-auto-resume"


def state_dir_source() -> str:
    """Where the state directory came from, for ``doctor`` output."""
    if os.environ.get(ENV_STATE_DIR):
        return ENV_STATE_DIR
    if os.environ.get("XDG_STATE_HOME"):
        return "XDG_STATE_HOME"
    platform = sysinfo.info().os
    if platform == sysinfo.OS_WINDOWS:
        return "LOCALAPPDATA"
    if platform == sysinfo.OS_MACOS:
        if os.environ.get("XDG_STATE_HOME"):
            return "XDG_STATE_HOME"
        if (Path.home() / ".local" / "state" / "codex-auto-resume").is_dir():
            return "legacy ~/.local/state"
        return "~/Library/Application Support"
    return "XDG_STATE_HOME (~/.local/state)"


def state_dir() -> Path:
    """Return the tool's state directory (never created here)."""
    override = os.environ.get(ENV_STATE_DIR)
    if override:
        return Path(override).expanduser()
    platform = sysinfo.info().os
    if platform == sysinfo.OS_WINDOWS:
        return _windows_local_appdata() / "codex-auto-resume"
    if platform == sysinfo.OS_MACOS:
        return _macos_state_home()
    return _xdg_state_home() / "codex-auto-resume"


def config_file() -> Path:
    return state_dir() / "config.json"


def sessions_dir() -> Path:
    return state_dir() / "sessions"


def pending_dir() -> Path:
    return state_dir() / "pending"


def cancelled_dir() -> Path:
    return state_dir() / "cancelled"


def launching_dir() -> Path:
    return state_dir() / "launching"


def logs_dir() -> Path:
    return state_dir() / "logs"


def log_file() -> Path:
    return logs_dir() / "helper.log"


def lock_file() -> Path:
    return state_dir() / "helper.lock"


def helper_pid_file() -> Path:
    return state_dir() / "helper.pid"


def all_dirs() -> list[Path]:
    return [
        state_dir(),
        sessions_dir(),
        pending_dir(),
        cancelled_dir(),
        launching_dir(),
        logs_dir(),
    ]


def ensure_dirs() -> None:
    for directory in all_dirs():
        ensure_dir(directory)


def ensure_dir(path: Path, mode: int = DIR_MODE) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def atomic_write_text(path: Path, text: str, mode: int = FILE_MODE) -> None:
    """Write ``text`` to ``path`` atomically (temp file + fsync + rename)."""
    ensure_dir(path.parent)
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def atomic_write_json(path: Path, payload: Any, mode: int = FILE_MODE) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    atomic_write_text(path, text, mode=mode)


def read_json(path: Path) -> Optional[dict]:
    """Read a JSON object, returning ``None`` for missing or corrupt files."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def remove_file(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()


@contextlib.contextmanager
def file_lock(path: Path, blocking: bool = True) -> Iterator[bool]:
    """Advisory exclusive lock. Yields True when the lock was acquired."""
    ensure_dir(path.parent)
    if sysinfo.is_windows():
        yield from _windows_file_lock(path, blocking)
        return
    import fcntl

    handle = os.open(str(path), os.O_RDWR | os.O_CREAT, FILE_MODE)
    acquired = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def _windows_file_lock(path: Path, blocking: bool) -> Iterator[bool]:
    """Byte-range lock through ``msvcrt.locking`` (Windows has no flock)."""
    import msvcrt

    handle = os.open(str(path), os.O_RDWR | os.O_CREAT, FILE_MODE)
    acquired = False
    deadline = time.monotonic() + (LOCK_TIMEOUT_SECONDS if blocking else 0.0)
    try:
        while True:
            try:
                msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                os.lseek(handle, 0, os.SEEK_SET)
                msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        os.close(handle)


def boot_id() -> Optional[str]:
    """Boot identity, or ``None`` where the platform does not expose one.

    Recorded alongside the helper pid: on Linux process start times are jiffies
    since boot, so a pid reuse after a reboot could otherwise look like the same
    process. macOS and Windows report an absolute creation time, which cannot
    alias across a reboot; they still provide a boot id so a marker from a
    previous boot is reported as such instead of as a generic mismatch.
    """
    return sysinfo.boot_id()


def process_start_time(pid: int) -> Optional[float]:
    """Start time of ``pid`` in the platform's native unit (see sysinfo)."""
    return sysinfo.process_start_time(pid)


def process_alive(pid: int, start_time: Optional[float] = None) -> bool:
    """Return True when ``pid`` is alive *and* is the recorded process.

    On Linux the recorded start time is compared too, so a recycled PID is not
    mistaken for the original process. Where /proc is unavailable the check
    degrades to a ``kill(pid, 0)`` existence probe (POSIX) or a process handle
    probe (Windows), exactly as before.
    """
    return sysinfo.process_alive(pid, start_time)


def process_cmdline(pid: int) -> str:
    """Command line of ``pid``; empty where the platform does not expose it."""
    return sysinfo.process_cmdline(pid)
