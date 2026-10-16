"""Hook entry points invoked by Codex through the bundled plugin hooks.

Every handler reads one JSON object on stdin (Codex's hook payload), performs a
bounded amount of work, and exits 0. A hook must never fail a Codex turn, so all
errors are swallowed and written to the helper log instead.

Verified payloads (Codex CLI 0.162.1):

``SessionStart``
    ``{"session_id","transcript_path","cwd","hook_event_name","model",
    "permission_mode","source"}``

``UserPromptSubmit``
    as above plus ``turn_id`` and ``prompt``

``SessionEnd``
    ``{"session_id","transcript_path","cwd","hook_event_name","reason"}``

``Stop``
    ``{"session_id","turn_id","transcript_path","cwd","hook_event_name",
    "model","permission_mode","stop_hook_active","last_assistant_message"}``
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Optional

from . import log, paths, quota, state, sysinfo

#: Kept for backwards compatibility with the systemd-era log lines.
HELPER_UNIT = "codex-auto-resume-helper.service"
#: Codex clamps SessionEnd/Interrupt hook timeouts, so keep the work tiny.
FAST_EVENTS = {"SessionEnd", "Interrupt"}
FAST_DEADLINE_SECONDS = 2.5
DEFAULT_DEADLINE_SECONDS = 8.0


class _Timeout(Exception):
    pass


#: POSIX uses an interval timer + SIGALRM; Windows has no SIGALRM, so a
#: watchdog thread ends the hook process instead. Either way the hook exits 0
#: and never blocks a Codex turn.
_WATCHDOG: Optional[object] = None


def _install_alarm(seconds: float) -> None:
    """Bound this hook's runtime, however the platform can."""
    global _WATCHDOG
    if sysinfo.is_windows():
        import threading

        cancelled = threading.Event()

        def _watchdog() -> None:
            if cancelled.wait(seconds):
                return
            log.log("hook_deadline_reached", seconds=seconds)
            os._exit(0)

        watchdog = threading.Thread(target=_watchdog, name="hook-deadline", daemon=True)
        watchdog.start()
        _WATCHDOG = cancelled
        return

    def handler(signum, frame):  # noqa: ANN001 - signal handler
        del signum, frame
        raise _Timeout()

    try:
        signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except (AttributeError, OSError, ValueError):
        pass


