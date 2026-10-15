"""Persistent state: settings, session registry, pending recoveries.

One small JSON document per session lives under the state directory. There is
no database: a truncated or missing file can only ever lose one session's
recovery, and every write is atomic.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

from . import paths

SCHEMA_VERSION = 1

#: A quota interruption was detected and the helper is waiting for the reset.
STATE_WAITING = "waiting_for_quota"
#: The helper launched the resume for this session.
STATE_RESUMING = "resuming"
#: The session finished normally after a resume; nothing left to do.
STATE_COMPLETED = "completed"
#: The resume failed for a reason that waiting cannot fix.
STATE_FAILED = "failed"
#: Codex reported a quota problem that has no reset time (credits, spend cap).
STATE_NOT_RESUMABLE = "not_resumable"

ACTIVE_STATES = (STATE_WAITING, STATE_RESUMING)

DEFAULT_DELAY_SECONDS = 15 * 60
MIN_AUTOMATIC_DELAY_SECONDS = 60
MAX_AUTOMATIC_DELAY_SECONDS = 6 * 60 * 60
#: Small safety margin added to a reset timestamp before retrying.
RESET_MARGIN_SECONDS = 10
#: Do not retry a failed resume more often than this.
MIN_ATTEMPT_INTERVAL_SECONDS = 5 * 60

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "enabled": True,
    "continue_prompt": (
        "Continue the unfinished task from where this session stopped. "
        "Preserve the existing requirements and complete the requested end state."
    ),
    "reset_margin_seconds": RESET_MARGIN_SECONDS,
    "resume_when_attached": False,
}


def load_settings() -> dict:
    stored = paths.read_json(paths.config_file())
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in DEFAULT_SETTINGS or key == "version":
                settings[key] = value
    settings["version"] = SCHEMA_VERSION
    return settings


def save_settings(settings: dict) -> None:
    merged = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in settings:
            merged[key] = settings[key]
    merged["version"] = SCHEMA_VERSION
    paths.atomic_write_json(paths.config_file(), merged)


def is_enabled() -> bool:
    return bool(load_settings().get("enabled", True))


def set_enabled(value: bool) -> dict:
    settings = load_settings()
    settings["enabled"] = bool(value)
    save_settings(settings)
    return settings


# --------------------------------------------------------------------------- #
# Session registry
# --------------------------------------------------------------------------- #


def _safe_name(session_id: str) -> str:
    return "".join(ch for ch in session_id if ch.isalnum() or ch in "-_.@")[:200]


def session_path(session_id: str):
    return paths.sessions_dir() / f"{_safe_name(session_id)}.json"


def pending_path(session_id: str):
    return paths.pending_dir() / f"{_safe_name(session_id)}.json"


def cancelled_path(session_id: str):
    return paths.cancelled_dir() / f"{_safe_name(session_id)}.json"


def launching_path(session_id: str):
    return paths.launching_dir() / f"{_safe_name(session_id)}.json"


def read_session(session_id: str) -> Optional[dict]:
    return paths.read_json(session_path(session_id))


def write_session(session_id: str, record: dict) -> None:
    record = dict(record)
    record["version"] = SCHEMA_VERSION
    record["session_id"] = session_id
    paths.atomic_write_json(session_path(session_id), record)


def iter_sessions() -> Iterable[dict]:
    if not paths.sessions_dir().is_dir():
        return []
    records = []
    for path in sorted(paths.sessions_dir().glob("*.json")):
        record = paths.read_json(path)
        if record and record.get("session_id"):
            records.append(record)
    return records


# --------------------------------------------------------------------------- #
# Pending recoveries
# --------------------------------------------------------------------------- #


def read_pending(session_id: str) -> Optional[dict]:
    return paths.read_json(pending_path(session_id))


def write_pending(record: dict) -> None:
    record = dict(record)
    record["version"] = SCHEMA_VERSION
    record["updated_at"] = time.time()
    paths.atomic_write_json(pending_path(record["session_id"]), record)


def iter_pending() -> list[dict]:
    if not paths.pending_dir().is_dir():
        return []
    records = []
    for path in sorted(paths.pending_dir().glob("*.json")):
        record = paths.read_json(path)
        if record and record.get("session_id"):
            records.append(record)
    records.sort(key=lambda item: item.get("resume_at") or item.get("created_at") or 0)
    return records


def active_pending() -> list[dict]:
    return [r for r in iter_pending() if r.get("state") in ACTIVE_STATES]


def clear_pending(session_id: str, status: str, detail: Optional[dict] = None) -> None:
    """Drop the active pending record, keeping a short audit trail in the log."""
    paths.remove_file(pending_path(session_id))
    record = paths.read_json(session_path(session_id)) or {}
    record.pop("pending_state", None)
    if record:
        write_session(session_id, record)
    del status, detail


# --------------------------------------------------------------------------- #
# Cancellation tombstones
# --------------------------------------------------------------------------- #


def read_cancelled(session_id: str) -> Optional[dict]:
    return paths.read_json(cancelled_path(session_id))


def write_cancelled(session_id: str, reason: str, interruption_at: Optional[float]) -> dict:
    record = {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "cancelled_at": time.time(),
        "interruption_at": interruption_at,
        "reason": reason,
    }
    paths.atomic_write_json(cancelled_path(session_id), record)
    return record


def clear_cancelled(session_id: str) -> None:
    paths.remove_file(cancelled_path(session_id))


def is_cancelled(session_id: str, interruption_at: Optional[float]) -> bool:
    """True when a tombstone covers this interruption.

    A tombstone only covers interruptions that happened at or before it, so a
    later, genuinely new quota interruption is eligible for recovery again.
    """
    record = read_cancelled(session_id)
    if not record:
        return False
    cancelled_at = record.get("cancelled_at") or 0
    if interruption_at is None:
        return True
    return float(interruption_at) <= float(cancelled_at)


# --------------------------------------------------------------------------- #
# Launch markers (distinguish our own resume from a manual user resume)
# --------------------------------------------------------------------------- #


def mark_launching(session_id: str, extra: Optional[dict] = None) -> None:
    record = {"version": SCHEMA_VERSION, "session_id": session_id, "launched_at": time.time()}
    if extra:
        record.update(extra)
    paths.atomic_write_json(launching_path(session_id), record)


def read_launching(session_id: str, max_age_seconds: float = 12 * 3600) -> Optional[dict]:
    record = paths.read_json(launching_path(session_id))
    if not record:
        return None
    launched_at = record.get("launched_at") or 0
    if time.time() - float(launched_at) > max_age_seconds:
        return None
    return record


def clear_launching(session_id: str) -> None:
    paths.remove_file(launching_path(session_id))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def new_pending(
    session_id: str,
    *,
    cwd: Optional[str],
    model: Optional[str],
    permission_mode: Optional[str],
    transcript_path: Optional[str],
    message: Optional[str],
    reset_at: Optional[float],
    reset_source: str,
    interruption_at: Optional[float],
    now: Optional[float] = None,
) -> dict:
    now = now if now is not None else time.time()
    margin = float(load_settings().get("reset_margin_seconds", RESET_MARGIN_SECONDS))
    if reset_at is not None:
        resume_at = float(reset_at) + margin
    else:
        resume_at = now + DEFAULT_DELAY_SECONDS
    return {
        "version": SCHEMA_VERSION,
        "session_id": session_id,
        "state": STATE_WAITING,
        "created_at": now,
        "updated_at": now,
        "interruption_at": interruption_at,
        "cwd": cwd,
        "model": model,
        "permission_mode": permission_mode,
        "transcript_path": transcript_path,
        "quota_message": message,
        "reset_at": float(reset_at) if reset_at is not None else None,
        "reset_source": reset_source,
        "resume_at": resume_at,
        "attempts": 0,
        "last_attempt_at": None,
        "last_result": None,
    }


def next_resume_delay(attempts: int) -> float:
    """Backoff used only when Codex reported no usable reset time."""
    exponent = max(0, min(int(attempts), 8))
    delay = DEFAULT_DELAY_SECONDS * (1.5 ** exponent)
    return max(MIN_AUTOMATIC_DELAY_SECONDS, min(delay, MAX_AUTOMATIC_DELAY_SECONDS))
