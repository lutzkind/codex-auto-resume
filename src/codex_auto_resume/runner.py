"""Launch the resume of one explicit Codex session and classify its outcome.

The session is always identified by its explicit UUID. ``--last`` is never
used anywhere in this project: another Codex session may start while a
recovery is waiting, and resuming the wrong one would be worse than doing
nothing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Optional

from . import codexenv, log, quota, state, sysinfo

RESULT_COMPLETED = "completed"
RESULT_QUOTA = "quota"
RESULT_ATTACHED = "attached"
RESULT_ERROR = "error"
RESULT_START_FAILED = "start_failed"

#: Codex refuses to resume a thread that a live TUI (or another client) still
#: has loaded. This is not a failure to retry immediately: the session is still
#: attached, and waiting is the correct behaviour. Captured from the installed
#: CLI on 2026-10-27 (0.164.0):
#: ``thread-store conflict: thread <uuid> already has an active writer``.
_ACTIVE_WRITER_RE = re.compile(r"already has an active writer", re.IGNORECASE)
#: Codex colorises its error labels; the message itself must stay matchable.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
#: Bound what is kept from a failed CLI invocation. The diagnostic is a Codex
#: error, never a prompt or transcript.
_MAX_DIAGNOSTIC_CHARS = 2000


def _redact(text: Optional[str], prompt: str) -> Optional[str]:
    """Keep a CLI diagnostic from echoing the continuation prompt into logs."""
    if not text:
        return text
    if prompt:
        return text.replace(prompt, "<prompt>")
    return text

#: Flags that are safe for unattended operation and never escalate permissions.
BASE_FLAGS = ("exec", "resume", "--json", "--skip-git-repo-check")


def detach_kwargs() -> dict:
    """Platform-appropriate "do not die with the caller" process flags.

    POSIX starts the child in its own session (``setsid``); Windows has no
    sessions to inherit, and a new process group keeps console control events
    from reaching the helper's own process group.
    """
    if sysinfo.is_windows():
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}


def _is_windows_batch(binary: str) -> bool:
    return sysinfo.is_windows_batch(binary)


def spawn_target(command: list[str]):
    """The exact executable/command line handed to the OS.

    A ``.cmd``/``.bat`` shim (which is what an npm-managed Codex installs on
    Windows) cannot be started by ``CreateProcess`` directly; it must go through
    ``cmd.exe /d /s /c`` with a properly quoted command line. An ``.exe`` (the
    documented standalone installer) is started directly, exactly as on POSIX.
    """
    return sysinfo.spawn_command(command)


def build_resume_command(
    codex_bin: str,
    session_id: str,
    prompt: str,
    model: Optional[str] = None,
) -> list[str]:
    command = [codex_bin, *BASE_FLAGS]
    if model:
        command += ["--model", model]
    command += [session_id, prompt]
    return command


def effective_model(recorded_model: Optional[str]) -> Optional[str]:
    """Pass the recorded model only when the user's default model changed."""
    if not recorded_model:
        return None
    current = codexenv.default_model()
    if current and current == recorded_model:
        return None
    return recorded_model


def _classify_event(event: dict) -> Optional[tuple[str, str]]:
    kind = event.get("type")
    if kind == "turn.completed":
        return RESULT_COMPLETED, ""
    if kind == "error":
        message = event.get("message") or ""
        if quota.classify_message(message) == "quota":
            return RESULT_QUOTA, message
        return None
    if kind == "turn.failed":
        error = event.get("error")
        message = ""
        if isinstance(error, dict):
            message = error.get("message") or ""
        elif isinstance(error, str):
            message = error
        if quota.classify_message(message) == "quota":
            return RESULT_QUOTA, message
        return RESULT_ERROR, message
    return None


