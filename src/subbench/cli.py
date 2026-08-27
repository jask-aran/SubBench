from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import account, config
from .ccusage import CcusageSchemaError, normalise_payload
from .charts import render_product_series, render_value_history
from .doctor import exit_code as doctor_exit_code
from .doctor import run_doctor
from .crosssolve import account_plans, combined_estimates, divergences
from .push import push_all, value_report
from .weights import observations_from_windows, solve
from .regression import MIN_QUOTA_DELTA_PERCENT, _cluster_resets, robust_estimates
from .store import connect, estimate_windows, list_accounts, list_imports, model_mix, regression_points, save_import
from .timeseries import detect_regime_changes, rolling_values, window_history
from .watcher import WatchTarget, ccusage_command, watch

DEFAULT_DATABASE = Path(os.environ.get("SUBBENCH_DATABASE", Path.home() / ".local" / "share" / "subbench" / "subbench.sqlite3"))


# Old names, kept working so nothing already scripted breaks. Rewritten before parsing so
# the parser itself stays the shape the help output describes.
ALIASES = {
    "push": ("sync", "push"),
    "imports": ("data", "imports"),
    "ingest": ("data", "import"),
    "init": ("data", "init"),
    "models": ("detail", "models"),
    "weights": ("detail", "weights"),
    "accounts": ("detail", "accounts"),
    "report": ("values",),
}


# Global options that consume the token after them, so their value is not mistaken for
# the command name.
GLOBAL_OPTIONS_WITH_VALUES = {"--database"}


