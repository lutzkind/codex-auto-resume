#!/usr/bin/env python3
"""Validate the Codex plugin packaging in this repository.

Checks the portable Agent Plugins manifest, the Codex overlay, the hook pack and
the marketplace manifest against what the Codex CLI expects.
"""

from __future__ import annotations

import json
import pathlib
import sys

REQUIRED_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "Interrupt")
AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


def load(root: pathlib.Path, relative: str, failures: list[str]) -> dict:
    path = root / relative
    if not path.is_file():
        failures.append(f"{relative} is missing")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        failures.append(f"{relative} is not valid JSON: {error}")
        return {}


def check_plugin(root: pathlib.Path, failures: list[str]) -> None:
    portable = load(root, "plugin.json", failures)
    if portable.get("$schema") != AGENT_PLUGIN_SCHEMA:
        failures.append(f"plugin.json must declare the Agent Plugins v1 $schema {AGENT_PLUGIN_SCHEMA}")
    name = str(portable.get("name") or "")
    if (
        not name
        or name.startswith("-")
        or name.endswith("-")
        or "--" in name
        or ".." in name
        or not all(ch.islower() or ch.isdigit() or ch in ".-" for ch in name)
    ):
        failures.append(f"plugin.json name {name!r} is not a valid Agent Plugins plugin name")


def check_overlay(root: pathlib.Path, failures: list[str]) -> None:
    overlay = load(root, ".codex-plugin/plugin.json", failures)
    if overlay.get("hooks") != "./hooks/hooks.json":
        failures.append("the Codex overlay must declare hooks './hooks/hooks.json'")
    if overlay.get("skills") != "./skills/":
        failures.append("the Codex overlay should declare skills './skills/'")
    interface = overlay.get("interface") or {}
    for field in ("displayName", "developerName"):
        if not interface.get(field):
            failures.append(f"the Codex overlay interface needs {field}")


def check_hooks(root: pathlib.Path, failures: list[str]) -> None:
    hooks = load(root, "hooks/hooks.json", failures).get("hooks")
    if not isinstance(hooks, dict):
        failures.append("hooks/hooks.json must contain a 'hooks' object")
        return
    for event in REQUIRED_EVENTS:
        if not hooks.get(event):
            failures.append(f"hooks/hooks.json is missing {event}")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            failures.append(f"{event} must be a list of matcher groups")
            continue
        for group in groups:
            for handler in (group or {}).get("hooks", []):
                if handler.get("type") != "command":
                    failures.append(f"{event} handler must be of type 'command'")
                command = handler.get("command") or ""
                if not command:
                    failures.append(f"{event} handler has an empty command")
                if "${PLUGIN_ROOT}" not in command:
                    failures.append(
                        f"{event} handler must reference ${{PLUGIN_ROOT}} so it works from any install"
                    )
                timeout = handler.get("timeout")
                if timeout is not None and not isinstance(timeout, int):
                    failures.append(f"{event} handler timeout must be an integer")


def check_marketplace(root: pathlib.Path, failures: list[str]) -> None:
    for relative in (
        ".agents/plugins/marketplace.json",
        ".claude-plugin/marketplace.json",
        ".cursor-plugin/marketplace.json",
    ):
        if (root / relative).is_file():
            break
    else:
        failures.append("no Codex marketplace manifest found under a supported path")
        return
    manifest = load(root, ".agents/plugins/marketplace.json", failures)
    if not manifest.get("name"):
        failures.append("marketplace.json needs a name")
    plugins = manifest.get("plugins") or []
    entry = next((item for item in plugins if item.get("name") == "codex-auto-resume"), None)
    if entry is None:
        failures.append("marketplace.json must list the codex-auto-resume plugin")
        return
    if (entry.get("source") or {}).get("path") not in (".", "./"):
        failures.append("marketplace.json must point the plugin at the repository root")


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    failures: list[str] = []
    check_plugin(root, failures)
    check_overlay(root, failures)
    check_hooks(root, failures)
    check_marketplace(root, failures)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("manifests: FAILED")
        return 1
    print("manifests: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
