from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from . import account
from .ccusage import CcusageSchemaError, normalise_payload
from .charts import render_value_history
from .doctor import exit_code as doctor_exit_code
from .doctor import run_doctor
from .regression import robust_estimates
from .store import connect, estimate_windows, list_accounts, list_imports, regression_points, save_import
from .timeseries import detect_regime_changes, rolling_values, window_history
from .watcher import WatchTarget, ccusage_command, watch

DEFAULT_DATABASE = Path(os.environ.get("SUBBENCH_DATABASE", Path.home() / ".local" / "share" / "subbench" / "subbench.sqlite3"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subbench", description="Continuously measure API-equivalent value delivered by coding-agent subscriptions.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help=f"SQLite database path (default: {DEFAULT_DATABASE})")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="Create the SQLite database")

    doctor = subcommands.add_parser("doctor", help="Check local dependencies, logs, database and observation freshness")
    doctor.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    watch_parser = subcommands.add_parser("watch", help="Watch local agent logs and collect after changes")
    watch_parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    watch_parser.add_argument("--interval", type=float, default=2.0, help="Filesystem scan interval in seconds (default: 2)")
    watch_parser.add_argument("--debounce", type=float, default=5.0, help="Wait after the latest log write before collecting (default: 5)")
    watch_parser.add_argument("--reconcile", type=float, default=21600.0, help="Maximum seconds between full ccusage reconciliations (default: 21600)")
    watch_parser.add_argument("--runner", choices=("npx", "bunx", "pnpm"), default="npx")
    watch_parser.add_argument("--once", action="store_true")

    report_parser = subcommands.add_parser("report", help="Report current plan value, window history and regime changes")
    report_parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    report_parser.add_argument("--account", default=None, help="Restrict to one account_id (use `subbench accounts` to list)")
    report_parser.add_argument("--scope", choices=("all", "account", "plan"), default="all", help="Rolling/regime scope (default: all)")
    report_parser.add_argument("--json", action="store_true", dest="as_json")
    report_parser.add_argument("--intervals", action="store_true", help="Show raw adjacent-interval estimates")
    report_parser.add_argument("--history", action="store_true", help="Show one robust estimate per reset window")
    report_parser.add_argument("--min-quota-span", type=float, default=5.0, help="Minimum observed quota movement for rolling estimates (default: 5)")

    chart_parser = subcommands.add_parser("chart", help="Plot entitlement-value history in the terminal")
    chart_parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    chart_parser.add_argument("--account", default=None, help="Restrict to one account_id")
    chart_parser.add_argument("--window", choices=("all", "five_hour", "weekly"), default="all")
    chart_parser.add_argument("--width", type=int)
    chart_parser.add_argument("--height", type=int)
    chart_parser.add_argument("--min-quota-span", type=float, default=5.0)

    accounts_parser = subcommands.add_parser("accounts", help="List discovered accounts")
    accounts_parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")

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
    if args.command == "doctor":
        providers = ("claude", "codex") if args.provider == "all" else (args.provider,)
        checks = run_doctor(db, args.database, providers)
        if args.as_json:
            print(json.dumps([check.as_dict() for check in checks], indent=2))
        else:
            print("status\tcheck\tdetail")
            for check in checks:
                print(f"{check.status}\t{check.name}\t{check.detail}")
        return doctor_exit_code(checks)
    if args.command == "imports":
        return _print_imports(db)
    if args.command == "chart":
        provider = None if args.provider == "all" else args.provider
        estimates = robust_estimates(regression_points(db, provider=provider, account_id=args.account))
        rows = [row for row in window_history(estimates) if row["quota_span_percent"] >= args.min_quota_span]
        ok = render_value_history(rows, provider=provider, account_id=args.account, window=None if args.window == "all" else args.window, width=args.width, height=args.height)
        if not ok:
            print("No sufficiently informative reset-window estimates to chart.")
        return 0
    if args.command == "accounts":
        return _print_accounts(db, args.provider)
    if args.command == "report":
        provider = None if args.provider == "all" else args.provider
        return _print_report(db, provider=provider, account_id=args.account, scope=args.scope, as_json=args.as_json, intervals=args.intervals, history=args.history, min_quota_span=args.min_quota_span)
    if args.command == "watch":
        providers = ("claude", "codex") if args.provider == "all" else (args.provider,)
        return watch(db, targets=[WatchTarget(provider=p) for p in providers], runner=args.runner, interval_seconds=args.interval, debounce_seconds=args.debounce, reconcile_seconds=args.reconcile, once=args.once)
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
    account_id = account.active_account_id() if provider == "codex" else None
    import_id, row_count, created = save_import(
        db, raw=raw, payload=payload, rows=rows, provider=provider, report=report,
        command=source_command, account_id=account_id,
    )
    label = account.account_label(account_id) if account_id else "-"
    print(f"{'imported' if created else 'already present'}: import {import_id}, account {label}, {row_count} normalised rows")
    return 0