def expand_aliases(argv: Sequence[str]) -> list[str]:
    argv = list(argv)
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in GLOBAL_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return [*argv[:index], *ALIASES.get(token, (token,)), *argv[index + 1:]]
    return argv


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--product", default=None, help="Match part of a product name, e.g. 'Claude' or 'ChatGPT Plus'")
    parser.add_argument("--account", default=None, help="Match the start of an account id")
    parser.add_argument("--window", choices=("all", "five_hour", "weekly"), default="all")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subbench", description="Continuously measure API-equivalent value delivered by coding-agent subscriptions.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help=f"SQLite database path (default: {DEFAULT_DATABASE})")
    subcommands = parser.add_subparsers(dest="command", required=True)

    status = subcommands.add_parser("status", help="What is being measured, and is collection healthy")
    status.add_argument("--json", action="store_true", dest="as_json")

    values = subcommands.add_parser("values", help="The individual window measurements")
    _add_filters(values)
    values.add_argument("--tier", choices=("all", "confirmed", "likely", "provisional"), default="all",
                        help="Restrict to one confidence tier (default: all)")
    values.add_argument("--state", choices=("all", "completed", "open"), default="all",
                        help="Whether the window has reset yet (default: all)")
    values.add_argument("--converted", action="store_true",
                        help="Include short windows restated in long-window terms")
    values.add_argument("--json", action="store_true", dest="as_json")

    chart_parser = subcommands.add_parser("chart", help="Plot the same pooled series the site plots")
    _add_filters(chart_parser)
    chart_parser.add_argument("--width", type=int)
    chart_parser.add_argument("--height", type=int)

    watch_parser = subcommands.add_parser("watch", help="Watch local agent logs and collect after changes")
    watch_parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    watch_parser.add_argument("--interval", type=float, default=2.0, help="Filesystem scan interval in seconds (default: 2)")
    watch_parser.add_argument("--debounce", type=float, default=60.0, help="Wait seconds after the latest log write before collecting (default: 60)")
    watch_parser.add_argument("--reconcile", type=float, default=21600.0, help="Maximum seconds between full ccusage reconciliations (default: 21600)")
    watch_parser.add_argument("--runner", choices=("npx", "bunx", "pnpm"), default="npx")
    watch_parser.add_argument("--once", action="store_true")

    collect = subcommands.add_parser("collect", help="Take one ccusage snapshot now")
    collect.add_argument("provider", choices=("claude", "codex"))
    collect.add_argument("--report", default="daily")
    collect.add_argument("--runner", choices=("npx", "bunx", "pnpm"), default="npx")
    collect.add_argument("ccusage_args", nargs=argparse.REMAINDER)

    doctor = subcommands.add_parser("doctor", help="Check local dependencies, logs, database and observation freshness")
    doctor.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    sync = subcommands.add_parser("sync", help="Send measurements to a SubBench site")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    sync_push = sync_commands.add_parser("push", help="Send everything not yet acknowledged")
    sync_push.add_argument("--url", default=os.environ.get("SUBBENCH_PUSH_URL"), help="Ingest endpoint (default: $SUBBENCH_PUSH_URL)")
    sync_push.add_argument("--token", default=os.environ.get("SUBBENCH_PUSH_TOKEN"), help="Bearer token (default: $SUBBENCH_PUSH_TOKEN)")
    sync_commands.add_parser("status", help="Endpoint, agent identity, cursor and last error")
    sync_reset = sync_commands.add_parser("reset", help="Take a new agent identity and send everything again")
    sync_reset.add_argument("--yes", action="store_true", help="Required: this replays the whole history to the site")

    data = subcommands.add_parser("data", help="The local database")
    data_commands = data.add_subparsers(dest="data_command", required=True)
    data_commands.add_parser("init", help="Create the database")
    data_commands.add_parser("path", help="Print the database path and size")
    data_commands.add_parser("imports", help="List recorded ccusage imports")
    data_import = data_commands.add_parser("import", help="Import ccusage JSON from a file or stdin")
    data_import.add_argument("path", type=str)
    data_import.add_argument("--provider", choices=("claude", "codex"), required=True)
    data_import.add_argument("--report", default="daily")
    data_backup = data_commands.add_parser("backup", help="Copy the database somewhere safe")
    data_backup.add_argument("destination", type=Path)
    data_reset = data_commands.add_parser("reset", help="Delete every observation and start measuring again")
    data_reset.add_argument("--yes", action="store_true", help="Required: this cannot be undone")
    data_prune = data_commands.add_parser("prune", help="Drop raw usage rows older than a cutoff")
    data_prune.add_argument("--days", type=float, default=90.0, help="Keep imports newer than this (default: 90)")
    data_prune.add_argument("--yes", action="store_true", help="Required: pruned periods can no longer be re-estimated")

    detail = subcommands.add_parser("detail", help="Estimator internals")
    detail_commands = detail.add_subparsers(dest="detail_command", required=True)
    detail_models = detail_commands.add_parser("models", help="The model mix behind each reset window")
    detail_models.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    detail_models.add_argument("--account", default=None)
    detail_models.add_argument("--json", action="store_true", dest="as_json")
    detail_weights = detail_commands.add_parser("weights", help="Fit per-model quota weights, or explain why not yet")
    detail_weights.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    detail_weights.add_argument("--json", action="store_true", dest="as_json")
    detail_accounts = detail_commands.add_parser("accounts", help="List discovered accounts")
    detail_accounts.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    convergence = detail_commands.add_parser(
        "convergence", help="How an estimate settled as evidence arrived, per observation")
    convergence.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    convergence.add_argument("--account", default=None)
    convergence.add_argument("--window", choices=("all", "five_hour", "weekly"), default="all")
    convergence.add_argument("--width", type=int)
    convergence.add_argument("--height", type=int)
    convergence.add_argument("--min-quota-span", type=float, default=5.0)
    convergence.add_argument("--min-pair-delta", type=float, default=MIN_QUOTA_DELTA_PERCENT)
    convergence.add_argument("--slopes", action="store_true", help="Show every valid slope behind the estimate")
    detail_report = detail_commands.add_parser("estimator", help="Raw estimator output: rolling values, regimes, divergences")
    detail_report.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    detail_report.add_argument("--account", default=None)
    detail_report.add_argument("--scope", choices=("all", "account", "plan"), default="all")
    detail_report.add_argument("--json", action="store_true", dest="as_json")
    detail_report.add_argument("--intervals", action="store_true")
    detail_report.add_argument("--history", action="store_true")
    detail_report.add_argument("--min-quota-span", type=float, default=5.0)
    detail_report.add_argument("--min-pair-delta", type=float, default=MIN_QUOTA_DELTA_PERCENT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Before the parser is built: --url and --token take their defaults from the
    # environment, so the file has to be in it by then.
    config.load()
    args = build_parser().parse_args(expand_aliases(sys.argv[1:] if argv is None else argv))
    db = connect(args.database)

    if args.command == "status":
        return _print_status(db, args.database, as_json=args.as_json)
    if args.command == "values":
        return _print_values(db, args)
    if args.command == "chart":
        report = value_report(db)
        drawn = render_product_series(
            report["product_series"],
            window=None if args.window == "all" else args.window,
            product=args.product,
            width=args.width,
            height=args.height,
        )
        if not drawn:
            print("No completed, confirmed window to chart yet. `subbench values` shows what is still gathering.")
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
    if args.command == "watch":
        providers = ("claude", "codex") if args.provider == "all" else (args.provider,)
        return watch(db, targets=[WatchTarget(provider=p) for p in providers], runner=args.runner, interval_seconds=args.interval, debounce_seconds=args.debounce, reconcile_seconds=args.reconcile, once=args.once)
    if args.command == "collect":
        passthrough = args.ccusage_args[1:] if args.ccusage_args and args.ccusage_args[0] == "--" else args.ccusage_args
        command = [*ccusage_command(runner=args.runner, provider=args.provider, report=args.report), *passthrough]
        result = subprocess.run(command, check=False, capture_output=True)
        if result.returncode != 0:
            sys.stderr.buffer.write(result.stderr)
            return result.returncode
        return _ingest(db, raw=result.stdout, provider=args.provider, report=args.report, source_command=" ".join(command))

    if args.command == "sync":
        return _sync(db, args)
    if args.command == "data":
        return _data(db, args)
    if args.command == "detail":
        return _detail(db, args)
    raise AssertionError(f"unhandled command: {args.command}")


def _sync(db, args) -> int:
    if args.sync_command == "push":
        if not args.url or not args.token:
            print(f"no endpoint configured. Set SUBBENCH_PUSH_URL and SUBBENCH_PUSH_TOKEN in "
                  f"{config.config_path()}, or pass --url/--token", file=sys.stderr)
            return 2
        result = push_all(db, url=args.url, token=args.token)
        print(result.message)
        return 0 if result.drained else 1
    if args.sync_command == "status":
        rows = list(db.execute("SELECT * FROM push_state"))
        if not rows:
            print(f"Nothing has been sent yet. Endpoint and token are read from {config.config_path()}.")
            return 0
        for row in rows:
            pending = db.execute(
                "SELECT COUNT(*) FROM entitlement_snapshots WHERE ? IS NULL OR observed_at > ?",
                (row["entitlement_cursor"], row["entitlement_cursor"]),
            ).fetchone()[0]
            print(f"endpoint     {row['endpoint']}")
            print(f"agent        {row['agent_id']}")
            print(f"last sent    {row['last_pushed_at'] or 'never'}")
            print(f"acknowledged {row['entitlement_cursor'] or 'nothing yet'}")
            print(f"pending      {pending} reading(s)")
            print(f"last error   {row['last_error'] or 'none'}")
        return 0
    if args.sync_command == "reset":
        if not args.yes:
            print("This takes a new agent identity and sends the whole history again. Pass --yes.", file=sys.stderr)
            return 2
        with db:
            db.execute("DELETE FROM push_state")
        print("Push state cleared. The next push takes a new agent identity and sends everything.")
        return 0
    raise AssertionError(args.sync_command)


def _data(db, args) -> int:
    if args.data_command == "init":
        print(args.database)
        return 0
    if args.data_command == "path":
        size = args.database.stat().st_size if args.database.exists() else 0
        readings = db.execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0]
        usage = db.execute("SELECT COUNT(*) FROM usage_rows").fetchone()[0]
        print(f"{args.database}\n{size / 1e6:.1f} MB · {readings:,} quota readings · {usage:,} usage rows")
        return 0
    if args.data_command == "imports":
        return _print_imports(db)
    if args.data_command == "import":
        raw = sys.stdin.buffer.read() if args.path == "-" else Path(args.path).read_bytes()
        return _ingest(db, raw=raw, provider=args.provider, report=args.report, source_command=None)
    if args.data_command == "backup":
        destination = args.destination.expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # The SQLite backup API, so a copy taken while the collector is writing is still
        # a consistent database rather than a half-written page.
        with sqlite3.connect(destination) as target:
            db.backup(target)
        print(f"{destination} ({destination.stat().st_size / 1e6:.1f} MB)")
        return 0
    if args.data_command == "reset":
        if not args.yes:
            print("This deletes every observation on this machine and cannot be undone.\n"
                  "Take a copy first with `subbench data backup <path>`, then pass --yes.", file=sys.stderr)
            return 2
        with db:
            for table in ("entitlement_snapshots", "usage_rows", "imports", "accounts", "push_state"):
                db.execute(f"DELETE FROM {table}")
        db.execute("VACUUM")
        print("Every observation deleted. Collection starts again from the next reading.")
        return 0
    if args.data_command == "prune":
        if not args.yes:
            print(f"This drops raw usage older than {args.days:.0f} days. Windows in that period can no\n"
                  "longer be re-estimated, though measurements already derived are kept. Pass --yes.", file=sys.stderr)
            return 2
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        with db:
            rows = db.execute(
                "DELETE FROM usage_rows WHERE import_id IN "
                "(SELECT id FROM imports WHERE COALESCE(last_seen_at, imported_at) < ?)", (cutoff,)
            ).rowcount
            imports = db.execute(
                "DELETE FROM imports WHERE COALESCE(last_seen_at, imported_at) < ?", (cutoff,)
            ).rowcount
        db.execute("VACUUM")
        print(f"Dropped {rows:,} usage rows across {imports:,} imports older than {cutoff[:10]}.")
        return 0
    raise AssertionError(args.data_command)


