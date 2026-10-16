"""macOS backend: a user LaunchAgent driven by ``launchctl``.

The design mirrors the systemd unit key for key:

=========================================  ====================================
systemd --user (Linux)                     launchd LaunchAgent (macOS)
=========================================  ====================================
``WantedBy=default.target``                ``RunAtLoad`` (loads at login)
``Restart=on-failure``                     ``KeepAlive{SuccessfulExit: false}``
``KillMode=process``                       ``AbandonProcessGroup``
``systemctl --user start``                 ``launchctl kickstart gui/$UID/<label>``
``systemctl --user stop``                  ``launchctl kill SIGTERM gui/$UID/<label>``
=========================================  ====================================

Modern ``launchctl`` verbs are used (``bootstrap``/``bootout``/``kickstart``/
``print``/``kill``); the deprecated ``load``/``unload`` pair is never used.
Only user-space paths are touched: ``~/Library/LaunchAgents``, never
``/Library/LaunchDaemons``, and never with root.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from .base import ServiceBackend, ServiceError, helper_argv, run_command

LABEL = "com.lutzkind.codex-auto-resume.helper"
PLIST_NAME = f"{LABEL}.plist"

#: launchd throttles a repeatedly failing job instead of hammering it.
THROTTLE_SECONDS = 15


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return launch_agents_dir() / PLIST_NAME


def domain() -> str:
    """launchd's per-user GUI domain, e.g. ``gui/501``.

    ``os.getuid`` is POSIX-only; the fallback keeps the module importable and
    callable on any platform (the macOS backend is only *used* on macOS).
    """
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - Windows only
        return "gui/0"
    return f"gui/{getuid()}"


def service_target() -> str:
    return f"{domain()}/{LABEL}"


def _string(value: str) -> str:
    return f"        <string>{escape(value)}</string>"


def render_plist(
    codex_bin: Optional[str],
    *,
    python: Optional[str] = None,
    launcher: Optional[str] = None,
    stdout: Optional[str] = None,
    label: str = LABEL,
) -> str:
    """The exact LaunchAgent property list ``install-service`` writes.

    ``ProgramArguments`` is an array, so paths with spaces, quotes or non-ASCII
    characters need no shell quoting at all.
    """
    from .. import launcher as launcher_module
    from .. import paths

    python_path = python or sys.executable
    launcher_script = launcher or launcher_module.launcher_path()
    log_path = stdout or str(paths.log_file())

    arguments = helper_argv(codex_bin)
    arguments[0] = python_path
    arguments[1] = launcher_script

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "    <key>Label</key>",
        _string(label),
        "    <key>ProgramArguments</key>",
        "    <array>",
    ]
    lines += [_string(item) for item in arguments]
    lines += [
        "    </array>",
        "    <key>RunAtLoad</key>",
        "    <true/>",
        "    <key>KeepAlive</key>",
        "    <dict>",
        "        <key>SuccessfulExit</key>",
        "        <false/>",
        "    </dict>",
        "    <key>AbandonProcessGroup</key>",
        "    <true/>",
        "    <key>ProcessType</key>",
        "    <string>Background</string>",
        "    <key>ThrottleInterval</key>",
        f"    <integer>{THROTTLE_SECONDS}</integer>",
        "    <key>EnvironmentVariables</key>",
        "    <dict>",
        "        <key>PYTHONUNBUFFERED</key>",
        "        <string>1</string>",
        "    </dict>",
        "    <key>StandardOutPath</key>",
        _string(log_path),
        "    <key>StandardErrorPath</key>",
        _string(log_path),
        "</dict>",
        "</plist>",
    ]
    return "\n".join(lines) + "\n"


def _launchctl(*args: str, check: bool = True):
    return run_command(["launchctl", *args], check=check)


def _bootout_if_loaded() -> None:
    # Best effort: bootout fails when the job is not loaded, which is normal.
    _launchctl("bootout", service_target(), check=False)


class MacOSBackend(ServiceBackend):
    name = "launchd"
    display = "launchd LaunchAgent"

    def available(self) -> tuple[bool, str]:
        completed = run_command(["launchctl", "print", domain()], check=False)
        if completed.returncode != 0:
            return False, (completed.stdout or "").strip() or "launchctl unavailable"
        return True, "launchd user domain available"

    def artifact_path(self) -> Optional[Path]:
        return plist_path()

    def install(self, codex_bin: Optional[str]) -> str:
        from .. import paths

        paths.ensure_dirs()
        launch_agents_dir().mkdir(parents=True, exist_ok=True)
        target = plist_path()
        content = render_plist(codex_bin)
        if target.exists():
            target.replace(target.with_suffix(target.suffix + ".bak"))
        target.write_text(content, encoding="utf-8")
        _bootout_if_loaded()
        completed = _launchctl("bootstrap", domain(), str(target), check=False)
        if completed.returncode != 0:
            raise ServiceError(
                "launchctl bootstrap failed: "
                + (completed.stdout or "").strip()
                + f" (plist kept at {target})"
            )
        return str(target)

    def uninstall(self) -> None:
        _bootout_if_loaded()
        target = plist_path()
        if target.exists():
            target.unlink()
        if target.exists():
            raise RuntimeError(f"could not remove {target}")

    def start(self) -> None:
        # kickstart is a no-op error when the job is already running, which is
        # exactly the semantics needed: never kill an in-flight resume.
        _launchctl("kickstart", service_target(), check=False)

    def stop(self) -> None:
        _launchctl("kill", "SIGTERM", service_target(), check=False)

    def status(self) -> dict:
        target = plist_path()
        installed = target.exists()
        detail = ""
        loaded = False
        if installed:
            completed = _launchctl("print", service_target(), check=False)
            loaded = completed.returncode == 0
            if not loaded:
                detail = (completed.stdout or "").strip()
        disabled = ""
        listing = run_command(["launchctl", "print-disabled", domain()], check=False)
        if listing.returncode == 0:
            text = listing.stdout or ""
            if "disabled services" in text:
                block = text.split("disabled services", 1)[1]
                for line in block.splitlines():
                    if LABEL in line and "true" in line.lower():
                        disabled = line.strip()
                        break
        return {
            "backend": self.name,
            "display": self.display,
            "installed": installed,
            "active": "loaded" if loaded else ("registered" if installed else "not installed"),
            "enabled": "disabled" if disabled else ("enabled" if installed else "not installed"),
            "artifact": str(target),
            "detail": detail or disabled,
            "start_hint": f"launchctl kickstart {service_target()}",
        }
