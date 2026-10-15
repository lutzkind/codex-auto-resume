#!/usr/bin/env python3
"""Validate the Codex plugin packaging in this repository.

Packaging was chosen from the behaviour of the installed Codex CLI
(``codex-cli 0.162.1``), not from assumption:

* ``codex_utils_plugins::find_plugin_manifest_path`` looks for a root
  ``plugin.json`` first and only falls back to ``.codex-plugin/plugin.json``
  when the root file is absent (or is not an Agent Plugins manifest).
* ``core-plugins`` only loads plugin-bundled hooks when the effective manifest
  is the legacy format: ``if manifest_format == PluginManifestFormat::Legacy {
  load_plugin_hooks(..) } else { (Vec::new(), Vec::new()) }``.

A portable Agent Plugins root ``plugin.json`` therefore wins discovery and
silently disables the bundled hooks, so it must not be shipped. Verified with
``hooks/list``: with the root manifest present the plugin reported zero hooks;
without it all five hooks are discovered with hashes.

This script fails the build if that mistake is reintroduced.
"""

from __future__ import annotations

import json
import pathlib
import sys

REQUIRED_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd", "Interrupt")
AGENT_PLUGIN_SCHEMA_PREFIX = "https://agent-plugins.org/schemas/"


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


def check_no_root_manifest(root: pathlib.Path, failures: list[str]) -> None:
    plugin_json = root / "plugin.json"
    if not plugin_json.exists():
        return
    text = plugin_json.read_text(encoding="utf-8", errors="replace")
    if AGENT_PLUGIN_SCHEMA_PREFIX in text:
        failures.append(
            "a root plugin.json with an Agent Plugins $schema takes precedence over "
            ".codex-plugin/plugin.json and disables plugin-bundled hooks in Codex 0.162.x; "
            "remove it or verify hooks/list still reports the hooks"
        )
    else:
        failures.append(
            "root plugin.json is not a supported manifest; the Codex manifest lives at "
            ".codex-plugin/plugin.json"
        )


def check_manifest(root: pathlib.Path, failures: list[str]) -> None:
    manifest = load(root, ".codex-plugin/plugin.json", failures)
    name = str(manifest.get("name") or "")
    if not name:
        failures.append(".codex-plugin/plugin.json needs a name")
    if manifest.get("hooks") != "./hooks/hooks.json":
        failures.append(".codex-plugin/plugin.json must declare hooks './hooks/hooks.json'")
    if manifest.get("skills") != "./skills/":
        failures.append(".codex-plugin/plugin.json should declare skills './skills/'")
    interface = manifest.get("interface") or {}
    for field in ("displayName", "developerName"):
        if not interface.get(field):
            failures.append(f".codex-plugin/plugin.json interface needs {field}")


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
    supported = (
        ".agents/plugins/marketplace.json",
        ".agents/plugins/api_marketplace.json",
        ".claude-plugin/marketplace.json",
        ".cursor-plugin/marketplace.json",
    )
    if not any((root / relative).is_file() for relative in supported):
        failures.append(
            "no marketplace manifest found under a supported path: " + ", ".join(supported)
        )
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
    check_no_root_manifest(root, failures)
    check_manifest(root, failures)
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