def _clear_alarm() -> None:
    global _WATCHDOG
    if _WATCHDOG is not None:
        _WATCHDOG.set()  # type: ignore[attr-defined]
        _WATCHDOG = None
        return
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except (AttributeError, OSError, ValueError):
        pass


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return {}
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _ensure_helper_running() -> None:
    """Wake the helper, or ask the platform backend to start it.

    The hook must never block a Codex turn: every failure is swallowed (and
    logged) so Codex is unaffected, while a failed start is still diagnosable
    through ``codex-auto-resume logs``/``doctor``.
    """
    from . import helper

    if helper.notify_helper():
        return
    try:
        from . import service

        service.start_helper()
        return
    except Exception as error:  # noqa: BLE001 - a hook must never fail Codex
        log.log("helper_start_failed", error=f"{type(error).__name__}: {error}")
    # Last resort for older Linux installs where the module is present but the
    # backend raised: the systemctl shape that was verified before.
    if not sysinfo.is_windows():
        try:
            subprocess.Popen(
                ["systemctl", "--user", "start", "--no-block", HELPER_UNIT],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return


def _session_record(payload: dict, attached: bool, event: str) -> dict:
    session_id = str(payload.get("session_id") or "")
    pid = sysinfo.host_pid()
    record = state.read_session(session_id) or {}
    record.update(
        {
            "session_id": session_id,
            "cwd": payload.get("cwd"),
            "transcript_path": payload.get("transcript_path"),
            "model": payload.get("model") or record.get("model"),
            "permission_mode": payload.get("permission_mode") or record.get("permission_mode"),
            "attached": attached,
            "last_event": event,
            "last_event_at": time.time(),
            "host_pid": pid if attached else record.get("host_pid"),
            "host_pid_start": (
                paths.process_start_time(pid) if attached else record.get("host_pid_start")
            ),
        }
    )
    if event == "SessionEnd":
        record["ended_at"] = time.time()
        record["end_reason"] = payload.get("reason")
    return record


def handle_session_start(payload: dict) -> None:
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    record = _session_record(payload, attached=True, event="SessionStart")
    record["source"] = payload.get("source")
    state.write_session(session_id, record)

    pending = state.read_pending(session_id)
    launching = state.read_launching(session_id)
    if pending and (launching or pending.get("state") == state.STATE_RESUMING):
        # Our own resume is starting this session again.
        log.log("hook_session_start_own_resume", session_id=session_id)
        return
    if pending and payload.get("source") in ("resume", "startup"):
        log.log(
            "hook_session_start_superseded",
            session_id=session_id,
            detail="session started again outside of codex-auto-resume",
        )
        state.clear_pending(session_id, "superseded")
        state.clear_launching(session_id)
    _ensure_helper_running()


def handle_user_prompt_submit(payload: dict) -> None:
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    record = _session_record(payload, attached=True, event="UserPromptSubmit")
    record["last_turn_id"] = payload.get("turn_id")
    state.write_session(session_id, record)


def handle_stop(payload: dict) -> None:
    """A turn finished normally: nothing is waiting to be resumed."""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    record = _session_record(payload, attached=True, event="Stop")
    record["last_turn_id"] = payload.get("turn_id")
    state.write_session(session_id, record)
    pending = state.read_pending(session_id)
    if pending and pending.get("state") == state.STATE_WAITING:
        log.log("hook_stop_clears_pending", session_id=session_id)
        state.clear_pending(session_id, state.STATE_COMPLETED)
        state.clear_launching(session_id)


def _arm_recovery(payload: dict, session_id: str, transcript: Optional[str]) -> None:
    outcome = quota.last_turn_outcome(transcript) if transcript else {"outcome": quota.OUTCOME_UNKNOWN}
    outcome_kind = outcome.get("outcome")
    log.log(
        "hook_session_end_outcome",
        session_id=session_id,
        outcome=outcome_kind,
        error_info=outcome.get("error_info"),
        reset_at=outcome.get("reset_at"),
    )
    if outcome_kind not in (quota.OUTCOME_QUOTA, quota.OUTCOME_QUOTA_NO_RESET):
        if state.read_pending(session_id):
            state.clear_pending(session_id, state.STATE_COMPLETED)
        return

    interruption_at = outcome.get("at")
    if state.is_cancelled(session_id, interruption_at):
        log.log("hook_session_end_cancelled", session_id=session_id)
        return

    existing = state.read_pending(session_id)
    if existing and existing.get("interruption_at") == interruption_at:
        if existing.get("state") in state.ACTIVE_STATES:
            log.log("hook_session_end_duplicate", session_id=session_id)
            _ensure_helper_running()
            return

    reset_at = outcome.get("reset_at")
    session = state.read_session(session_id) or {}
    record = state.new_pending(
        session_id,
        cwd=payload.get("cwd") or session.get("cwd"),
        model=session.get("model"),
        permission_mode=session.get("permission_mode"),
        transcript_path=transcript,
        message=outcome.get("message"),
        reset_at=reset_at,
        reset_source="transcript_message" if reset_at else "unknown",
        interruption_at=interruption_at,
    )
    if outcome_kind == quota.OUTCOME_QUOTA_NO_RESET:
        record["state"] = state.STATE_NOT_RESUMABLE
        record["quota_message"] = outcome.get("message")
    state.write_pending(record)
    log.log(
        "hook_recovery_armed",
        session_id=session_id,
        state=record["state"],
        reset_at=record["reset_at"],
        resume_at=record["resume_at"],
        reset_source=record["reset_source"],
    )
    if record["state"] == state.STATE_WAITING:
        _ensure_helper_running()


def handle_session_end(payload: dict) -> None:
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    record = _session_record(payload, attached=False, event="SessionEnd")
    state.write_session(session_id, record)
    _arm_recovery(payload, session_id, payload.get("transcript_path"))


def handle_interrupt(payload: dict) -> None:
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        return
    record = _session_record(payload, attached=True, event="Interrupt")
    state.write_session(session_id, record)
    if state.read_pending(session_id):
        log.log("hook_interrupt_cancels_pending", session_id=session_id)
        state.clear_pending(session_id, "user_interrupt")


HANDLERS = {
    "SessionStart": handle_session_start,
    "UserPromptSubmit": handle_user_prompt_submit,
    "Stop": handle_stop,
    "SessionEnd": handle_session_end,
    "Interrupt": handle_interrupt,
}


def main(event: str, argv: Optional[list[str]] = None) -> int:
    del argv
    handler = HANDLERS.get(event)
    if handler is None:
        return 0
    deadline = FAST_DEADLINE_SECONDS if event in FAST_EVENTS else DEFAULT_DEADLINE_SECONDS
    _install_alarm(deadline)
    try:
        paths.ensure_dirs()
        payload = read_payload()
        handler(payload)
    except _Timeout:
        log.log("hook_timeout", hook_event=event)
    except Exception as error:  # noqa: BLE001 - hooks must never fail Codex
        log.log("hook_error", hook_event=event, error=f"{type(error).__name__}: {error}")
    finally:
        _clear_alarm()
    return 0