def _detail(db, args) -> int:
    provider = None if args.provider == "all" else args.provider
    if args.detail_command == "accounts":
        return _print_accounts(db, args.provider)
    if args.detail_command == "convergence":
        # The per-observation curve, which is a different question from `subbench chart`:
        # that plots what each finished window was worth, this plots how one window's
        # estimate moved as its own evidence arrived.
        points = [dict(row) for row in regression_points(db, provider=provider, account_id=args.account)]
        if args.window != "all":
            # Replaying the estimator is cubic in observations, so narrowing first is the
            # difference between seconds and minutes on a long history.
            points = [row for row in points if str(row["window"]) == args.window]
        if len(points) > REPLAY_WARNING_POINTS:
            print(f"Replaying {len(points):,} observations. This can take minutes; narrow it with "
                  "--window, --provider or --account.", file=sys.stderr)
        estimates = robust_estimates(points, min_quota_delta=args.min_pair_delta)
        rows = [row for row in window_history(estimates) if row["quota_span_percent"] >= args.min_quota_span]
        drawn = render_value_history(
            rows, points=points, min_pair_delta=args.min_pair_delta, provider=provider,
            account_id=args.account, window=None if args.window == "all" else args.window,
            width=args.width, height=args.height, show_slopes=args.slopes,
        )
        if not drawn:
            print("No sufficiently informative reset-window estimates to chart.")
        return 0
    if args.detail_command == "estimator":
        return _print_report(db, provider=provider, account_id=args.account, scope=args.scope,
                             as_json=args.as_json, intervals=args.intervals, history=args.history,
                             min_quota_span=args.min_quota_span, min_pair_delta=args.min_pair_delta)
    if args.detail_command == "models":
        rows = [dict(row) for row in model_mix(db, provider=provider, account_id=args.account)]
        if args.as_json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("No model mix recorded yet.")
            return 0
        # Group on the same clustered reset key the estimator uses, so a boundary that
        # jitters across a minute does not show as two separate windows here either.
        clusters = _cluster_resets([str(row["resets_at"]) for row in rows if row["resets_at"]])
        for row in rows:
            row["resets_at"] = clusters.get(str(row["resets_at"]), row["resets_at"])
        totals: dict[tuple, int] = {}
        merged: dict[tuple, dict] = {}
        for row in rows:
            key = (row["provider"], row["account_id"], row["window"], row["resets_at"], row["model"])
            if key in merged:
                merged[key]["total_tokens"] = max(int(merged[key]["total_tokens"]), int(row["total_tokens"]))
            else:
                merged[key] = row
        rows = list(merged.values())
        for row in rows:
            key = (row["provider"], row["account_id"], row["window"], row["resets_at"])
            totals[key] = totals.get(key, 0) + int(row["total_tokens"])
        print("provider\taccount\twindow\treset\tmodel\ttokens\tshare")
        for row in rows:
            key = (row["provider"], row["account_id"], row["window"], row["resets_at"])
            share = 100.0 * int(row["total_tokens"]) / totals[key] if totals[key] else 0.0
            print(f"{row['provider']}\t{account.account_label(row.get('account_id'))}\t{row['window']}\t{row['resets_at']}\t{row['model']}\t{int(row['total_tokens']):,}\t{share:.1f}%")
        return 0
    if args.detail_command == "weights":
        estimates = robust_estimates(regression_points(db, provider=provider))
        mix = [dict(row) for row in model_mix(db, provider=provider)]
        observations = observations_from_windows(estimates, mix)
        names = sorted({row["provider"] for row in observations}) or ([provider] if provider else ["codex", "claude"])
        fits = [solve(observations, provider=name) for name in names]
        if args.as_json:
            print(json.dumps([fit.as_dict() for fit in fits], indent=2))
            return 0
        for fit in fits:
            if not fit.sufficient:
                print(f"{fit.provider}: cannot fit yet - {fit.reason}")
                continue
            print(f"{fit.provider}: fitted over {fit.window_count} windows (residual {fit.residual:.4f})")
            print("model\tquota % per million tokens")
            for model, weight in sorted(zip(fit.models, fit.weights), key=lambda pair: -pair[1]):
                print(f"{model}\t{weight * 1e6:.4f}")
        return 0
    raise AssertionError(args.detail_command)


