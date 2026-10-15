"""Append-only JSONL logging with bounded size."""

from __future__ import annotations

import json
import os
import time
from typing import Any

from . import paths

MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 2
SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "authorization",
    "auth",
    "token",
    "credential",
    "credentials",
    "password",
    "secret",
}

# Never log more than this many characters of free text (prompt/error text).
MAX_TEXT = 600


def _scrub(key: str, value: Any) -> Any:
    if key.lower() in SECRET_KEYS:
        return "<redacted>"
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "...<truncated>"
    if isinstance(value, dict):
        return {k: _scrub(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(key, item) for item in value]
    return value


def _rotate(path) -> None:
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size < MAX_BYTES:
        return
    for index in range(BACKUPS, 0, -1):
        source = path if index == 1 else path.with_suffix(path.suffix + f".{index - 1}")
        target = path.with_suffix(path.suffix + f".{index}")
        if source.exists():
            try:
                os.replace(source, target)
            except OSError:
                pass


def log(event: str, **fields: Any) -> None:
    """Append one structured log record. Never raises."""
    record = {"ts": time.time(), "event": event}
    for key, value in fields.items():
        record[key] = _scrub(key, value)
    try:
        paths.ensure_dir(paths.logs_dir())
        path = paths.log_file()
        _rotate(path)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        try:
            os.chmod(path, paths.FILE_MODE)
        except OSError:
            pass
    except OSError:
        return


def read_records(limit: int = 200) -> list[dict]:
    path = paths.log_file()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records
