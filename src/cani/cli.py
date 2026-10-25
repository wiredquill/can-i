"""can-i command line entry point: run / tui / web."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError
from .probes import Status
from .state import ProbeState

DEFAULT_CONFIG = os.environ.get("CAN_I_CONFIG", "checks.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="can-i",
        description="Security capability inspector and live sandbox demo dashboard.",
    )
    parser.add_argument("--version", action="version", version=f"can-i {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config",
            default=DEFAULT_CONFIG,
            help="path to checks.yaml (default: %(default)s, or $CAN_I_CONFIG)",
        )

    p_run = sub.add_parser("run", help="run all probes once and print results")
    add_config(p_run)
    p_run.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    p_tui = sub.add_parser("tui", help="interactive terminal dashboard")
    add_config(p_tui)

    p_web = sub.add_parser("web", help="web dashboard (FastAPI + SSE)")
    add_config(p_web)
    p_web.add_argument("--host", default="0.0.0.0")
    p_web.add_argument("--port", type=int, default=8000)

    p_from = sub.add_parser(
        "from-policy", help="derive a checks.yaml test plan from an OpenShell policy"
    )
    p_from.add_argument("policy", help="path to the OpenShell policy YAML")
    p_from.add_argument("--output", "-o", help="write checks.yaml here (default: stdout)")

    p_to = sub.add_parser(
        "to-policy", help="generate an OpenShell policy from checks.yaml"
    )
    add_config(p_to)

    return parser


def _make_state(config_path: str) -> ProbeState:
    try:
        return ProbeState(Path(config_path))
    except ConfigError as exc:
        print(f"can-i: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_run(args: argparse.Namespace) -> int:
    state = _make_state(args.config)
    results = asyncio.run(state.run_now())
    snap = state.snapshot()

    if args.json:
        print(json.dumps(snap, indent=2))
        return 0

    from rich.console import Console
    from rich.table import Table

    console = Console()
    sysinfo = snap["system"]
    landlock = (
        f"ABI v{sysinfo['landlock_abi']}" if sysinfo["landlock_abi"] else "unsupported"
    )
    console.print(
        f"[bold]{sysinfo['hostname']}[/] kernel {sysinfo['kernel']}  "
        f"user {sysinfo['user']} (uid {sysinfo['uid']})  "
        f"openshell: {'YES' if sysinfo['openshell'] else 'no'}  "
        f"runtime: {sysinfo['container_runtime'] or 'host'}  "
        f"landlock: {landlock}  seccomp: {sysinfo['seccomp']}"
    )
    table = Table(show_lines=False)
    for col in ("Status", "Check", "Category", "Blocked by", "Exit", "Latency", "Detail"):
        table.add_column(col)
    styles = {"allowed": "bold green", "denied": "bold red", "error": "bold yellow"}
    blocker_styles = {"sandbox": "bold magenta", "baseline": "dim", "unknown": "yellow"}
    for r in results:
        label = r.status.value.upper()
        style = styles.get(r.status.value, "white")
        blocked = (
            f"[{blocker_styles[r.blocker.kind]}]{r.blocker.layer}[/]" if r.blocker else ""
        )
        table.add_row(
            f"[{style}]{label}[/]",
            r.name,
            r.category,
            blocked,
            str(r.exit_code) if r.exit_code is not None else "—",
            f"{r.latency_ms:.0f} ms",
            r.detail,
        )
    console.print(table)
    denied = sum(1 for r in results if r.status != Status.ALLOWED)
    console.print(f"{len(results) - denied} allowed, {denied} denied/error")
    with_expect = [r for r in results if r.expect]
    if with_expect:
        unmet = [r for r in with_expect if r.met is False]
        style = "bold green" if not unmet else "bold yellow"
        console.print(
            f"[{style}]expectations: {len(with_expect) - len(unmet)}/{len(with_expect)} met[/]"
        )
        for r in unmet:
            console.print(f"  [yellow]✗ {r.id}: expected {r.expect}, got {r.status.value}[/]")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import CanIApp

    CanIApp(_make_state(args.config)).run()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(args.config), host=args.host, port=args.port)
    return 0


def cmd_from_policy(args: argparse.Namespace) -> int:
    from .policy import policy_to_checks

    path = Path(args.policy)
    if not path.is_file():
        print(f"can-i: policy file not found: {path}", file=sys.stderr)
        return 2
    try:
        cfg = policy_to_checks(path.read_text(encoding="utf-8"))
    except (ValueError, ConfigError) as exc:
        print(f"can-i: {exc}", file=sys.stderr)
        return 2
    text = cfg.to_yaml()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"can-i: wrote {len(cfg.checks)} checks to {args.output}", file=sys.stderr)
    else:
        print(text, end="")
    return 0


def cmd_to_policy(args: argparse.Namespace) -> int:
    from .config import load_config
    from .policy import checks_to_policy

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"can-i: {exc}", file=sys.stderr)
        return 2
    print(checks_to_policy(cfg), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "run": cmd_run,
        "tui": cmd_tui,
        "web": cmd_web,
        "from-policy": cmd_from_policy,
        "to-policy": cmd_to_policy,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