WINDOW_NAMES = {"weekly": "weekly", "five_hour": "5-hour"}

# Above this many observations, replaying the estimator per observation is slow enough to
# be worth warning about before it starts rather than after.
REPLAY_WARNING_POINTS = 400


def _money(value: Any) -> str:
    value = float(value)
    return f"${value:,.0f}" if value >= 100 else f"${value:,.2f}"


def _ago(stamp: Any) -> str:
    moment = _moment(stamp)
    if moment is None:
        return "never"
    minutes = (datetime.now(timezone.utc) - moment).total_seconds() / 60
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    if minutes < 36 * 60:
        return f"{minutes / 60:.0f} h ago"
    return f"{minutes / 1440:.0f} d ago"


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _matches(row: Mapping[str, Any], args) -> bool:
    if args.product and args.product.lower() not in str(row.get("product", "")).lower():
        return False
    if args.account and not str(row.get("account_id") or "").startswith(args.account):
        return False
    if args.window != "all" and str(row.get("window")) != args.window:
        return False
    return True


def _print_status(db, database: Path, *, as_json: bool) -> int:
    report = value_report(db)
    readings = db.execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0]
    newest = db.execute("SELECT MAX(observed_at) FROM entitlement_snapshots").fetchone()[0]
    push = db.execute("SELECT * FROM push_state LIMIT 1").fetchone()

    if as_json:
        print(json.dumps({
            "database": str(database),
            "readings": readings,
            "newest_reading": newest,
            "last_pushed_at": push["last_pushed_at"] if push else None,
            "last_error": push["last_error"] if push else None,
            **{key: report[key] for key in ("products", "products_likely", "products_open")},
        }, indent=2))
        return 0

    print(f"{readings:,} quota readings · newest {_ago(newest)} · {database}")
    if push:
        pending = db.execute(
            "SELECT COUNT(*) FROM entitlement_snapshots WHERE ? IS NULL OR observed_at > ?",
            (push["entitlement_cursor"], push["entitlement_cursor"]),
        ).fetchone()[0]
        detail = f"last sent {_ago(push['last_pushed_at'])} · {pending} pending"
        if push["last_error"]:
            detail += f" · error: {push['last_error']}"
        print(f"{push['endpoint']} · {detail}")
    else:
        print(f"not sending anywhere · configure in {config.config_path()}")

    settled = {(row["product"], row["window"]): row for row in report["products"]}
    ranged = {(row["product"], row["window"]): row for row in report["products_likely"]}
    running = {(row["product"], row["window"]): row for row in report["products_open"]}
    names = sorted({row["product"] for row in report["windows"]})
    if not names:
        print("\nNothing measured yet.")
        return 0
    for name in names:
        print(f"\n{name}")
        for window in ("weekly", "five_hour"):
            label = WINDOW_NAMES[window]
            row = settled.get((name, window))
            wider = ranged.get((name, window))
            if row:
                line = f"  {label:8} {_money(row['estimate_usd']):>8}  {_money(row['lower_usd'])}–{_money(row['upper_usd'])}"
                line += f"  ({row['window_count']} window(s), {row['account_count']} account(s))"
            elif wider:
                line = f"  {label:8} {'':>8}  {_money(wider['lower_usd'])}–{_money(wider['upper_usd'])}  (range only, not yet settled)"
            else:
                line = f"  {label:8} {'':>8}  not enough evidence yet"
            live = running.get((name, window))
            if live:
                line += f"\n  {'':8} in progress {_money(live['estimate_usd'])}"
                line += f"  {_money(live['lower_usd'])}–{_money(live['upper_usd'])}"
                line += f"  {live['covered_quota_percent']:.0f}% measured"
            print(line)
    return 0


