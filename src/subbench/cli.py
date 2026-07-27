from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .ccusage import CcusageSchemaError, normalise_payload
from .store import connect, list_imports, save_import
from .watcher import WatchTarget, ccusage_command, watch

DEFAULT_DATABASE = Path(
    os.environ.get(
        "SUBBENCH_DATABASE",
        Path.home() / ".local" / "share" / "subbench" / "subbench.sqlite3",
    )
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subbench",
        description="Continuously measure API-equivalent value delivered by coding-agent subscriptions.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite database path (default: {DEFAULT_DATABASE})",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("init", help="Create the SQLite database")

    watch_parser = subcommands.add_parser(
        "watch",
        help="Continuously collect Claude Code and Codex usage while you work",
    )
    watch_parser.add_argument(
        "--provider",
        choices=("all", "claude", "codex"),
        default="all",
        help="Provider to monitor (default: both)",
    )
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between snapshots (default: 60)",
    )
    watch_parser.add_argument(
        "--runner",
        choices=("npx", "bunx", "pnpm"),
        default="npx",
        help="Package runner used to invoke ccusage",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help="Take one snapshot and exit; intended for service testing",
    )

    ingest = subcommands.add_parser("ingest", help="Import ccusage JSON from a file or stdin")
    ingest.add_argument("path", type=str, help="JSON file path, or '-' for stdin")
    ingest.add_argument("--provider", choices=("claude", "codex"), required=True)
    ingest.add_argument("--report", default="daily")

    collect = subcommands.add_parser("collect", help="Take one ccusage snapshot")
    collect.add_argument("provider", choices=("claude", "codex"))
    collect.add_argument("--report", default="daily")
    collect.add_argument(
        "--runner",
        choices=("npx", "bunx", "pnpm"),
        default="npx",
        help="Package runner used to invoke ccusage",
    )
    collect.add_argument(
        "ccusage_args",
        nargs=argparse.REMAINDER,
        help="Additional ccusage arguments after '--'",
    )

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
    if args.command == "watch":
        providers = ("claude", "codex") if args.provider == "all" else (args.provider,)
        targets = [WatchTarget(provider=provider) for provider in providers]
        return watch(
            db,
            targets=targets,
            runner=args.runner,
            interval_seconds=args.interval,
            once=args.once,
        )
    if args.command == "ingest":
        raw = sys.stdin.buffer.read() if args.path == "-" else Path(args.path).read_bytes()
        return _ingest(
            db,
            raw=raw,
            provider=args.provider,
            report=args.report,
            source_command=None,
        )
    if args.command == "collect":
        command = _ccusage_command(
            runner=args.runner,
            provider=args.provider,
            report=args.report,
            extra=args.ccusage_args,
        )
        result = subprocess.run(command, check=False, capture_output=True)
        if result.returncode != 0:
            sys.stderr.buffer.write(result.stderr)
            return result.returncode
        return _ingest(
            db,
            raw=result.stdout,
            provider=args.provider,
            report=args.report,
            source_command=" ".join(command),
        )
    raise AssertionError(f"unhandled command: {args.command}")


def _ccusage_command(*, runner: str, provider: str, report: str, extra: list[str]) -> list[str]:
    passthrough = extra[1:] if extra and extra[0] == "--" else extra
    return [*ccusage_command(runner=runner, provider=provider, report=report), *passthrough]


def _ingest(db, *, raw: bytes, provider: str, report: str, source_command: str | None) -> int:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        print(f"invalid ccusage JSON: {error}", file=sys.stderr)
        return 2

    try:
        rows = normalise_payload(payload, provider=provider, report=report)
    except (CcusageSchemaError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    import_id, row_count, created = save_import(
        db,
        raw=raw,
        payload=payload,
        rows=rows,
        provider=provider,
        report=report,
        command=source_command,
    )
    state = "imported" if created else "already present"
    print(f"{state}: import {import_id}, {row_count} normalised rows")
    return 0


def _print_imports(db) -> int:
    rows = list_imports(db)
    if not rows:
        print("No imports recorded.")
        return 0
    print("id\tprovider\treport\trows\timported_at\tsha256")
    for row in rows:
        print(
            f"{row['id']}\t{row['provider']}\t{row['report']}\t{row['row_count']}\t"
            f"{row['imported_at']}\t{row['payload_sha256'][:12]}"
        )
    return 0