def run_resume(
    codex_bin: str,
    session_id: str,
    prompt: str,
    cwd: Optional[str],
    model: Optional[str] = None,
    timeout: Optional[float] = None,
    extra_env: Optional[dict] = None,
    popen_kwargs: Optional[dict] = None,
) -> dict:
    """Run ``codex exec resume <SESSION_ID> <prompt>`` and classify the result.

    The child is started in its own session so it keeps running if the parent
    terminal disappears; the caller decides whether to wait for it.
    """
    command = build_resume_command(codex_bin, session_id, prompt, model)
    workdir = cwd if cwd and os.path.isdir(cwd) else None
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    log.log(
        "resume_launch",
        session_id=session_id,
        cwd=workdir,
        model=model,
        # Never log the full command line with the prompt: the prompt is fixed
        # boilerplate, but the session id and binary are what matter.
        argv0=command[0],
    )

    started = time.time()
    result: dict[str, Any] = {
        "result": RESULT_ERROR,
        "message": None,
        "exit_code": None,
        "started_at": started,
        "finished_at": None,
        "events": 0,
    }
    target = spawn_target(command)
    kwargs = dict(
        cwd=workdir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        **detach_kwargs(),
    )
    if popen_kwargs:
        kwargs.update(popen_kwargs)
    try:
        proc = subprocess.Popen(target, **kwargs)
    except OSError as error:
        result["message"] = f"failed to start codex: {error}"
        result["result"] = RESULT_START_FAILED
        result["finished_at"] = time.time()
        log.log("resume_start_failed", session_id=session_id, error=str(error))
        return result

    deadline = started + timeout if timeout else None
    classified: Optional[tuple[str, str]] = None
    tail: list[str] = []
    diagnostics: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        result["events"] += 1
        line = _ANSI_RE.sub("", line.rstrip("\n"))
        if not line:
            continue
        tail.append(line)
        if len(tail) > 20:
            tail.pop(0)
        try:
            event = json.loads(line)
        except ValueError:
            # The CLI writes startup/thread errors as plain text. Keep them:
            # discarding them made a failed resume look like an empty result.
            diagnostics.append(_redact(line, prompt) or line)
            if len(diagnostics) > 20:
                diagnostics.pop(0)
            continue
        if not isinstance(event, dict):
            continue
        found = _classify_event(event)
        if found and classified is None:
            classified = found
        if deadline and time.time() > deadline:
            break
    try:
        result["exit_code"] = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        sysinfo.terminate_tree(proc)
        try:
            result["exit_code"] = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            result["exit_code"] = None
    result["finished_at"] = time.time()

    if classified is not None:
        result["result"], result["message"] = classified
        result["message"] = _redact(result["message"], prompt)
    elif result["exit_code"] == 0:
        result["result"] = RESULT_COMPLETED
    for line in tail:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and not result["message"]:
                result["message"] = _redact(error.get("message"), prompt)

    diagnostic = " | ".join(diagnostics)[-_MAX_DIAGNOSTIC_CHARS:]
    result["diagnostic"] = diagnostic or None
    if result["result"] == RESULT_ERROR and diagnostic:
        if _ACTIVE_WRITER_RE.search(diagnostic):
            result["result"] = RESULT_ATTACHED
        if not result["message"]:
            # The last plain-text line is Codex's own error message.
            result["message"] = diagnostics[-1]
    log.log(
        "resume_finished",
        session_id=session_id,
        result=result["result"],
        exit_code=result["exit_code"],
        seconds=round(result["finished_at"] - started, 2),
        message=result["message"],
    )
    return result


def reconcile_with_transcript(
    outcome: dict,
    transcript_path: Optional[str],
    not_before: Optional[float] = None,
) -> dict:
    """Prefer the transcript's own record of how the resumed turn ended.

    This keeps the decision correct even when the plugin hooks are not trusted
    (so no hook ran during the resume) and when the process output is
    truncated. It must not, however, turn a failed launch into a quota result:
    if Codex refused to resume the thread (for example because a live TUI still
    owns it), the transcript still ends with the *original* interruption and
    has not changed. ``not_before`` (the resume's start time) makes that
    distinction.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return outcome
    if outcome.get("result") in (RESULT_ATTACHED, RESULT_START_FAILED):
        return outcome
    if not_before is not None:
        try:
            if os.path.getmtime(transcript_path) <= float(not_before):
                return outcome
        except OSError:
            return outcome
    recorded = quota.last_turn_outcome(transcript_path)
    if recorded["outcome"] == quota.OUTCOME_QUOTA:
        outcome = dict(outcome)
        outcome["result"] = RESULT_QUOTA
        outcome["message"] = recorded.get("message") or outcome.get("message")
        outcome["reset_at"] = recorded.get("reset_at")
        return outcome
    if recorded["outcome"] in (quota.OUTCOME_COMPLETED,):
        outcome = dict(outcome)
        outcome["result"] = RESULT_COMPLETED
        return outcome
    return outcome


def pending_resume_at(
    reset_at: Optional[float],
    attempts: int,
    now: Optional[float] = None,
    margin: float = state.RESET_MARGIN_SECONDS,
    minimum_delay: float = 0.0,
) -> float:
    """Compute when to try again, honouring an already-passed reset time.

    A passed reset is normally retried immediately (for example after a
    reboot). After a resume that itself reported quota, however, an immediate
    retry can only spin: ``minimum_delay`` gives that caller a bounded pause.
    """
    now = now if now is not None else time.time()
    if reset_at is None:
        return now + state.next_resume_delay(attempts)
    if reset_at <= now:
        return now + max(0.0, float(minimum_delay))
    return reset_at + margin
