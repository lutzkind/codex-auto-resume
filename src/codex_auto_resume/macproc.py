"""macOS process and boot primitives.

macOS has no ``/proc``. Codex itself reads a process's start time with
``ps -p <pid> -o stat= -o lstart=`` (``codex-rs/app-server-daemon/src/backend/pid.rs``),
which is the same mechanism used here, with ``LC_ALL=C`` pinned so the month
and weekday names do not depend on the user's locale.

Everything degrades gracefully: a process that cannot be inspected returns
``None``/"" exactly like a missing ``/proc`` entry does on other platforms.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

IS_MACOS = sys.platform == "darwin"

_PS_TIMEOUT = 10

#: ``ps -o lstart=`` prints e.g. ``Thu Oct 16 10:22:31 2026`` in local time.
_LSTART_FORMATS = ("%a %b %d %H:%M:%S %Y", "%a %b  %d %H:%M:%S %Y")


def _ps(*args: str) -> str:
    """Run ``ps`` with a locale-stable environment; "" on any failure."""
    if not IS_MACOS:
        return ""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        completed = subprocess.run(
            ["ps", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_PS_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout


def process_start_time(pid: int) -> Optional[float]:
    """Process start time as an absolute Unix epoch, or None.

    Absolute rather than boot-relative, so the value alone distinguishes a
    recycled pid even across a reboot.
    """
    if pid <= 0:
        return None
    text = _ps("-p", str(pid), "-o", "lstart=").strip()
    if not text:
        return None
    # Collapse repeated spaces, then parse.
    parts = text.split()
    text = " ".join(parts)
    for fmt in _LSTART_FORMATS:
        try:
            parsed = time.strptime(text, fmt)
        except ValueError:
            continue
        return time.mktime(parsed)
    return None


def process_alive(pid: int, start_time: Optional[float] = None) -> bool:
    """True when ``pid`` is alive and matches the recorded start time.

    ``ps`` reports whole seconds, so the comparison allows a 2 s tolerance; a
    recycled pid cannot land inside that window for the same pid.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if start_time is None:
        return True
    current = process_start_time(pid)
    if current is None:
        # The process exists but ps could not describe it; be conservative and
        # treat the recorded identity as unconfirmed.
        return False
    try:
        return abs(float(current) - float(start_time)) <= 2.0
    except (TypeError, ValueError, OverflowError):
        return False


def process_cmdline(pid: int) -> str:
    """Full command line of ``pid`` via ``ps -o command=`` ("" when unreadable)."""
    if pid <= 0:
        return ""
    return _ps("-p", str(pid), "-o", "command=").strip()


def boot_id() -> Optional[str]:
    """Identifier of the current boot from ``sysctl kern.boottime``."""
    if not IS_MACOS:
        return None
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_PS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    # "{ sec = 1697566951, usec = 123456 } Thu Oct 16 ..."
    if "sec" not in text:
        return None
    try:
        fragment = text.split("sec", 1)[1].split("=", 1)[1].split(",", 1)[0].strip()
        seconds = int(fragment)
    except (IndexError, ValueError):
        return None
    if seconds <= 0:
        return None
    return f"darwin-boot-{seconds}"
