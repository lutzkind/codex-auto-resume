"""Locate the installed launcher script for the running package."""

from __future__ import annotations

from pathlib import Path

LAUNCHER_NAME = "codex-auto-resume"
WINDOWS_LAUNCHER_NAME = "codex-auto-resume.cmd"


def install_root() -> Path:
    """The directory that contains ``bin/`` and ``src/`` for this package.

    The package always runs from a source tree (the checkout or the installed
    Codex plugin payload); ``bin/codex-auto-resume`` sits two levels above
    ``codex_auto_resume/`` in both.
    """
    import codex_auto_resume

    package_dir = Path(codex_auto_resume.__file__).resolve().parent
    for candidate in (package_dir.parent.parent, package_dir.parent):
        if (candidate / "bin" / LAUNCHER_NAME).exists():
            return candidate
    return package_dir.parent.parent


def launcher_path() -> str:
    """Absolute path of the Python launcher (works with spaces and macOS/Windows)."""
    return str(install_root() / "bin" / LAUNCHER_NAME)


def windows_launcher_path() -> str:
    """Absolute path of the ``cmd`` launcher used by Codex hooks on Windows."""
    return str(install_root() / "bin" / WINDOWS_LAUNCHER_NAME)
