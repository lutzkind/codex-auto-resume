"""Filesystem locations, atomic writes and advisory locking."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

ENV_STATE_DIR = "CODEX_AUTO_RESUME_STATE_DIR"
ENV_CODEX_HOME = "CODEX_HOME"

DIR_MODE = 0o700
FILE_MODE = 0o600


def codex_home() -> Path:
    """Return the Codex home directory Codex itself would use."""
    override = os.environ.get(ENV_CODEX_HOME)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def _xdg_state_home() -> Path:
    override = os.environ.get("XDG_STATE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "state"


def state_dir() -> Path:
    """Return the tool's state directory (never created here)."""
    override = os.environ.get(ENV_STATE_DIR)
    if override:
        return Path(override).expanduser()
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
    import fcntl

    ensure_dir(path.parent)
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


def boot_id() -> Optional[str]:
    """Kernel boot id, or ``None`` where the platform does not expose one.

    Recorded alongside the helper pid: process start times are jiffies since
    boot, so a pid reuse after a reboot could otherwise look like the same
    process. This is a plain /proc read, not new infrastructure.
    """
    try:
        text = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    text = text.strip()
    return text or None


def process_start_time(pid: int) -> Optional[float]:
    """Return the start time (jiffies since boot) of ``pid`` from /proc.

    Returns ``None`` on platforms without /proc (macOS, the BSDs), where PID
    reuse cannot be detected and liveness falls back to a signal probe.
    """
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
    """Return True when ``pid`` is alive.

    On Linux the recorded start time is compared too, so a recycled PID is not
    mistaken for the original process. Where /proc is unavailable the check
    degrades to a ``kill(pid, 0)`` existence probe.
    """
    if pid <= 0:
        return False
    if os.path.isdir("/proc"):
        current = process_start_time(pid)
        if current is None:
            return False
        if start_time is None:
            return True
        # /proc reports jiffies; allow a small tolerance for rounding.
        return abs(current - float(start_time)) <= 1.0
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
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""
