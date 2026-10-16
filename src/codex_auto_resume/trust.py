"""Persist Codex hook trust for this plugin's bundled hooks.

Codex refuses to run hooks whose current hash is not persisted in
``config.toml`` as ``[hooks.state."<key>"] trusted_hash = "<hash>"``. The TUI
does the same thing when a user approves a hook in ``/hooks``.

This module does not guess the hash: it asks Codex for it through the
app-server ``hooks/list`` method (which reports ``currentHash`` and
``trustStatus`` per handler) and then writes it with the app-server
``config/value/write`` method, the same programmatic write path Codex uses
internally.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Optional

from . import __version__, appserver

PLUGIN_ID_PREFIX = "codex-auto-resume@"
INITIALIZE_ID = 1
HOOKS_LIST_ID = 2


def _exchange(codex_bin: str, codex_home: str, requests: list[dict], timeout: float = 60.0) -> list[dict]:
    """Send JSON-RPC requests to ``codex app-server --stdio`` in order."""
    proc = appserver.spawn_app_server(codex_bin, extra_env={"CODEX_HOME": codex_home})
    reader = appserver.LineReader(proc.stdout)
    responses: list[dict] = []
    try:
        deadline = time.time() + timeout
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            request_id = request.get("id")
            if request_id is None:
                continue
            while True:
                line = reader.read_line(deadline)
                if line is None:
                    raise TimeoutError("timed out talking to codex app-server")
                if not line:
                    if proc.poll() is not None:
                        raise RuntimeError("codex app-server closed stdout")
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                if isinstance(message.get("error"), dict):
                    raise RuntimeError(str(message["error"]))
                responses.append(message.get("result") or {})
                break
    finally:
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass
        from . import sysinfo

        sysinfo.terminate_tree(proc)
        try:
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    return responses


def _initialize_request() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": INITIALIZE_ID,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "codex-auto-resume",
                "title": "codex-auto-resume",
                "version": __version__,
            }
        },
    }


def list_hooks(codex_bin: str, codex_home: str) -> list[dict]:
    """Return the hook entries Codex currently discovers, including ours."""
    requests = [
        _initialize_request(),
        {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        {"jsonrpc": "2.0", "id": HOOKS_LIST_ID, "method": "hooks/list", "params": {}},
    ]
    responses = _exchange(codex_bin, codex_home, requests)
    if not responses:
        return []
    data = responses[-1].get("data")
    if not isinstance(data, list):
        return []
    entries: list[dict] = []
    for group in data:
        cwd = group.get("cwd") if isinstance(group, dict) else None
        for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
            if not isinstance(hook, dict):
                continue
            entries.append({**hook, "cwd": cwd})
    return entries


def plugin_hook_status(codex_bin: str, codex_home: str) -> list[dict]:
    """Return this plugin's hooks with their trust status."""
    result = []
    try:
        entries = list_hooks(codex_bin, codex_home)
    except (OSError, RuntimeError, TimeoutError):
        return []
    for entry in entries:
        key = str(entry.get("key") or "")
        if not _is_ours(entry, key):
            continue
        result.append(
            {
                "key": key,
                "event_name": entry.get("eventName") or entry.get("event_name"),
                "trust_status": entry.get("trustStatus") or entry.get("trust_status"),
                "current_hash": entry.get("currentHash") or entry.get("current_hash"),
                "trusted": (entry.get("trustStatus") or entry.get("trust_status"))
                in ("trusted", "managed"),
            }
        )
    return result


def _is_ours(entry: dict, key: str) -> bool:
    plugin_id = entry.get("pluginId") or entry.get("plugin_id")
    if isinstance(plugin_id, str) and plugin_id.startswith(PLUGIN_ID_PREFIX):
        return True
    return key.startswith(PLUGIN_ID_PREFIX)


def _escape_key(key: str) -> str:
    return key.replace("\\", "\\\\").replace('"', '\\"')


def trust_plugin_hooks(codex_bin: str, codex_home: str, dry_run: bool = False) -> dict:
    """Persist trust for every hook this plugin ships."""
    result: dict[str, Any] = {"hooks": [], "errors": [], "written": 0}
    try:
        entries = list_hooks(codex_bin, codex_home)
    except (OSError, RuntimeError, TimeoutError) as error:
        result["errors"].append(f"hooks/list failed: {error}")
        return result

    ours = [
        entry
        for entry in entries
        if _is_ours(entry, str(entry.get("key") or ""))
    ]
    if not ours:
        result["errors"].append(
            "no codex-auto-resume hooks discovered; install the plugin first"
        )
        return result

    requests: list[dict] = [_initialize_request(), {"jsonrpc": "2.0", "method": "initialized", "params": {}}]
    request_id = HOOKS_LIST_ID
    for entry in ours:
        key = str(entry.get("key") or "")
        current_hash = entry.get("currentHash") or entry.get("current_hash")
        trust_status = entry.get("trustStatus") or entry.get("trust_status")
        action = "unchanged"
        if trust_status == "trusted" or not current_hash:
            pass
        elif dry_run:
            action = "would-write"
        else:
            request_id += 1
            requests.append(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "config/value/write",
                    "params": {
                        "keyPath": f'hooks.state."{_escape_key(key)}".trusted_hash',
                        "value": current_hash,
                        "mergeStrategy": "replace",
                    },
                }
            )
            action = "written"
        result["hooks"].append(
            {
                "key": key,
                "trust_status": trust_status,
                "current_hash": current_hash,
                "action": action,
            }
        )

    if dry_run or not any(item["action"] == "written" for item in result["hooks"]):
        return result

    try:
        _exchange(codex_bin, codex_home, requests)
    except (OSError, RuntimeError, TimeoutError) as error:
        result["errors"].append(f"config/value/write failed: {error}")
        return result
    result["written"] = sum(1 for item in result["hooks"] if item["action"] == "written")
    return result
