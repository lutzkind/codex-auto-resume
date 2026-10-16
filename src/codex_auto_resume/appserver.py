"""Talk to ``codex app-server --stdio`` portably.

Two platform details are easy to get wrong and are centralised here:

* ``select`` cannot wait on a pipe on Windows (sockets only), so stdout is
  consumed by a reader thread that feeds a queue and every caller reads with a
  deadline instead of selecting.
* A Codex installed as a ``.cmd`` shim (npm on Windows) cannot be started by
  ``CreateProcess``; :func:`spawn_app_server` goes through
  :func:`codex_auto_resume.sysinfo.spawn_command`, which routes it via
  ``cmd.exe`` with proper quoting.

The JSON-RPC protocol itself is unchanged and still entirely Codex's.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from typing import Optional

from . import sysinfo


def spawn_app_server(codex_bin: str, extra_env: Optional[dict] = None) -> subprocess.Popen:
    """Start one short-lived ``codex app-server --stdio`` process."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    command = [codex_bin, "app-server", "--stdio"]
    return subprocess.Popen(
        sysinfo.spawn_command(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
    )


class LineReader:
    """Read a process's stdout lines with a deadline, on every platform."""

    #: Sentinel pushed after EOF so readers stop waiting.
    EOF = None

    def __init__(self, stream) -> None:
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for line in stream:
                self._queue.put(line)
        except Exception:  # noqa: BLE001 - a closed pipe ends the reader
            pass
        finally:
            self._queue.put(self.EOF)

    def read_line(self, deadline: float) -> Optional[str]:
        """Next line, or None when the deadline passed or stdout closed."""
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                return self._queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
