"""Command line interface: status, enable, disable, cancel, logs, doctor."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__, codexenv, helper, hooks, log, paths, state, sysinfo

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
    pid_state = helper.helper_pid_state()
    from . import service as service_module

    platform = sysinfo.info()
    service_report = service_module.service_status()
    payload = {
        "version": __version__,
        "enabled": bool(settings.get("enabled", True)),
        "state_dir": str(paths.state_dir()),
        "platform": platform.as_dict(),
        "service": {
            "backend": service_report.get("backend"),
            "installed": service_report.get("installed"),
            "active": service_report.get("active"),
            "enabled": service_report.get("enabled"),
        },
        "helper_running": pid_state["state"] == helper.PID_STATE_RUNNING,
        "helper_state": pid_state["state"],
        "helper_pid": pid_state["pid"],
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

    print(f"version: {payload['version']}")
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
    """True when the pid marker points at a live helper process.

    Thin alias over the canonical liveness logic in ``helper.helper_pid_state``
    so `status`, `doctor` and the hooks can never disagree.
    """
    return helper.is_helper_running()


def cmd_enable(args: argparse.Namespace) -> int:
    del args
    state.set_enabled(True)
    log.log("cli_enable")
    print("codex-auto-resume: enabled")
    if state.active_pending():
        helper.notify_helper()
        hooks._ensure_helper_running()
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    del args
    state.set_enabled(False)
    log.log("cli_disable")
    # Wake a waiting helper so it stops immediately instead of at the reset.
    helper.notify_helper()
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
    # Wake a waiting helper so it drops the cancelled record now.
    helper.notify_helper()
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
    from . import service as service_module

    codex_bin = codexenv.resolve_codex_binary()
    codex_home = paths.codex_home()
    platform = sysinfo.info()
    service_report = service_module.service_status()
    checks: list[tuple[str, bool, str]] = []

    checks.append(("platform", True, platform.label()))
    checks.append(
        (
            "service backend",
            service_report.get("backend") != "unsupported",
            "{} ({})".format(
                service_report.get("display") or service_report.get("backend"),
                "available" if _backend_available() else "unavailable",
            ),
        )
    )
    checks.append(("codex binary", bool(codex_bin), codex_bin or "not found on PATH"))
    checks.append(("codex home", codex_home.is_dir(), str(codex_home)))
    checks.append(
        (
            "state dir",
            paths.state_dir().is_dir(),
            f"{paths.state_dir()} (source: {paths.state_dir_source()})",
        )
    )

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
    checks.append(
        (
            "helper service installed",
            bool(service_report.get("installed")),
            str(service_report.get("artifact") or service_report.get("backend")),
        )
    )
    checks.append(
        (
            "helper service enabled for boot",
            str(service_report.get("enabled")) in ("enabled", "loaded"),
            "enabled={} active={}{}".format(
                service_report.get("enabled"),
                service_report.get("active"),
                f" · {service_report['detail']}" if service_report.get("detail") else "",
            ),
        )
    )
    helper_ok, helper_detail = _helper_health()
    checks.append(("helper state", helper_ok, helper_detail))

    ok = True
    for name, passed, detail in checks:
        if not passed:
            ok = False
        print(f"[{'ok' if passed else '!!'}] {name}: {detail}")
    print("doctor: all checks passed" if ok else "doctor: problems found")
    return 0 if ok else 1


def _helper_health() -> tuple[bool, str]:
    """Doctor's view of the helper: idle is healthy, orphaned work is not.

    The helper exits by design when there is nothing to resume, so "no helper
    process" is the normal state of a healthy installation — that is exactly
    what ``status`` reports as ``helper: not running``. It only becomes a
    problem when an active recovery exists while automatic resume is enabled
    and nothing is serving it. Stale pid markers found on the way are removed.
    """
    verdict = helper.reconcile_helper_pid()
    if verdict["state"] == helper.PID_STATE_RUNNING:
        return True, "healthy (running: pid {}, started {})".format(
            verdict["pid"], _fmt_time(verdict.get("started_at"))
        )
    active = state.active_pending()
    if active and state.is_enabled():
        from . import service as service_module

        start_hint = service_module.service_status().get("start_hint") or "start the helper"
        return False, f"not running with {len(active)} active recovery(ies); {start_hint}"
    if verdict["state"] == helper.PID_STATE_IDLE:
        detail = "no helper process, no active recoveries"
    else:
        detail = "removed {} pid marker ({})".format(
            verdict["state"], verdict["reason"]
        )
    if active:
        detail += f", {len(active)} recovery(ies) paused while disabled"
    return True, f"healthy (idle: {detail})"


def _availability(snapshot):
    from . import ratelimit

    return ratelimit.availability(snapshot, time.time())


def _trusted_plugin_hooks(codex_bin: Optional[str], codex_home) -> list[dict]:
    from . import trust

    if not codex_bin:
        return []
    return trust.plugin_hook_status(codex_bin, str(codex_home))


def _backend_available() -> bool:
    from . import service as service_module

    try:
        available, _ = service_module.available()
        return available
    except Exception:  # noqa: BLE001 - doctor must report, not crash
        return False


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
    from . import service as service_module

    codex_bin = codexenv.resolve_codex_binary()
    platform = sysinfo.info()
    try:
        description = service_module.install_service(codex_bin)
    except service_module.ServiceError as error:
        print(
            "codex-auto-resume: could not install the helper integration on "
            f"{platform.label()} ({platform.service_backend}): {error}",
            file=sys.stderr,
        )
        return 1
    print(
        f"codex-auto-resume: installed helper integration "
        f"({service_module.backend().display}): {description}"
    )
    return 0


def cmd_uninstall_service(args: argparse.Namespace) -> int:
    del args
    from . import service as service_module

    try:
        service_module.uninstall_service()
    except service_module.ServiceError as error:
        print(f"codex-auto-resume: could not remove the helper integration: {error}", file=sys.stderr)
        return 1
    print("codex-auto-resume: helper integration removed")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    return hooks.main(args.event)


def cmd_helper(args: argparse.Namespace) -> int:
    # The installed service passes the state dir (and CODEX_HOME when it was
    # set at install time) explicitly, so the helper does not depend on the
    # service manager's environment — which is minimal on launchd/Windows.
    if getattr(args, "state_dir", None):
        os.environ[paths.ENV_STATE_DIR] = args.state_dir
    if getattr(args, "codex_home", None):
        os.environ[paths.ENV_CODEX_HOME] = args.codex_home
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
    run_helper.add_argument(
        "--state-dir",
        default=None,
        help="internal: state directory the installed service was created with",
    )
    run_helper.add_argument(
        "--codex-home",
        default=None,
        help="internal: CODEX_HOME the installed service was created with",
    )
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
