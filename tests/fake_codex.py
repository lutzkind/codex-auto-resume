#!/usr/bin/env python3
"""A deterministic fake Codex CLI for the codex-auto-resume test suite.

It implements exactly the three surfaces the tool depends on, so the whole
wait/resume lifecycle can be exercised without spending real quota:

``fake_codex app-server --stdio``
    JSON-RPC: ``initialize``, ``initialized``, ``account/rateLimits/read``,
    ``hooks/list``, ``config/value/write``.

``fake_codex exec resume --json <SESSION_ID> <PROMPT>``
    Appends a turn outcome to the transcript named in the scenario file and
    prints the JSONL events the real CLI prints.

``fake_codex --version``

The scenario file is ``$FAKE_CODEX_SCENARIO`` (JSON):

.. code-block:: json

    {
      "transcript": "/tmp/rollout.jsonl",
      "resume_outcome": "completed",      # or "quota" / "error" / "aborted"
      "quota_message": "You’ve hit your usage limit. ...",
      "reset_epoch": 1792050694,
      "ordinary_usage_allowed": false,
      "windows": [{"slot": "primary", "used_percent": 100, "resets_at": 1792050694}],
      "resume_count_file": "/tmp/count",
      "argv_log": "/tmp/argv.jsonl"
    }
"""

from __future__ import annotations

import json
import os
import sys
import time

SCENARIO_PATH = os.environ.get("FAKE_CODEX_SCENARIO", "")


def scenario() -> dict:
    if not SCENARIO_PATH or not os.path.exists(SCENARIO_PATH):
        return {}
    with open(SCENARIO_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def default_quota_message(epoch: float) -> str:
    moment = time.localtime(epoch)
    stamp = time.strftime("%-I:%M %p", moment)
    return (
        "You\u2019ve hit your usage limit. Upgrade to Pro "
        "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/usage "
        f"to purchase more credits or try again at {stamp}."
    )


def append_task_complete(
    transcript: str,
    outcome: str,
    error_info: str | None,
    message: str | None,
    turn_id: str = "fake-turn",
) -> None:
    payload = {
        "type": "task_complete",
        "turn_id": turn_id,
        "started_at": int(time.time()),
        "completed_at": int(time.time()),
    }
    if outcome != "completed":
        payload["error"] = {"message": message, "codex_error_info": error_info}
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "type": "event_msg",
        "payload": payload,
    }
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def write_transcript(session_id: str, transcript: str, cwd: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(transcript)), exist_ok=True)
    if os.path.exists(transcript):
        return
    meta = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "type": "session_meta",
        "payload": {
            "session_id": session_id,
            "id": session_id,
            "cwd": cwd,
            "cli_version": "0.0.0-fake",
            "source": "exec",
        },
    }
    with open(transcript, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(meta) + "\n")


def handle_exec_resume(argv: list[str]) -> int:
    cfg = scenario()
    session_id = None
    seen_resume = False
    for token in argv:
        if token == "resume":
            seen_resume = True
            continue
        if seen_resume and not token.startswith("-"):
            session_id = token
            break
    template = cfg.get("transcript_template")
    transcript = (template.format(session_id=session_id) if template else None) or cfg.get(
        "transcript"
    ) or ""
    cwd = cfg.get("cwd") or os.getcwd()

    if cfg.get("argv_log"):
        with open(cfg["argv_log"], "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    if cfg.get("resume_count_file"):
        count = 0
        try:
            count = int(open(cfg["resume_count_file"]).read().strip() or "0")
        except (OSError, ValueError):
            count = 0
        with open(cfg["resume_count_file"], "w", encoding="utf-8") as handle:
            handle.write(str(count + 1))

    if transcript:
        write_transcript(session_id or "unknown", transcript, cwd)

    outcome = cfg.get("resume_outcome", "completed")
    print(json.dumps({"type": "thread.started", "thread_id": session_id}))
    print(json.dumps({"type": "turn.started"}))

    if outcome == "quota":
        reset_epoch = cfg.get("reset_epoch")
        message = cfg.get("quota_message") or (
            default_quota_message(reset_epoch) if reset_epoch else
            "You\u2019ve hit your usage limit. Try again later."
        )
        if transcript:
            append_task_complete(transcript, "quota", "usage_limit_exceeded", message)
        print(json.dumps({"type": "error", "message": message}))
        print(json.dumps({"type": "turn.failed", "error": {"message": message}}))
        return 1

    if outcome == "aborted":
        if transcript:
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "reason": "interrupted"},
            }
            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        print(json.dumps({"type": "turn.failed", "error": {"message": "interrupted"}}))
        return 1

    if outcome == "error":
        message = cfg.get("error_message", "stream disconnected")
        if transcript:
            append_task_complete(transcript, "error", "response_stream_disconnected", message)
        print(json.dumps({"type": "error", "message": message}))
        print(json.dumps({"type": "turn.failed", "error": {"message": message}}))
        return 1

    if transcript:
        append_task_complete(transcript, "completed", None, None)
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
    return 0


def handle_app_server(argv: list[str]) -> int:
    del argv
    cfg = scenario()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            _respond(request_id, {"userAgent": "fake-codex/0.0.0", "codexHome": os.environ.get("CODEX_HOME", "")})
        elif method == "initialized":
            continue
        elif method == "account/rateLimits/read":
            _respond(request_id, _rate_limits(cfg))
        elif method == "hooks/list":
            _respond(request_id, {"data": cfg.get("hooks", [])})
        elif method == "config/value/write":
            params = request.get("params") or {}
            if cfg.get("config_write_log"):
                with open(cfg["config_write_log"], "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(params) + "\n")
            _respond(request_id, {})
        else:
            if request_id is not None:
                _respond(request_id, {})
        sys.stdout.flush()
    return 0


def _rate_limits(cfg: dict) -> dict:
    windows = cfg.get("windows") or []
    primary = None
    secondary = None
    for window in windows:
        entry = {
            "usedPercent": window.get("used_percent", 0),
            "windowDurationMins": window.get("window_minutes", 300),
            "resetsAt": window.get("resets_at"),
        }
        if window.get("slot") == "secondary":
            secondary = entry
        else:
            primary = entry
    bucket = {
        "limitId": "codex",
        "limitName": None,
        "primary": primary,
        "secondary": secondary,
        "credits": {"hasCredits": True, "unlimited": False, "balance": "0"},
        "planType": cfg.get("plan_type", "plus"),
        "rateLimitReachedType": cfg.get("rate_limit_reached_type", "rate_limit_reached"),
    }
    response = {
        "ordinaryUsageAllowed": cfg.get("ordinary_usage_allowed"),
        "rateLimits": bucket,
        "rateLimitsByLimitId": {"codex": bucket},
        "rateLimitResetCredits": {"availableCount": 0, "credits": []},
        "accountId": "fake-account",
    }
    if cfg.get("upsell_reset_at"):
        response["rateLimitUpsell"] = {"reset_at": cfg["upsell_reset_at"]}
    return response


def _respond(request_id, result) -> None:
    if request_id is None:
        return
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print("codex-cli 0.0.0-fake")
        return 0
    if argv and argv[0] == "app-server":
        return handle_app_server(argv)
    if "exec" in argv and "resume" in argv:
        return handle_exec_resume(argv)
    print("fake-codex: unsupported invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
