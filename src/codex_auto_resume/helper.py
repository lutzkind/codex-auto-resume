"""The out-of-process worker that waits for a quota reset and resumes a session.

One process, one advisory lock, no polling loop: when Codex reported an exact
reset time the helper sleeps until it (a long ``select`` that only wakes early
on a signal or once an hour as a safety net). When no reset time is known it
falls back to a bounded exponential backoff.
"""

from __future__ import annotations

import os
import select
import signal
import socket
import time
from typing import Optional

from . import log, paths, quota, ratelimit, runner, state

MAX_SLEEP_SLICE = 3600.0
MAX_ATTEMPTS = 100
MAX_ITERATIONS = 500
#: When the user is still attached, re-check at most this often.
MIN_ATTACHED_RECHECK = 600.0


class Waiter:
    """Signal-aware sleeper so new state changes are noticed immediately."""

    def __init__(self) -> None:
        self._read, self._write = socket.socketpair()
        self._read.setblocking(False)
        self._write.setblocking(False)
        self._installed = False
        self.interrupted = False

    def install(self) -> None:
        try:
            signal.set_wakeup_fd(self._write.fileno())
            for signum in (signal.SIGUSR1, signal.SIGUSR2):
                signal.signal(signum, self._handler)
            self._installed = True
        except (OSError, ValueError):
            self._installed = False

    def _handler(self, signum, frame) -> None:  # noqa: ANN001 - signal handler
        del signum, frame

    def drain(self) -> None:
        while True:
            try:
                if not self._read.recv(4096):
                    return
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return

    def sleep_until(self, deadline: float, should_stop=None) -> None:
        """Sleep until ``deadline``, waking early on a signal."""
        while True:
            if should_stop is not None and should_stop():
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            slice_seconds = min(remaining, MAX_SLEEP_SLICE)
            try:
                select.select([self._read], [], [], slice_seconds)
            except (OSError, ValueError):
                time.sleep(slice_seconds)
            self.drain()

    def close(self) -> None:
        if self._installed:
            try:
                signal.set_wakeup_fd(-1)
            except (OSError, ValueError):
                pass
        for sock in (self._read, self._write):
            try:
                sock.close()
            except OSError:
                pass