def _print_values(db, args) -> int:
    rows = [row for row in value_report(db)["windows"] if _matches(row, args)]
    if not args.converted:
        rows = [row for row in rows if "~via~" not in str(row["reset_key"])]
    if args.tier != "all":
        rows = [row for row in rows if row["tier"] == args.tier]
    now = datetime.now(timezone.utc)
    for row in rows:
        resets = _moment(row["reset_key"].split("~via~")[0])
        row["state"] = "open" if resets and resets > now else "completed"
    if args.state != "all":
        rows = [row for row in rows if row["state"] == args.state]
    rows.sort(key=lambda row: (row["product"], row["window"], str(row["reset_key"])))

    if args.as_json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print("No measurements match. `subbench values` with no filters shows everything recorded.")
        return 0
    header = f"{'product':14} {'account':10} {'window':8} {'state':9} {'resets':11} {'tier':12} {'value':>9} {'range':>19} {'readings':>8} {'measured':>8}"
    print(header)
    for row in rows:
        account_id = str(row.get("account_id") or "")
        bounds = "-" if not row.get("interval_count") else f"{_money(row['lower_usd'])}–{_money(row['upper_usd'])}"
        print(
            f"{str(row['product'])[:14]:14} {account_id[:8] or '-':10} "
            f"{WINDOW_NAMES.get(str(row['window']), str(row['window'])):8} {row['state']:9} "
            f"{str(row['reset_key'])[:10]:11} {row['tier']:12} {_money(row['estimate_usd']):>9} "
            f"{bounds:>19} {int(row.get('interval_count') or 0):>8} {float(row.get('covered_quota_percent') or 0):>7.0f}%"
        )
    print(f"\n{len(rows)} measurement(s). Reasons: `subbench values --json`.")
    return 0


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
        # Claude snapshots carry no account_id; a bare "None" row is noise, not an account.
        if not row["account_id"]:
            continue
        label = row["email"] or row["alias"] or (row["account_id"] or "")[:8]
        print(f"{row['account_id']}\t{label or '-'}\t{row['email'] or '-'}\t{row['plan'] or '-'}")
    return 0