def _print_accounts(db, provider_arg: str) -> int:
    provider = None if provider_arg == "all" else provider_arg
    rows = list_accounts(db, provider=provider)
    if not rows:
        print("No accounts recorded yet. Run `subbench watch --once` after ensuring each Codex account is active.")
        return 0
    print("account_id\tprovider_or_alias\temail\tplan")
    for row in rows:
        label = row["email"] or row["alias"] or (row["account_id"] or "")[:8]
        print(f"{row['account_id']}\t{label or '-'}\t{row['email'] or '-'}\t{row['plan'] or '-'}")
    return 0


def _print_report(db, *, provider: str | None, account_id: str | None, scope: str, as_json: bool, intervals: bool, history: bool, min_quota_span: float) -> int:
    if intervals:
        rows = [dict(row) for row in estimate_windows(db, provider=provider, account_id=account_id)]
        if as_json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("No usable adjacent intervals yet.")
            return 0
        print("provider\taccount\twindow\tquota delta\tAPI value\timplied full window\tinterval end")
        for row in rows:
            print(f"{row['provider']}\t{account.account_label(row.get('account_id'))}\t{row['window']}\t{row['quota_delta_percent']:.2f}%\tUS${row['api_value_usd']:.4f}\tUS${row['implied_full_window_usd']:.2f}\t{row['observed_at']}")
        return 0

    estimates = robust_estimates(regression_points(db, provider=provider, account_id=account_id))
    if history:
        rows = window_history(estimates)
        if as_json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("No usable reset-window estimates yet.")
            return 0
        print("provider\taccount\twindow\treset\testimate\t80% slope range\tquota span\tobservations")
        for row in rows:
            print(f"{row['provider']}\t{account.account_label(row.get('account_id'))}\t{row['window']}\t{row['reset_key']}\tUS${row['estimate_usd']:.2f}\tUS${row['lower_usd']:.2f}–US${row['upper_usd']:.2f}\t{row['quota_span_percent']:.2f}%\t{row['observation_count']}")
        return 0

    all_current = [row.as_dict() for row in rolling_values(estimates, min_quota_span=min_quota_span)]
    all_changes = [row.as_dict() for row in detect_regime_changes(estimates, min_quota_span=min_quota_span)]
    current = [row for row in all_current if _matches_scope(row, scope)]
    changes = [row for row in all_changes if _matches_scope(row, scope)]
    payload = {"current": current, "regime_changes": changes}
    if as_json:
        print(json.dumps(payload, indent=2))
        return 0
    if not current:
        print("No usable current estimates yet. Keep `subbench watch` running until at least one reset window has meaningful quota movement.")
        return 0

    print("Current API-equivalent entitlement value")
    print("scope\taccount\testimate\trecent range\twindows\tquota evidence\tlatest reset")
    for row in current:
        scope_label = row.get("account_scope", "account")
        account_label = account.account_label(row.get("account_id")) if scope_label == "account" else "all accounts"
        print(f"{scope_label}\t{row['provider']} {row['window']}\tUS${row['estimate_usd']:.2f}\tUS${row['lower_usd']:.2f}–US${row['upper_usd']:.2f}\t{row['window_count']}\t{row['quota_span_percent']:.2f}%\t{row['latest_reset']}\t{account_label}")

    if changes:
        print("\nPossible backend limit changes")
        print("scope\taccount\testimate window\tstatus\tfirst observed\tbaseline\trecent\tchange")
        for row in changes:
            scope_label = row.get("account_scope", "account")
            account_label = account.account_label(row.get("account_id")) if scope_label == "account" else "all accounts"
            print(f"{scope_label}\t{account_label}\t{row['provider']} {row['window']}\t{row['status']}\t{row['first_observed_reset']}\tUS${row['baseline_usd']:.2f}\tUS${row['recent_usd']:.2f}\t{row['change_percent']:+.1f}%")
    return 0


def _matches_scope(row: dict, scope: str) -> bool:
    row_scope = row.get("account_scope", "account")
    if scope == "all":
        return True
    if scope == "account":
        return row_scope == "account"
    if scope == "plan":
        return row_scope == "plan"
    return True


def _print_imports(db) -> int:
    rows = list_imports(db)
    if not rows:
        print("No imports recorded.")
        return 0
    print("id\tprovider\taccount\treport\trows\timported_at\tsha256")
    for row in rows:
        label = account.account_label(row["account_id"]) if row["account_id"] else "-"
        print(f"{row['id']}\t{row['provider']}\t{label}\t{row['report']}\t{row['row_count']}\t{row['imported_at']}\t{row['payload_sha256'][:12]}")
    return 0
