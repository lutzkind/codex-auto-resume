"""Read Codex account rate limits through the local app server.

Codex's own TUI and CLI read quota state through the app-server JSON-RPC
surface ``account/rateLimits/read``. That response carries the exact window
reset timestamps (``resetsAt``, Unix seconds) and the authoritative
``ordinaryUsageAllowed`` flag, so this module reuses the same supported
surface instead of guessing from terminal text.

The client is a single short-lived ``codex app-server --stdio`` process: write
``initialize``, ``initialized``, ``account/rateLimits/read`` and read the
response. Nothing is left running afterwards.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from typing import Any, Optional

from . import __version__

DEFAULT_TIMEOUT = 30.0
INITIALIZE_REQUEST_ID = 1
READ_REQUEST_ID = 2


class RateLimitError(RuntimeError):
    pass


def _write(stream, payload: dict) -> None:
    stream.write(json.dumps(payload) + "\n")
    stream.flush()


def _read_response(proc, request_id: int, deadline: float) -> Optional[dict]:
    """Read JSON-RPC messages until the response for ``request_id`` arrives."""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
        if not ready:
            if proc.poll() is not None:
                return None
            continue
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                return None
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("id") != request_id:
            continue
        error = message.get("error")
        if isinstance(error, dict):
            raise RateLimitError(str(error.get("message") or error))
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def query(
    codex_bin: str,
    codex_home: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """Return a normalised rate-limit snapshot, or ``None`` on any failure."""
    env = dict(os.environ)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    proc = None
    try:
        proc = subprocess.Popen(
            [codex_bin, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError:
        return None

    try:
        deadline = time.time() + timeout
        _write(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": INITIALIZE_REQUEST_ID,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex-auto-resume",
                        "title": "codex-auto-resume",
                        "version": __version__,
                    }
                },
            },
        )
        initialized = _read_response(proc, INITIALIZE_REQUEST_ID, deadline)
        if initialized is None:
            return None
        _write(proc.stdin, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
        _write(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": READ_REQUEST_ID,
                "method": "account/rateLimits/read",
                "params": {},
            },
        )
        response = _read_response(proc, READ_REQUEST_ID, deadline)
        if response is None:
            return None
        return normalize(response)
    except (OSError, RateLimitError, ValueError):
        return None
    finally:
        if proc is not None:
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except (OSError, ValueError, AttributeError):
                    pass
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass


def _window(bucket: dict, slot: str, limit_id: Optional[str]) -> Optional[dict]:
    window = bucket.get(slot)
    if not isinstance(window, dict):
        return None
    resets_at = window.get("resetsAt", window.get("resets_at"))
    used = window.get("usedPercent", window.get("used_percent"))
    minutes = window.get("windowDurationMins", window.get("window_minutes"))
    return {
        "bucket": limit_id,
        "slot": slot,
        "used_percent": used if isinstance(used, (int, float)) else None,
        "window_minutes": minutes if isinstance(minutes, int) else None,
        "resets_at": float(resets_at) if isinstance(resets_at, (int, float)) else None,
    }


def normalize(response: dict) -> dict:
    """Flatten an ``account/rateLimits/read`` response into a small snapshot."""
    buckets: list[tuple[Optional[str], dict]] = []
    primary = response.get("rateLimits")
    if isinstance(primary, dict):
        buckets.append((primary.get("limitId") or primary.get("limit_id"), primary))
    by_id = response.get("rateLimitsByLimitId") or response.get("rate_limits_by_limit_id")
    if isinstance(by_id, dict):
        for key, value in by_id.items():
            if isinstance(value, dict):
                buckets.append((value.get("limitId") or key, value))

    windows: list[dict] = []
    plan_type = None
    reached = None
    for limit_id, bucket in buckets:
        if plan_type is None:
            plan_type = bucket.get("planType") or bucket.get("plan_type")
        if reached is None:
            reached = bucket.get("rateLimitReachedType") or bucket.get(
                "rate_limit_reached_type"
            )
        for slot in ("primary", "secondary"):
            window = _window(bucket, slot, limit_id)
            if window is not None:
                windows.append(window)

    allowed = response.get("ordinaryUsageAllowed")
    if allowed is None:
        allowed = response.get("ordinary_usage_allowed")

    upsell = response.get("rateLimitUpsell") or response.get("rate_limit_upsell")
    upsell_reset = None
    if isinstance(upsell, dict):
        candidate = upsell.get("reset_at", upsell.get("resetAt"))
        if isinstance(candidate, (int, float)):
            upsell_reset = float(candidate)

    return {
        "ordinary_usage_allowed": allowed if isinstance(allowed, bool) else None,
        "windows": windows,
        "plan_type": plan_type,
        "rate_limit_reached_type": reached,
        "upsell_reset_at": upsell_reset,
    }


def exhausted_windows(snapshot: dict) -> list[dict]:
    return [
        window
        for window in snapshot.get("windows", [])
        if isinstance(window.get("used_percent"), (int, float))
        and window["used_percent"] >= 100
    ]


def availability(snapshot: Optional[dict], now: float) -> dict:
    """Decide whether the account can run an ordinary request right now."""
    if not snapshot:
        return {"allowed": None, "next_reset_at": None, "reason": "no_snapshot"}
    allowed = snapshot.get("ordinary_usage_allowed")
    exhausted = exhausted_windows(snapshot)

    next_reset = None
    for window in exhausted:
        resets_at = window.get("resets_at")
        if resets_at and resets_at > now:
            next_reset = resets_at if next_reset is None else min(next_reset, resets_at)
    if next_reset is None:
        upsell = snapshot.get("upsell_reset_at")
        if upsell and upsell > now:
            next_reset = upsell

    if allowed is True:
        reason = "ordinary_usage_allowed"
    elif allowed is False:
        reason = "usage_blocked"
    elif not exhausted:
        reason = "no_exhausted_window"
    else:
        reason = "usage_blocked_inferred"
    allowed_now = allowed if isinstance(allowed, bool) else (not exhausted)
    return {"allowed": allowed_now, "next_reset_at": next_reset, "reason": reason}
