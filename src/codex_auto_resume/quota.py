"""Detect a Codex usage-limit interruption and extract the reported reset time.

Two independent sources are used, in this order of authority:

1. Codex's own structured error info recorded in the session transcript:
   ``event_msg`` / ``task_complete`` / ``error.codex_error_info``.
   ``usage_limit_exceeded`` is the value the installed CLI writes.
2. The human readable message Codex prints, parsed for the reset time
   ("... or try again at 7:51 AM."). Codex formats that timestamp in the local
   timezone of the host, so it is parsed back in the same local timezone.

Anything that is not a usage-limit interruption (network failures, cancellation,
auth errors, model errors, plain crashes) must be classified as ``other`` and
never scheduled for automatic resume.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional

#: ``codex_error_info`` value for "you have no quota left right now".
USAGE_LIMIT_ERROR_INFO = "usage_limit_exceeded"

#: Error infos that mean "waiting will not help"; never auto-resume these.
NON_RESUMABLE_ERROR_INFOS = frozenset(
    {
        "unauthorized",
        "bad_request",
        "invalid_prompt",
        "cyber_policy",
        "policy_violation",
        "misalignment",
        "content_filter",
        "context_window_exceeded",
        "sandbox_error",
        "active_turn_not_steerable",
        "thread_rollback_failed",
        "incompatible_compaction",
        "flex_unavailable",
    }
)

#: Outcome names returned by :func:`last_turn_outcome`.
OUTCOME_QUOTA = "quota"
OUTCOME_QUOTA_NO_RESET = "quota_no_reset"
OUTCOME_COMPLETED = "completed"
OUTCOME_ABORTED = "aborted"
OUTCOME_ERRORED = "errored"
OUTCOME_UNKNOWN = "unknown"

_QUOTA_TEXT = re.compile(
    r"(you'?[’']?ve hit your usage limit"
    r"|you have hit your usage limit"
    r"|usage limit reached"
    r"|the usage limit has been reached"
    r"|your workspace is out of credits"
    r"|you hit your spend cap)",
    re.IGNORECASE,
)

_NO_RESET_TEXT = re.compile(
    r"(out of credits|spend cap)",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_RETRY_FULL = re.compile(
    r"try again at\s+"
    r"(?P<month>[A-Za-z]{3})\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,\s*(?P<year>\d{4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)",
    re.IGNORECASE,
)

_RETRY_TIME_ONLY = re.compile(
    r"try again at\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)",
    re.IGNORECASE,
)

_RETRY_ISO = re.compile(
    r"try again at\s+(?P<iso>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)


def _hour24(hour: int, ampm: str) -> Optional[int]:
    if not 1 <= hour <= 12:
        return None
    if ampm.upper() == "AM":
        return 0 if hour == 12 else hour
    return 12 if hour == 12 else hour + 12


def parse_retry_time(text: str, now: Optional[float] = None) -> Optional[float]:
    """Parse Codex's "try again at ..." text into a Unix timestamp.

    Returns ``None`` when no retry time is present or it cannot be parsed.
    A bare clock time is resolved against local midnight and rolled forward by
    one day when it is already in the past, matching how Codex prints it (it
    only omits the date when the reset falls on the current local date).
    """
    if not text:
        return None
    now_dt = datetime.fromtimestamp(now if now is not None else time.time())

    match = _RETRY_FULL.search(text)
    if match:
        month = _MONTHS.get(match.group("month")[:3].lower())
        hour = _hour24(int(match.group("hour")), match.group("ampm"))
        if month is None or hour is None:
            return None
        try:
            candidate = datetime(
                int(match.group("year")),
                month,
                int(match.group("day")),
                hour,
                int(match.group("minute")),
            )
        except ValueError:
            return None
        return candidate.timestamp()

    match = _RETRY_ISO.search(text)
    if match:
        raw = match.group("iso").replace(" ", "T")
        try:
            if raw.endswith("Z"):
                parsed = datetime.fromisoformat(raw[:-1]).replace(
                    tzinfo=now_dt.astimezone().tzinfo
                )
                return parsed.astimezone().timestamp()
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now_dt.astimezone().tzinfo)
        return parsed.timestamp()

    match = _RETRY_TIME_ONLY.search(text)
    if match:
        hour = _hour24(int(match.group("hour")), match.group("ampm"))
        if hour is None:
            return None
        candidate = now_dt.replace(
            hour=hour, minute=int(match.group("minute")), second=0, microsecond=0
        )
        if candidate.timestamp() <= now_dt.timestamp() - 60:
            candidate = candidate + timedelta(days=1)
        return candidate.timestamp()

    return None


def classify_message(text: str) -> Optional[str]:
    """Classify free text as a usage-limit message (``quota``) or not."""
    if not text or not _QUOTA_TEXT.search(text):
        return None
    if _NO_RESET_TEXT.search(text):
        return "quota_no_reset"
    return "quota"


def classify_error(message: Optional[str], error_info: Optional[str]) -> Optional[str]:
    """Classify one Codex error as resumable quota, non-resumable, or other.

    ``error_info`` wins when present: it is Codex's own structured signal, so a
    structured non-quota error is never overridden by text.
    """
    info = (error_info or "").strip()
    if info == USAGE_LIMIT_ERROR_INFO:
        parsed = classify_message(message or "")
        return parsed or OUTCOME_QUOTA
    if info:
        if info in NON_RESUMABLE_ERROR_INFOS:
            return None
        # Unknown structured error: do not guess from text.
        return None
    return classify_message(message or "")


def _iter_tail_lines(path: str, tail_bytes: int) -> list[str]:
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    with open(path, "rb") as handle:
        start = max(0, size - tail_bytes)
        handle.seek(start)
        data = handle.read()
    if start > 0:
        # Drop the first partial line.
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline != -1 else b""
    return [
        line for line in data.decode("utf-8", "replace").splitlines() if line.strip()
    ]


def _iter_head_lines(path: str, head_bytes: int) -> list[str]:
    try:
        with open(path, "rb") as handle:
            data = handle.read(head_bytes)
    except OSError:
        return []
    return [line for line in data.decode("utf-8", "replace").splitlines() if line.strip()]


def _loads(line: str) -> Optional[dict]:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def session_meta(transcript_path: str) -> dict:
    """Read the session metadata (id / cwd) from a Codex transcript."""
    meta: dict[str, Any] = {}
    for line in _iter_head_lines(transcript_path, 256 * 1024):
        entry = _loads(line)
        if not entry or entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload")
        if isinstance(payload, dict):
            meta = {
                "session_id": payload.get("session_id") or payload.get("id"),
                "cwd": payload.get("cwd"),
                "cli_version": payload.get("cli_version"),
                "source": payload.get("source"),
            }
        break
    return meta


def _event_timestamp(entry: dict) -> Optional[float]:
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_turn_outcome(
    transcript_path: str, now: Optional[float] = None, tail_bytes: int = 1024 * 1024
) -> dict:
    """Return the outcome of the most recent turn recorded in a transcript.

    The returned dict always contains ``outcome``; ``resumable`` is True only
    for a genuine usage-limit interruption.
    """
    result: dict[str, Any] = {
        "outcome": OUTCOME_UNKNOWN,
        "resumable": False,
        "error_info": None,
        "message": None,
        "reset_at": None,
        "turn_id": None,
        "at": None,
        "transcript_path": transcript_path,
        "transcript_mtime": None,
    }
    try:
        result["transcript_mtime"] = os.path.getmtime(transcript_path)
    except OSError:
        return result

    for line in reversed(_iter_tail_lines(transcript_path, tail_bytes)):
        entry = _loads(line)
        if not entry or entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind not in ("task_complete", "turn_aborted", "turn_failed"):
            continue

        result["turn_id"] = payload.get("turn_id") or payload.get("root_turn_id")
        result["at"] = _event_timestamp(entry)

        if kind == "turn_aborted":
            result["outcome"] = OUTCOME_ABORTED
            reason = payload.get("reason")
            if reason:
                result["message"] = str(reason)
            return result

        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            error_info = error.get("codex_error_info")
        elif isinstance(error, str):
            message, error_info = error, None
        else:
            message, error_info = None, None

        if error is None:
            result["outcome"] = OUTCOME_COMPLETED
            return result

        result["message"] = message
        result["error_info"] = error_info
        classified = classify_error(message, error_info)
        if classified == "quota":
            result["outcome"] = OUTCOME_QUOTA
            result["resumable"] = True
            # "try again at 7:51 AM" means the next 7:51 AM after the message
            # was written, not after the transcript happens to be read.
            result["reset_at"] = parse_retry_time(message or "", result.get("at") or now)
        elif classified == "quota_no_reset":
            result["outcome"] = OUTCOME_QUOTA_NO_RESET
        else:
            result["outcome"] = OUTCOME_ERRORED
        return result

    return result