def _print_report(db, *, provider: str | None, account_id: str | None, scope: str, as_json: bool, intervals: bool, history: bool, min_quota_span: float, min_pair_delta: float) -> int:
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

    points = [dict(row) for row in regression_points(db, provider=provider, account_id=account_id)]
    # Short windows expressed in long-window terms join the pool: two meters measuring one
    # subscription are two measurements of it, and the short one turns over far more often.
    estimates, window_ratio_rows = combined_estimates(
        points, robust_estimates(points, min_quota_delta=min_pair_delta)
    )
    if history:
        rows = window_history(estimates)
        if as_json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("No usable reset-window estimates yet.")
            return 0
        print("provider\taccount\twindow\treset\twindow avg\tmarginal\t80% slope range\tquota span\tcoverage\tobservations")
        for row in rows:
            marginal = row.get("marginal_usd")
            marginal_label = f"US${marginal:.2f}" if marginal is not None else "-"
            print(f"{row['provider']}\t{account.account_label(row.get('account_id'))}\t{row['window']}\t{row['reset_key']}\tUS${row['estimate_usd']:.2f}\t{marginal_label}\tUS${row['lower_usd']:.2f}–US${row['upper_usd']:.2f}\t{row['quota_span_percent']:.2f}%\t{row.get('coverage_percent', 100.0):.0f}%\t{row['observation_count']}")
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
    print("scope\taccount\twindow avg\tmarginal\trecent range\twindows\tquota evidence\tcoverage\tlatest reset")
    for row in current:
        scope_label = row.get("account_scope", "account")
        account_label = account.account_label(row.get("account_id")) if scope_label == "account" else "all accounts"
        marginal = row.get("marginal_usd")
        marginal_label = f"US${marginal:.2f}" if marginal is not None else "-"
        print(f"{scope_label}\t{row['provider']} {row['window']}\tUS${row['estimate_usd']:.2f}\t{marginal_label}\tUS${row['lower_usd']:.2f}–US${row['upper_usd']:.2f}\t{row['window_count']}\t{row['quota_span_percent']:.2f}%\t{row.get('coverage_percent', 100.0):.0f}%\t{row['latest_reset']}\t{account_label}")

    found = divergences(estimates, window_ratio_rows, account_plans(db))
    if found:
        print("\nIndependent measurements disagreeing")
        print("scope\tprovider\tsubject\tdifference\tdetail")
        for row in found:
            print(f"{row.scope}\t{row.provider}\t{row.subject}\t{row.difference:+.1%}\t{row.detail}")

    if window_ratio_rows:
        print("\nWindow conversion")
        for ratio in window_ratio_rows:
            print(f"{ratio.provider}: {1 / ratio.ratio:.2f} {ratio.short_window} entitlements per {ratio.long_window} "
                  f"(from {ratio.short_quota_percent:.0f}% vs {ratio.long_quota_percent:.0f}% observed movement)")

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
