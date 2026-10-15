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
MAX_ITERATIONS = 5000
#: When the user is still attached, re-check at most this often.
MIN_ATTACHED_RECHECK = 600.0
#: How often to look for a new usage-limit interruption while watching.
SCAN_INTERVAL_ATTACHED = 20.0
SCAN_INTERVAL_IDLE = 60.0
#: Stay alive this long after the last session's host process disappeared.
#: Quota errors are visible in the TUI while the client is alive, so this is a
#: safety margin rather than the primary window.
WATCH_RESIDUAL_SECONDS = 600.0
#: How many recently modified rollout files a single sweep will consider.
MAX_SWEEP_TRANSCRIPTS = 400
#: Ignore interruptions older than this; their reset has long passed and the
#: user has almost certainly moved on.
MAX_INTERRUPTION_AGE_SECONDS = 24 * 3600
#: A transcript written to this recently means Codex may still be running it.
TRANSCRIPT_IDLE_SECONDS = 300.0
#: Command-line markers of the shared local app-server daemon, which outlives
#: every client and therefore says nothing about whether a session is attached.
DAEMON_CMDLINE_MARKERS = ("app-server --listen", "--managed-daemon")


class Waiter:
    """Signal-aware sleeper so new state changes are noticed immediately."""

    def __init__(self) -> None:
        self._read, self._write = socket.socketpair()
        self._read.setblocking(False)
        self._write.setblocking(False)
        self._installed = False
        self.interrupted = False
        self.woken = False

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
        self.woken = True

    def consume_wake(self) -> bool:
        """Return True once for each batch of signals received."""
        woke = self.woken
        self.woken = False
        return woke

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
        """Sleep until ``deadline``, returning early when a signal arrives.

        Returning early lets the caller re-read state and look for other work
        (for example a different session that just hit its quota) without
        polling.
        """
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
            if self.consume_wake():
                return

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


def is_shared_daemon(pid: int) -> bool:
    """True when ``pid`` is the shared app-server daemon, not a session host.

    An interactive TUI runs its thread on the shared local app-server daemon,
    so the hook payload's parent process is the daemon. It stays alive after the
    user quits the client, so it can never be used as an "is this session still
    attached" signal.
    """
    cmdline = paths.process_cmdline(pid)
    return any(marker in cmdline for marker in DAEMON_CMDLINE_MARKERS)


def transcript_is_idle(pending: dict, now: float) -> bool:
    transcript = pending.get("transcript_path")
    if not transcript:
        return True
    try:
        mtime = os.path.getmtime(transcript)
    except OSError:
        return True
    return now - mtime >= TRANSCRIPT_IDLE_SECONDS


def session_detached(pending: dict, session: Optional[dict], settings: dict, now: float) -> tuple[bool, str]:
    """Decide whether it is safe to resume, i.e. nobody is attached.

    Resuming while the user still has the session open in a TUI would create
    two live hosts for the same session, so it is only done when the session
    ended, its host process is gone, or the session has gone quiet.
    """
    if settings.get("resume_when_attached"):
        return True, "resume_when_attached"
    if session is None:
        # The sweep can find a session that never registered through a hook
        # (for example because hooks were not trusted yet). Without a host
        # process, fall back to whether Codex is still writing the transcript.
        if transcript_is_idle(pending, now):
            return True, "no_session_registry_entry"
        return False, "transcript_active"
    if session.get("attached") is False:
        return True, "session_end_observed"
    pid = session.get("host_pid")
    if isinstance(pid, int) and pid > 0:
        if not paths.process_alive(pid, session.get("host_pid_start")):
            return True, "host_process_gone"
        if is_shared_daemon(pid):
            if transcript_is_idle(pending, now):
                return True, "daemon_hosted_transcript_idle"
            return False, "daemon_hosted_transcript_active"
        return False, "session_attached"
    if transcript_is_idle(pending, now):
        return True, "no_host_process_recorded"
    return False, "transcript_active"


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


# --------------------------------------------------------------------------- #
# Detection sweep
#
# Codex does not run a hook when a turn fails, and an interactive TUI session
# lives on the shared app-server daemon, so quitting the client does not fire
# SessionEnd either. The hook payloads therefore cannot be the only trigger:
# the helper also looks at Codex's own session data for the usage-limit
# signature. Only transcripts whose last turn ended in a usage limit are
# candidates, and an already-armed, cancelled, completed or too-old session is
# skipped.
# --------------------------------------------------------------------------- #


