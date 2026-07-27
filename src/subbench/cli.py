from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .ccusage import CcusageSchemaError, normalise_payload
from .store import connect, estimate_windows, list_imports, save_import
from .watcher import WatchTarget, ccusage_command, watch

DEFAULT_DATABASE = Path(os.environ.get("SUBBENCH_DATABASE", Path.home() / ".local" / "share" / "subbench" / "subbench.sqlite3"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subbench", description="Continuously measure API-equivalent value delivered by coding-agent subscriptions.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help=f"SQLite database path (default: {DEFAULT_DATABASE})")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="Create the SQLite database")

    watch_parser = subcommands.add_parser("watch", help="Continuously collect usage and entitlement while you work")
    watch_parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    watch_parser.add_argument("--interval", type=float, default=60.0)
    watch_parser.add_argument("--runner", choices=("npx", "bunx", "pnpm"), default="npx")
    watch_parser.add_argument("--once", action="store_true")

    report_parser = subcommands.add_parser("report", help="Estimate API-equivalent value of observed quota windows")
    report_parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    report_parser.add_argument("--json", action="store_true", dest="as_json")

    ingest = subcommands.add_parser("ingest", help="Import ccusage JSON from a file or stdin")
    ingest.add_argument("path", type=str)
    ingest.add_argument("--provider", choices=("claude", "codex"), required=True)
    ingest.add_argument("--report", default="daily")

    collect = subcommands.add_parser("collect", help="Take one ccusage snapshot")
    collect.add_argument("provider", choices=("claude", "codex"))
    collect.add_argument("--report", default="daily")
    collect.add_argument("--runner", choices=("npx", "bunx", "pnpm"), default="npx")
    collect.add_argument("ccusage_args", nargs=argparse.REMAINDER)
    subcommands.add_parser("imports", help="List recorded imports")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = connect(args.database)
    if args.command == "init":
        print(args.database)
        return 0
    if args.command == "imports":
        return _print_imports(db)
    if args.command == "report":
        provider = None if args.provider == "all" else args.provider
        return _print_report(db, provider=provider, as_json=args.as_json)
    if args.command == "watch":
        providers = ("claude", "codex") if args.provider == "all" else (args.provider,)
        return watch(db, targets=[WatchTarget(provider=p) for p in providers], runner=args.runner, interval_seconds=args.interval, once=args.once)
    if args.command == "ingest":
        raw = sys.stdin.buffer.read() if args.path == "-" else Path(args.path).read_bytes()
        return _ingest(db, raw=raw, provider=args.provider, report=args.report, source_command=None)
    if args.command == "collect":
        passthrough = args.ccusage_args[1:] if args.ccusage_args and args.ccusage_args[0] == "--" else args.ccusage_args
        command = [*ccusage_command(runner=args.runner, provider=args.provider, report=args.report), *passthrough]
        result = subprocess.run(command, check=False, capture_output=True)
        if result.returncode != 0:
            sys.stderr.buffer.write(result.stderr)
            return result.returncode
        return _ingest(db, raw=result.stdout, provider=args.provider, report=args.report, source_command=" ".join(command))
    raise AssertionError(f"unhandled command: {args.command}")


def _ingest(db, *, raw: bytes, provider: str, report: str, source_command: str | None) -> int:
    try:
        payload = json.loads(raw)
        rows = normalise_payload(payload, provider=provider, report=report)
    except (json.JSONDecodeError, UnicodeDecodeError, CcusageSchemaError, ValueError) as error:
        print(f"invalid ccusage data: {error}", file=sys.stderr)
        return 2
    import_id, row_count, created = save_import(db, raw=raw, payload=payload, rows=rows, provider=provider, report=report, command=source_command)
    print(f"{'imported' if created else 'already present'}: import {import_id}, {row_count} normalised rows")
    return 0


def _print_report(db, *, provider: str | None, as_json: bool) -> int:
    rows = [dict(row) for row in estimate_windows(db, provider)]
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No usable intervals yet. Keep `subbench watch` running until quota and cost both increase within one reset window.")
        return 0
    print("provider\twindow\tquota delta\tAPI value\timplied full window\tinterval end")
    for row in rows:
        print(f"{row['provider']}\t{row['window']}\t{row['quota_delta_percent']:.2f}%\tUS${row['api_value_usd']:.4f}\tUS${row['implied_full_window_usd']:.2f}\t{row['observed_at']}")
    return 0


def _print_imports(db) -> int:
    rows = list_imports(db)
    if not rows:
        print("No imports recorded.")
        return 0
    print("id\tprovider\treport\trows\timported_at\tsha256")
    for row in rows:
        print(f"{row['id']}\t{row['provider']}\t{row['report']}\t{row['row_count']}\t{row['imported_at']}\t{row['payload_sha256'][:12]}")
    return 0
