"""Locate the Codex CLI and read the small amount of configuration we reuse."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

from . import paths

ENV_CODEX_BIN = "CODEX_AUTO_RESUME_CODEX_BIN"

#: Last-resort locations, tried in order, when ``codex`` is not on ``PATH``.
FALLBACK_BINARIES = (
    "~/.local/bin/codex",
    "/usr/local/bin/codex",
    "/opt/homebrew/bin/codex",
)


def resolve_codex_binary(explicit: Optional[str] = None) -> Optional[str]:
    """Return an executable path for Codex, or ``None`` when not found."""
    for candidate in (explicit, os.environ.get(ENV_CODEX_BIN)):
        if candidate:
            expanded = str(Path(candidate).expanduser())
            if os.access(expanded, os.X_OK) and os.path.isfile(expanded):
                return expanded

    found = shutil.which("codex")
    if found:
        return found

    for candidate in FALLBACK_BINARIES:
        expanded = str(Path(candidate).expanduser())
        if os.access(expanded, os.X_OK) and os.path.isfile(expanded):
            return expanded
    return None


def codex_version(codex_bin: Optional[str]) -> Optional[str]:
    if not codex_bin:
        return None
    import subprocess

    try:
        completed = subprocess.run(
            [codex_bin, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


_TOP_LEVEL_MODEL = re.compile(r'^\s*model\s*=\s*"([^"]*)"\s*$', re.MULTILINE)
_TOP_LEVEL_APPROVAL = re.compile(
    r'^\s*approval_policy\s*=\s*"([^"]*)"\s*$', re.MULTILINE
)


def read_config() -> dict:
    """Read the user's Codex ``config.toml`` (best effort, never raises)."""
    path = paths.codex_home() / "config.toml"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:  # Python 3.11+
        import tomllib

        parsed = tomllib.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        result: dict = {}
        model = _TOP_LEVEL_MODEL.search(raw)
        if model:
            result["model"] = model.group(1)
        approval = _TOP_LEVEL_APPROVAL.search(raw)
        if approval:
            result["approval_policy"] = approval.group(1)
        return result


def default_model() -> Optional[str]:
    value = read_config().get("model")
    return value if isinstance(value, str) and value.strip() else None