def recent_rollouts(now: float, limit: int = MAX_SWEEP_TRANSCRIPTS) -> list[tuple[str, float]]:
    """Recently modified Codex transcripts, newest first."""
    root = paths.codex_home() / "sessions"
    if not root.is_dir():
        return []
    found: list[tuple[str, float]] = []
    try:
        for path in root.glob("**/rollout-*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime > MAX_INTERRUPTION_AGE_SECONDS:
                continue
            found.append((str(path), mtime))
    except OSError:
        return []
    found.sort(key=lambda item: item[1], reverse=True)
    return found[:limit]


def discover_candidates(now: float) -> list[dict]:
    """Sessions worth inspecting: registered ones plus recent transcripts."""
    candidates: dict[str, dict] = {}
    for record in state.iter_sessions():
        session_id = record.get("session_id")
        transcript = record.get("transcript_path")
        if not session_id or not transcript:
            continue
        candidates[session_id] = {
            "session_id": session_id,
            "transcript_path": transcript,
            "session": record,
        }
    for path, mtime in recent_rollouts(now):
        meta = quota.session_meta(path)
        session_id = meta.get("session_id")
        if not session_id or session_id in candidates:
            continue
        candidates[session_id] = {
            "session_id": session_id,
            "transcript_path": path,
            "session": None,
            "cwd": meta.get("cwd"),
        }
    return list(candidates.values())


def arm_from_candidate(candidate: dict, outcome: dict, now: float) -> Optional[dict]:
    """Create a pending recovery for a transcript that ended on a quota error."""
    session_id = candidate["session_id"]
    interruption_at = outcome.get("at")
    if state.is_cancelled(session_id, interruption_at):
        log.log("sweep_skip_cancelled", session_id=session_id)
        return None
    if interruption_at and now - float(interruption_at) > MAX_INTERRUPTION_AGE_SECONDS:
        log.log("sweep_skip_stale", session_id=session_id, at=interruption_at)
        return None

    session = candidate.get("session") or state.read_session(session_id) or {}
    cwd = session.get("cwd") or candidate.get("cwd")
    record = state.new_pending(
        session_id,
        cwd=cwd,
        model=session.get("model"),
        permission_mode=session.get("permission_mode"),
        transcript_path=candidate["transcript_path"],
        message=outcome.get("message"),
        reset_at=outcome.get("reset_at"),
        reset_source="transcript_message" if outcome.get("reset_at") else "unknown",
        interruption_at=interruption_at,
        now=now,
    )
    if outcome["outcome"] == quota.OUTCOME_QUOTA_NO_RESET:
        record["state"] = state.STATE_NOT_RESUMABLE
    state.write_pending(record)
    log.log(
        "sweep_armed",
        session_id=session_id,
        state=record["state"],
        reset_at=record["reset_at"],
        resume_at=record["resume_at"],
        reset_source=record["reset_source"],
    )
    return record


def sweep(now: Optional[float] = None) -> int:
    """Arm a recovery for every session whose last turn hit a usage limit."""
    now = now if now is not None else time.time()
    armed = 0
    for candidate in discover_candidates(now):
        session_id = candidate["session_id"]
        if state.read_pending(session_id):
            continue
        outcome = quota.last_turn_outcome(candidate["transcript_path"], now=now)
        if outcome["outcome"] not in (quota.OUTCOME_QUOTA, quota.OUTCOME_QUOTA_NO_RESET):
            continue
        if arm_from_candidate(candidate, outcome, now) is not None:
            armed += 1
    return armed


def any_session_attached() -> bool:
    """True while any registered session still has a live Codex process."""
    for record in state.iter_sessions():
        pid = record.get("host_pid")
        if record.get("attached") is False:
            continue
        if isinstance(pid, int) and pid > 0 and paths.process_alive(pid, record.get("host_pid_start")):
            return True
    return False


def wait_for_resume(pending: dict, waiter: Waiter, shutdown: Shutdown) -> Optional[dict]:
    """Sleep until this record's resume time, re-checking on every wake.

    The record is re-read after each wake so a cancellation or a change made
    while waiting is honoured, and each wake also runs a detection sweep so a
    second session that hits its quota is picked up while we wait.
    """
    session_id = pending["session_id"]
    logged_for = None
    while True:
        current = state.read_pending(session_id)
        if current is None or current.get("state") not in state.ACTIVE_STATES:
            return None
        pending = current
        if record_is_cancelled(pending):
            log.log("helper_skip_cancelled_after_wait", session_id=session_id)
            state.clear_pending(session_id, "cancelled")
            state.clear_launching(session_id)
            return None
        resume_at = float(pending.get("resume_at") or time.time())
        now = time.time()
        if resume_at <= now:
            return pending
        if logged_for != resume_at:
            log.log(
                "helper_waiting",
                session_id=session_id,
                resume_at=resume_at,
                reset_at=pending.get("reset_at"),
                reset_source=pending.get("reset_source"),
                seconds=round(resume_at - now, 1),
            )
            logged_for = resume_at
        waiter.sleep_until(resume_at, should_stop=lambda: shutdown.requested)
        if shutdown.requested:
            return None
        if waiter.consume_wake():
            try:
                sweep()
            except Exception as error:  # noqa: BLE001 - a sweep must never kill the wait
                log.log("sweep_error", error=f"{type(error).__name__}: {error}")


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
        # when the next hook event (or the watch sweep) wakes us.
        log.log("helper_session_attached", session_id=session_id, reason=reason)
        pending["resume_at"] = max(
            float(pending.get("resume_at") or 0), time.time() + MIN_ATTACHED_RECHECK
        )
        state.write_pending(pending)
        return False

    pending = wait_for_resume(pending, waiter, shutdown)
    if pending is None:
        return False

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
        # Prefer the authoritative window reset from Codex over the reset time
        # printed in an older error message: the backend recomputes the window,
        # so the transcript value can already be stale.
        reset_at = None
        snapshot = ratelimit.query(codex_bin, codex_home=str(paths.codex_home()))
        verdict = ratelimit.availability(snapshot, time.time())
        if verdict["next_reset_at"]:
            reset_at = verdict["next_reset_at"]
            pending["reset_source"] = "app_server"
        if reset_at is None:
            reset_at = outcome.get("reset_at")
            pending["reset_source"] = "transcript_message" if reset_at else "unknown"
        pending["reset_at"] = reset_at
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
            reset_source=pending["reset_source"],
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
    """Publish the helper pid so hooks can wake it and status can report it."""
    pid = os.getpid()
    try:
        paths.atomic_write_json(
            paths.helper_pid_file(),
            {
                "version": state.SCHEMA_VERSION,
                "pid": pid,
                "pid_start": paths.process_start_time(pid),
                "started_at": time.time(),
                "cmdline": "codex_auto_resume helper",
            },
        )
    except OSError:
        return


def notify_helper() -> bool:
    """Wake a running helper so it re-reads state immediately."""
    record = paths.read_json(paths.helper_pid_file()) or {}
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not paths.process_alive(pid, record.get("pid_start")):
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
        watch_until = time.time() + WATCH_RESIDUAL_SECONDS
        try:
            while iterations < MAX_ITERATIONS:
                iterations += 1
                if shutdown.requested:
                    log.log("helper_shutdown")
                    break
                if not state.is_enabled():
                    log.log("helper_stopped_disabled")
                    break
                try:
                    armed = sweep()
                    if armed:
                        log.log("helper_sweep_armed", count=armed)
                except Exception as error:  # noqa: BLE001 - never kill the waiter
                    log.log("sweep_error", error=f"{type(error).__name__}: {error}")

                if state.active_pending():
                    if not process_once(codex_bin, waiter, shutdown):
                        if shutdown.requested:
                            break
                        # No progress is possible right now (for example the
                        # user is still attached to the session). Re-check after
                        # a bounded interval, or immediately on a hook signal.
                        waiter.sleep_until(
                            time.time() + MIN_ATTACHED_RECHECK,
                            should_stop=lambda: shutdown.requested,
                        )
                        if waiter.consume_wake():
                            watch_until = max(
                                watch_until, time.time() + WATCH_RESIDUAL_SECONDS
                            )
                    continue

                # Nothing to resume yet. Stay alive while any Codex session is
                # open, plus a residual window, so the quota error that follows
                # the user's prompt is noticed even though Codex fires no hook
                # for a failed turn.
                now = time.time()
                attached = any_session_attached()
                if attached:
                    watch_until = now + WATCH_RESIDUAL_SECONDS
                elif now >= watch_until:
                    log.log("helper_idle_exit", iterations=iterations)
                    break
                interval = SCAN_INTERVAL_ATTACHED if attached else SCAN_INTERVAL_IDLE
                target = min(now + interval, watch_until)
                waiter.sleep_until(target, should_stop=lambda: shutdown.requested)
                if waiter.consume_wake():
                    watch_until = max(watch_until, time.time() + WATCH_RESIDUAL_SECONDS)
        finally:
            waiter.close()
            shutdown.restore()
            paths.remove_file(paths.helper_pid_file())
            log.log("helper_exited", iterations=iterations)
    return 0
