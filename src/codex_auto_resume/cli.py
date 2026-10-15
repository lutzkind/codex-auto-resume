"""Command line interface: status, enable, disable, cancel, logs, doctor."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__, codexenv, helper, hooks, log, paths, state

UNIT_NAME = "codex-auto-resume-helper.service"
CLI_NAME = "codex-auto-resume"


def _fmt_time(value: Optional[float]) -> str:
    if not value:
        return "-"
    try:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return "-"
    return moment.strftime("%Y-%m-%d %H:%M:%S %Z")


def _describe_state(record: dict) -> str:
    return str(record.get("state") or "unknown")


def _session_attached(session: Optional[dict]) -> Optional[bool]:
    if not session:
        return None
    attached = session.get("attached")
    if attached is False:
        return False
    pid = session.get("host_pid")
    if isinstance(pid, int) and pid > 0:
        return paths.process_alive(pid, session.get("host_pid_start"))
    return attached if isinstance(attached, bool) else None


def cmd_status(args: argparse.Namespace) -> int:
    settings = state.load_settings()
    pending = state.iter_pending()
    payload = {
        "enabled": bool(settings.get("enabled", True)),
        "state_dir": str(paths.state_dir()),
        "helper_running": _helper_running(),
        "pending": [],
    }
    for record in pending:
        session = state.read_session(record["session_id"])
        payload["pending"].append(
            {
                "session_id": record["session_id"],
                "state": _describe_state(record),
                "reset_at": record.get("reset_at"),
                "resume_at": record.get("resume_at"),
                "reset_source": record.get("reset_source"),
                "attempts": record.get("attempts", 0),
                "cwd": record.get("cwd"),
                "session_attached": _session_attached(session),
                "last_result": record.get("last_result"),
                "quota_message": record.get("quota_message"),
            }
        )

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    print("enabled" if payload["enabled"] else "disabled")
    print(f"helper: {'running' if payload['helper_running'] else 'not running'}")
    if not payload["pending"]:
        print("no pending sessions")
        return 0
    for item in payload["pending"]:
        print(f"session: {item['session_id']}")
        print(f"  state: {item['state']}")
        print(
            "  reset_at: {} (source: {})".format(
                _fmt_time(item["reset_at"]), item.get("reset_source") or "unknown"
            )
        )
        print(f"  resume_at: {_fmt_time(item['resume_at'])}")
        attached = item["session_attached"]
        print(
            "  session_attached: "
            + ("yes" if attached else "no" if attached is False else "unknown")
        )
        print(f"  attempts: {item['attempts']}")
        if item.get("cwd"):
            print(f"  cwd: {item['cwd']}")
        if item.get("last_result"):
            print(f"  last_result: {item['last_result']}")
    return 0


def _helper_running() -> bool:
    record = paths.read_json(paths.helper_pid_file()) or {}
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    text = paths.process_cmdline(pid)
    if "codex_auto_resume" not in text and "codex-auto-resume" not in text:
        return False
    return paths.process_alive(pid, None)


def cmd_enable(args: argparse.Namespace) -> int:
    del args
    state.set_enabled(True)
    log.log("cli_enable")
    print("codex-auto-resume: enabled")
    if state.active_pending():
        hooks._ensure_helper_running()
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    del args
    state.set_enabled(False)
    log.log("cli_disable")
    print("codex-auto-resume: disabled (pending state is kept, nothing will resume)")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    targets: list[str] = []
    if args.all:
        targets = [record["session_id"] for record in state.iter_pending()]
        if not targets:
            print("codex-auto-resume: no pending sessions")
            return 0
    elif args.session_id:
        targets = [args.session_id]
    else:
        print("codex-auto-resume: provide a session id or --all", file=sys.stderr)
        return 2

    for session_id in targets:
        pending = state.read_pending(session_id)
        interruption_at = pending.get("interruption_at") if pending else None
        state.write_cancelled(session_id, "user_cancel", interruption_at)
        state.clear_pending(session_id, "cancelled")
        state.clear_launching(session_id)
        log.log("cli_cancel", session_id=session_id)
        print(f"codex-auto-resume: cancelled automatic resume for {session_id}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    records = log.read_records(limit=args.limit)
    if args.json:
        for record in records:
            print(json.dumps(record, ensure_ascii=False))
        return 0
    if not records:
        print(f"no log records in {paths.log_file()}")
        return 0
    for record in records:
        stamp = _fmt_time(record.get("ts"))
        event = record.get("event")
        detail = {
            key: value
            for key, value in record.items()
            if key not in ("ts", "event") and value is not None
        }
        print(f"{stamp}  {event}  {json.dumps(detail, ensure_ascii=False)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    del args
    codex_bin = codexenv.resolve_codex_binary()
    codex_home = paths.codex_home()
    checks: list[tuple[str, bool, str]] = []

    checks.append(("codex binary", bool(codex_bin), codex_bin or "not found on PATH"))
    checks.append(("codex home", codex_home.is_dir(), str(codex_home)))
    checks.append(("state dir", paths.state_dir().is_dir(), str(paths.state_dir())))

    config = codexenv.read_config()
    features = config.get("features") or {}
    checks.append(
        ("plugins feature enabled", bool(features.get("plugins")), f"features.plugins={features.get('plugins')}")
    )
    checks.append(
        ("hooks feature enabled", features.get("hooks") is not False, f"features.hooks={features.get('hooks')}")
    )
    approval = config.get("approval_policy")
    checks.append(
        (
            "unattended approvals safe",
            approval in (None, "never", "on-failure"),
            f"approval_policy={approval!r} (should be 'never' for unattended resume)",
        )
    )
    trusted = _trusted_plugin_hooks(codex_bin, codex_home)
    checks.append(
        (
            "plugin hooks trusted",
            bool(trusted and all(item.get("trusted") for item in trusted)),
            ", ".join(
                f"{item['key']}={item['trust_status']}" for item in trusted
            )
            or "no codex-auto-resume hooks discovered",
        )
    )
    snapshot = None
    if codex_bin:
        from . import ratelimit

        snapshot = ratelimit.query(codex_bin, codex_home=str(codex_home))
    verdict = _availability(snapshot)
    checks.append(
        (
            "app-server rate limits readable",
            snapshot is not None,
            json.dumps(verdict, ensure_ascii=False) if snapshot else "unavailable",
        )
    )
    checks.append(("helper service installed", _unit_installed(), UNIT_NAME))
    checks.append(("helper running", _helper_running(), str(paths.helper_pid_file())))

    ok = True
    for name, passed, detail in checks:
        if not passed:
            ok = False
        print(f"[{'ok' if passed else '!!'}] {name}: {detail}")
    print("doctor: ok" if ok else "doctor: problems found")
    return 0 if ok else 1


def _availability(snapshot):
    from . import ratelimit

    return ratelimit.availability(snapshot, time.time())


def _trusted_plugin_hooks(codex_bin: Optional[str], codex_home) -> list[dict]:
    from . import trust

    if not codex_bin:
        return []
    return trust.plugin_hook_status(codex_bin, str(codex_home))


def _unit_installed() -> bool:
    unit_path = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    return unit_path.exists()


def cmd_trust_hooks(args: argparse.Namespace) -> int:
    from . import trust

    codex_bin = codexenv.resolve_codex_binary()
    if not codex_bin:
        print("codex-auto-resume: codex binary not found", file=sys.stderr)
        return 1
    result = trust.trust_plugin_hooks(codex_bin, str(paths.codex_home()), dry_run=args.dry_run)
    for item in result["hooks"]:
        print(f"{item['key']}: {item['trust_status']} -> {item['action']}")
    if result["errors"]:
        for error in result["errors"]:
            print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def cmd_install_service(args: argparse.Namespace) -> int:
    del args
    from . import service

    codex_bin = codexenv.resolve_codex_binary()
    path = service.install_unit(codex_bin=codex_bin)
    print(f"codex-auto-resume: installed {path}")
    return 0


def cmd_uninstall_service(args: argparse.Namespace) -> int:
    del args
    from . import service

    service.uninstall_unit()
    print("codex-auto-resume: helper service removed")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    return hooks.main(args.event)


def cmd_helper(args: argparse.Namespace) -> int:
    codex_bin = args.codex_bin or codexenv.resolve_codex_binary()
    return helper.main(codex_bin)


def cmd_version(args: argparse.Namespace) -> int:
    del args
    print(f"{CLI_NAME} {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=CLI_NAME,
        description=(
            "Resume a Codex session automatically after a usage-limit interruption."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{CLI_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="show enabled state and pending recoveries")
    status.add_argument("--json", action="store_true", help="machine readable output")
    status.set_defaults(func=cmd_status)

    enable = sub.add_parser("enable", help="allow automatic resume")
    enable.set_defaults(func=cmd_enable)

    disable = sub.add_parser("disable", help="stop automatic resume without deleting state")
    disable.set_defaults(func=cmd_disable)

    cancel = sub.add_parser("cancel", help="cancel a pending automatic resume")
    cancel.add_argument("session_id", nargs="?", help="Codex session id (UUID)")
    cancel.add_argument("--all", action="store_true", help="cancel every pending recovery")
    cancel.set_defaults(func=cmd_cancel)

    logs = sub.add_parser("logs", help="show recent helper log records")
    logs.add_argument("-n", "--limit", type=int, default=50)
    logs.add_argument("--json", action="store_true", help="one JSON object per line")
    logs.set_defaults(func=cmd_logs)

    doctor = sub.add_parser("doctor", help="check the local installation")
    doctor.set_defaults(func=cmd_doctor)

    trust_hooks = sub.add_parser(
        "trust-hooks", help="persist Codex hook trust for the bundled plugin hooks"
    )
    trust_hooks.add_argument("--dry-run", action="store_true")
    trust_hooks.set_defaults(func=cmd_trust_hooks)

    install = sub.add_parser("install-service", help="install the user-level helper service")
    install.set_defaults(func=cmd_install_service)

    uninstall = sub.add_parser(
        "uninstall-service", help="remove the user-level helper service"
    )
    uninstall.set_defaults(func=cmd_uninstall_service)

    hook = sub.add_parser("hook", help="internal: Codex hook entry point")
    hook.add_argument("event")
    hook.set_defaults(func=cmd_hook)

    run_helper = sub.add_parser("helper", help="internal: run the resume helper in the foreground")
    run_helper.add_argument("--codex-bin", default=None)
    run_helper.set_defaults(func=cmd_helper)

    version = sub.add_parser("version", help="print the version")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