class Shutdown:
    def __init__(self) -> None:
        self.requested = False
        self._previous = {}

    def install(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                self._previous[signum] = signal.signal(signum, self._handler)
            except (OSError, ValueError):
                continue

    def _handler(self, signum, frame) -> None:  # noqa: ANN001 - signal handler
        del signum, frame
        self.requested = True

    def restore(self) -> None:
        for signum, handler in self._previous.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                continue


def session_detached(pending: dict, session: Optional[dict], settings: dict, now: float) -> tuple[bool, str]:
    """Decide whether it is safe to resume, i.e. nobody is attached.

    Resuming while the user still has the session open in a TUI would create
    two live hosts for the same session, so it is only done when the session
    ended or its host process is gone.
    """
    del pending, now
    if settings.get("resume_when_attached"):
        return True, "resume_when_attached"
    if session is None:
        return True, "no_session_registry_entry"
    if session.get("attached") is False:
        return True, "session_end_observed"
    pid = session.get("host_pid")
    if isinstance(pid, int) and pid > 0:
        if not paths.process_alive(pid, session.get("host_pid_start")):
            return True, "host_process_gone"
        return False, "session_attached"
    return True, "no_host_process_recorded"


def record_is_cancelled(pending: dict) -> bool:
    return state.is_cancelled(pending["session_id"], pending.get("interruption_at"))


def _ambiguous_resume(pending: dict) -> Optional[str]:
    """Handle a record left in ``resuming`` by a crash."""
    transcript = pending.get("transcript_path")
    if transcript and os.path.exists(transcript):
        outcome = quota.last_turn_outcome(transcript)
        if outcome["outcome"] == quota.OUTCOME_QUOTA:
            return "quota"
        if outcome["outcome"] == quota.OUTCOME_COMPLETED:
            return "completed"
    return None


def process_once(codex_bin: Optional[str], waiter: Waiter, shutdown: Shutdown) -> bool:
    """Handle the earliest pending recovery. Returns True if work was done."""
    pending_records = state.active_pending()
    if not pending_records:
        return False

    pending = pending_records[0]
    session_id = pending["session_id"]
    settings = state.load_settings()

    if not settings.get("enabled", True):
        log.log("helper_disabled", session_id=session_id)
        return False

    session = state.read_session(session_id)

    if record_is_cancelled(pending):
        log.log("helper_skip_cancelled", session_id=session_id)
        state.clear_pending(session_id, "cancelled")
        state.clear_launching(session_id)
        return True

    if pending.get("state") == state.STATE_RESUMING and not pending.get("last_result"):
        verdict = _ambiguous_resume(pending)
        if verdict == "completed":
            log.log("helper_ambiguous_completed", session_id=session_id)
            state.clear_pending(session_id, state.STATE_COMPLETED)
            state.clear_launching(session_id)
            return True
        if verdict != "quota":
            log.log(
                "helper_ambiguous_failed",
                session_id=session_id,
                detail="helper restarted during a resume; refusing to replay it",
            )
            pending["state"] = state.STATE_FAILED
            pending["last_result"] = "ambiguous_restart"
            state.write_pending(pending)
            return True
        pending["state"] = state.STATE_WAITING
        state.write_pending(pending)

    detached, reason = session_detached(pending, session, settings, time.time())
    if not detached:
        # The user is still attached: leave the recovery armed and try again
        # when the next hook event (or the safety net) wakes us.
        log.log("helper_session_attached", session_id=session_id, reason=reason)
        pending["resume_at"] = max(
            float(pending.get("resume_at") or 0), time.time() + MIN_ATTACHED_RECHECK
        )
        state.write_pending(pending)
        return False

    resume_at = float(pending.get("resume_at") or time.time())
    now = time.time()
    if resume_at > now:
        log.log(
            "helper_waiting",
            session_id=session_id,
            resume_at=resume_at,
            reset_at=pending.get("reset_at"),
            reset_source=pending.get("reset_source"),
            seconds=round(resume_at - now, 1),
        )
        waiter.sleep_until(resume_at, should_stop=lambda: shutdown.requested)
        if shutdown.requested:
            return False
        # Re-read: the record may have been cancelled or changed while asleep.
        pending = state.read_pending(session_id) or pending
        if pending.get("state") not in state.ACTIVE_STATES:
            return True
        if record_is_cancelled(pending):
            log.log("helper_skip_cancelled_after_wait", session_id=session_id)
            state.clear_pending(session_id, "cancelled")
            state.clear_launching(session_id)
            return True

    if not codex_bin:
        log.log("helper_no_codex_binary", session_id=session_id)
        return False

    now = time.time()
    snapshot = ratelimit.query(codex_bin, codex_home=str(paths.codex_home()))
    verdict = ratelimit.availability(snapshot, now)
    log.log(
        "helper_quota_check",
        session_id=session_id,
        allowed=verdict["allowed"],
        reason=verdict["reason"],
        next_reset_at=verdict["next_reset_at"],
        plan_type=(snapshot or {}).get("plan_type"),
    )

    if verdict["allowed"] is False and verdict["next_reset_at"]:
        pending["reset_at"] = verdict["next_reset_at"]
        pending["reset_source"] = "app_server"
        pending["resume_at"] = verdict["next_reset_at"] + float(
            settings.get("reset_margin_seconds", state.RESET_MARGIN_SECONDS)
        )
        state.write_pending(pending)
        log.log(
            "helper_rescheduled",
            session_id=session_id,
            reset_at=pending["reset_at"],
            resume_at=pending["resume_at"],
        )
        return True

    if verdict["allowed"] is False and not verdict["next_reset_at"]:
        attempts = int(pending.get("attempts") or 0)
        if attempts >= MAX_ATTEMPTS:
            pending["state"] = state.STATE_FAILED
            pending["last_result"] = "max_attempts"
            state.write_pending(pending)
            log.log("helper_giving_up", session_id=session_id, attempts=attempts)
            return True
        pending["attempts"] = attempts + 1
        pending["resume_at"] = time.time() + state.next_resume_delay(attempts)
        state.write_pending(pending)
        log.log(
            "helper_backoff",
            session_id=session_id,
            attempts=pending["attempts"],
            resume_at=pending["resume_at"],
        )
        return True

    # Quota looks available (or cannot be determined): try the resume.
    return _do_resume(codex_bin, pending, settings, waiter)


MIN_ATTACHED_RECHECK = 600.0


def _do_resume(codex_bin: str, pending: dict, settings: dict, waiter: Waiter) -> bool:
    session_id = pending["session_id"]
    prompt = str(settings.get("continue_prompt") or state.DEFAULT_SETTINGS["continue_prompt"])
    transcript = pending.get("transcript_path")

    # If the user already made progress since the interruption, do not resume.
    if transcript and os.path.exists(transcript):
        outcome = quota.last_turn_outcome(transcript)
        mtime = outcome.get("transcript_mtime")
        interruption_at = pending.get("interruption_at")
        if (
            outcome["outcome"] == quota.OUTCOME_COMPLETED
            and mtime
            and interruption_at
            and mtime > float(interruption_at) + 1
        ):
            log.log("helper_superseded", session_id=session_id, transcript_mtime=mtime)
            state.clear_pending(session_id, "superseded")
            state.clear_launching(session_id)
            return True

    state.mark_launching(session_id, {"resume_at": pending.get("resume_at")})
    pending["state"] = state.STATE_RESUMING
    pending["attempts"] = int(pending.get("attempts") or 0) + 1
    pending["last_attempt_at"] = time.time()
    state.write_pending(pending)

    outcome = runner.run_resume(
        codex_bin=codex_bin,
        session_id=session_id,
        prompt=prompt,
        cwd=pending.get("cwd"),
        model=runner.effective_model(pending.get("model")),
    )
    outcome = runner.reconcile_with_transcript(outcome, transcript)
    state.clear_launching(session_id)

    pending = state.read_pending(session_id) or pending
    pending["state"] = state.STATE_WAITING
    pending["last_result"] = outcome.get("result")
    pending["last_message"] = outcome.get("message")
    pending["last_finished_at"] = outcome.get("finished_at")

    result = outcome.get("result")
    if result == runner.RESULT_COMPLETED:
        log.log("helper_completed", session_id=session_id)
        state.clear_pending(session_id, state.STATE_COMPLETED)
        _mark_session(session_id, completed=True)
        return True
    if result == runner.RESULT_QUOTA:
        reset_at = outcome.get("reset_at")
        if reset_at is None:
            snapshot = ratelimit.query(codex_bin, codex_home=str(paths.codex_home()))
            verdict = ratelimit.availability(snapshot, time.time())
            if verdict["next_reset_at"]:
                reset_at = verdict["next_reset_at"]
                pending["reset_source"] = "app_server"
        pending["reset_at"] = reset_at
        if reset_at is None:
            pending["reset_source"] = "unknown"
        pending["resume_at"] = runner.pending_resume_at(
            reset_at,
            int(pending.get("attempts") or 0),
            margin=float(settings.get("reset_margin_seconds", state.RESET_MARGIN_SECONDS)),
        )
        state.write_pending(pending)
        log.log(
            "helper_requeued",
            session_id=session_id,
            reset_at=reset_at,
            resume_at=pending["resume_at"],
        )
        return True

    log.log("helper_resume_failed", session_id=session_id, result=result)
    if result == runner.RESULT_START_FAILED:
        pending["attempts"] = int(pending.get("attempts") or 0)
        pending["resume_at"] = time.time() + state.next_resume_delay(
            int(pending.get("attempts") or 0)
        )
        state.write_pending(pending)
        return True
    pending["state"] = state.STATE_FAILED
    state.write_pending(pending)
    return True


def _mark_session(session_id: str, completed: bool) -> None:
    session = state.read_session(session_id)
    if not session:
        return
    session["last_recovery"] = "completed" if completed else "unknown"
    session["recovery_finished_at"] = time.time()
    state.write_session(session_id, session)


def write_pid() -> None:
    try:
        paths.atomic_write_text(paths.helper_pid_file(), f"{os.getpid()}\n")
    except OSError:
        return


def notify_helper() -> bool:
    """Wake a running helper so it re-reads state immediately."""
    record = paths.read_json(paths.helper_pid_file()) or {}
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    text = paths.process_cmdline(pid)
    if "codex_auto_resume" not in text and "codex-auto-resume" not in text:
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError:
        return False
    return True


def main(codex_bin: Optional[str] = None) -> int:
    paths.ensure_dirs()
    with paths.file_lock(paths.lock_file(), blocking=False) as acquired:
        if not acquired:
            log.log("helper_already_running")
            return 0
        waiter = Waiter()
        waiter.install()
        shutdown = Shutdown()
        shutdown.install()
        write_pid()
        log.log("helper_started", pid=os.getpid(), codex_bin=codex_bin)
        iterations = 0
        try:
            while iterations < MAX_ITERATIONS:
                iterations += 1
                if shutdown.requested:
                    log.log("helper_shutdown")
                    break
                if not state.is_enabled():
                    log.log("helper_stopped_disabled")
                    break
                if not state.active_pending():
                    log.log("helper_idle_exit")
                    break
                if not process_once(codex_bin, waiter, shutdown):
                    # Nothing actionable: wait for a hook to wake us, or the
                    # safety net, then look again once.
                    if not state.active_pending():
                        break
                    waiter.sleep_until(time.time() + MIN_ATTACHED_RECHECK)
                    if shutdown.requested:
                        break
        finally:
            waiter.close()
            shutdown.restore()
            paths.remove_file(paths.helper_pid_file())
            log.log("helper_exited", iterations=iterations)
    return 0
