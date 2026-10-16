@echo off
rem codex-auto-resume Windows launcher.
rem
rem Codex runs plugin command hooks through `cmd.exe /C "<commandWindows>"`
rem (codex-rs/hooks/src/engine/command_runner.rs), so the hook command resolves
rem to this batch file, which then finds a Python 3.9+ interpreter and runs the
rem real launcher next to it. Hooks receive their JSON payload on stdin and the
rem exit code must always be 0 so a hook can never fail a Codex turn; the Python
rem entry point already guarantees that, and this file forwards it faithfully.
setlocal enableextensions

set "PYTHON="
for %%I in (python.exe py.exe python3.exe) do (
  if not defined PYTHON (
    where %%I >nul 2>nul
    if not errorlevel 1 set "PYTHON=%%I"
  )
)

if not defined PYTHON (
  >&2 echo codex-auto-resume: Python 3.9+ was not found on PATH. Install Python or run ^"codex-auto-resume doctor^" from a shell that has it.
  exit /b 127
)

"%PYTHON%" "%~dp0codex-auto-resume" %*
exit /b %ERRORLEVEL%
