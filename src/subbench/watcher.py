from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from . import account
from .ccusage import CcusageSchemaError, compare_daily_tokens, normalise_payload
from .entitlement import MeterDeferred, collect_entitlements, meter_retry_in
from .incremental import AuthFileDetector, LogChangeDetector
from .logs import LogReader, raw as logs_raw
from .push import build_reports, push_all
from .store import dropped_periods, plan_moves, save_entitlements, save_hourly, save_import, upsert_account


@dataclass(frozen=True)
class WatchTarget:
    provider: str
    report: str = "daily"


RUNNERS = ("native", "npx", "bunx", "pnpm")

# One reader per provider for the life of the process, so each pass parses only what the
# agents appended since the last one.
_READERS: dict[str, LogReader] = {}


def reader(provider: str) -> LogReader:
    if provider not in _READERS:
        _READERS[provider] = LogReader(provider)
    return _READERS[provider]


def native_report(provider: str, report: str) -> tuple[bytes, Any]:
    if report != "daily":
        raise ValueError(f"the built-in reader produces daily reports; use --runner npx for a {report} report")
    payload = reader(provider).daily()
    return logs_raw(payload), payload


def ccusage_command(*, runner: str, provider: str, report: str) -> list[str]:
    if runner == "npx":
        prefix = ["npx", "--yes", "ccusage@latest"]
    elif runner == "bunx":
        prefix = ["bunx", "ccusage"]
    elif runner == "pnpm":
        prefix = ["pnpm", "dlx", "ccusage"]
    else:
        raise ValueError(f"unsupported runner: {runner}")
    return [*prefix, provider, report, "--json"]


CCUSAGE_TIMEOUT_SECONDS = 120
CCUSAGE_CHECK_SECONDS = 3600
_CCUSAGE_CHECKS: dict[str, float] = {}


def ccusage_executable() -> str | None:
    value = os.environ.get("SUBBENCH_CCUSAGE")
    if value is not None:
        if value.lower() in {"off", "0", "false", "no"}:
            return None
        return value if os.path.isfile(value) and os.access(value, os.X_OK) else None
    return shutil.which("ccusage")


def _run_ccusage(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
    options = {"timeout": timeout} if timeout is not None else {}
    return subprocess.run(command, check=False, capture_output=True, **options)


def _check_ccusage(db, *, provider: str, payload: Any, messages: list[str]) -> None:
    try:
        exe = ccusage_executable()
        if exe is None:
            return
        now = time.monotonic()
        if provider in _CCUSAGE_CHECKS and now - _CCUSAGE_CHECKS[provider] < CCUSAGE_CHECK_SECONDS:
            return
        _CCUSAGE_CHECKS[provider] = now
        check = {"at": datetime.now(timezone.utc).isoformat(), "version": None,
                 "days": 0, "differing": [], "error": None}
        try:
            result = _run_ccusage([exe, provider, "daily", "--json", "--offline"], timeout=CCUSAGE_TIMEOUT_SECONDS)
            if result.returncode:
                error = result.stderr.decode("utf-8", errors="replace").strip()
                raise ValueError(error or f"ccusage exited with status {result.returncode}")
            check["days"], check["differing"] = compare_daily_tokens(payload, json.loads(result.stdout))
        except Exception as error:  # The backup must not stop quota collection.
            check["error"] = str(error).strip().split("\n")[0][:300] or type(error).__name__
        # regression_points prices each quota reading from the newest provider import.
        # Save only the check here. Two imports would alternate the price source.
        with db:
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"ccusage.check.{provider}", json.dumps(check)),
            )
        if check["error"]:
            messages.append(f"ccusage check failed: {check['error']}")
        elif check["differing"]:
            messages.append(f"ccusage differs on {len(check['differing'])} day(s): "
                            + ", ".join(check["differing"][:3]))
        else:
            messages.append(f"ccusage agrees ({check['days']} days)")
    except Exception as error:  # Include detection and database failures.
        reason = str(error).strip().split("\n")[0][:300] or type(error).__name__
        messages.append(f"ccusage check failed: {reason}")


TRUNCATION_RETRIES = 2

# Pushing is not collection. It runs on its own slow clock so a slow or unreachable
# server can never delay a quota reading, and a failure is reported without failing
# the cycle -- the local database remains the source of truth either way. A cycle with
# nothing pending costs one indexed query and sends no request.
PUSH_INTERVAL_SECONDS = 1800.0


def refresh_if_due(db, *, last_refreshed: float, now: float, emit) -> float:
    """Re-derive the measurements, then send them if there is anywhere to send them.

    Deriving happens whether or not pushing is configured, and whether or not there is
    anything new to send. It used to happen only as a side effect of a push that had rows
    to carry, which left the stored measurements silently hours behind the readings on a
    machine that was collecting and pushing perfectly well.
    """
    # Zero means nothing has been derived in this run yet, which is due immediately. The
    # clock cannot stand in for that: it counts from boot, so on a machine started minutes
    # ago every interval looks as though it has only just begun.
    if last_refreshed > 0 and now - last_refreshed < PUSH_INTERVAL_SECONDS:
        return last_refreshed
    try:
        reports = build_reports(db)
    except Exception as error:  # noqa: BLE001 - collection must survive any derivation failure
        emit(f"{datetime.now(timezone.utc).isoformat()} derive failed: {error}")
        return now

    url = os.environ.get("SUBBENCH_PUSH_URL")
    token = os.environ.get("SUBBENCH_PUSH_TOKEN")
    if not url or not token:
        return now
    try:
        result = push_all(db, url=url, token=token, reports=reports)
        if result.sent_entitlements or result.sent_usage or not result.drained:
            emit(f"{datetime.now(timezone.utc).isoformat()} push: {result.message}")
    except Exception as error:  # noqa: BLE001 - collection must survive any push failure
        emit(f"{datetime.now(timezone.utc).isoformat()} push failed: {error}")
    return now


def collect_target(db, *, target: WatchTarget, runner: str) -> tuple[bool, str]:
    messages: list[str] = []
    failed = False
    account_id = (account.active_account_id() if target.provider == "codex"
                  else account.active_claude_account_id() if target.provider == "claude" else None)

    if runner == "native":
        try:
            raw, payload = native_report(target.provider, target.report)
        except (OSError, CcusageSchemaError, ValueError) as error:
            exe = ccusage_executable()
            failed = True
            if exe is not None:
                failed = _collect_ccusage(
                    db, target=target, command=[exe, target.provider, target.report, "--json", "--offline"],
                    account_id=account_id, messages=messages, timeout=CCUSAGE_TIMEOUT_SECONDS,
                )
            if failed:
                messages.append(f"usage error: {error}")
            else:
                messages.append(f"usage from ccusage (built-in reader failed: {error})")
        else:
            try:
                rows = normalise_payload(payload, provider=target.provider, report=target.report) if payload["daily"] else []
                if rows:
                    import_id, row_count, created = save_import(
                        db, raw=raw, payload=payload, rows=rows, provider=target.provider, report=target.report,
                        command=f"subbench {target.provider} logs", account_id=account_id,
                    )
                    messages.append(f"usage {'recorded' if created else 'unchanged'} import {import_id} ({row_count} rows)")
                else:
                    messages.append("usage: no local sessions yet")
                hours = save_hourly(db, provider=target.provider, account_id=account_id,
                                    buckets=reader(target.provider).hourly(), now=datetime.now(timezone.utc).isoformat())
                if hours:
                    messages.append(f"{hours} hour(s) updated")
                unknown = sorted(reader(target.provider).prices.unknown) if reader(target.provider).prices else []
                if unknown:
                    messages.append(f"no price for {', '.join(unknown)}")
            except (OSError, CcusageSchemaError, ValueError) as error:
                messages.append(f"usage error: {error}")
                failed = True
            else:
                _check_ccusage(db, provider=target.provider, payload=payload, messages=messages)
    else:
        command = ccusage_command(runner=runner, provider=target.provider, report=target.report)
        failed = _collect_ccusage(db, target=target, command=command, account_id=account_id, messages=messages)

    try:
        entitlements = collect_entitlements(target.provider)
        observed_at = datetime.now(timezone.utc).isoformat()
        messages.extend(plan_moves(db, entitlements))
        count = save_entitlements(db, entitlements, observed_at)
        cached = any(row.source == "claude-code-cache" for row in entitlements)
        messages.append(f"entitlement {count} window(s)" + (" from Claude Code's cached meter" if cached else ""))
    except MeterDeferred as error:
        messages.append(f"entitlement deferred: {error}")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        messages.append(f"entitlement unavailable: {error}")

    if account_id and target.provider == "codex":
        registry = account.lookup_account(account_id)
        if registry is not None:
            upsert_account(db, registry)
            label = registry.email or registry.alias or account_id
            messages.append(f"account {label}")
        else:
            upsert_account(db, account.Account(account_id=account_id))
            messages.append(f"account {account_id[:8]}")

    return not failed, f"{target.provider}: " + "; ".join(messages)


def _collect_ccusage(db, *, target: WatchTarget, command: list[str], account_id: str | None,
                     messages: list[str], timeout: int | None = None) -> bool:
    """Run ccusage and save its report, with retries for absent periods."""
    try:
        result = _run_ccusage(command, timeout=timeout)
        if result.returncode != 0:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            messages.append(error or f"ccusage exited with status {result.returncode}")
            return True
        payload = json.loads(result.stdout)
        rows = normalise_payload(payload, provider=target.provider, report=target.report)
        # ccusage sometimes returns fewer days than its own previous run. Retry a
        # bounded number of times rather than storing a report known to be short.
        for _ in range(TRUNCATION_RETRIES):
            dropped = dropped_periods(db, provider=target.provider, account_id=account_id, rows=rows)
            if not dropped:
                break
            messages.append(f"retrying, ccusage dropped {len(dropped)} period(s)")
            retry = _run_ccusage(command, timeout=timeout)
            if retry.returncode != 0:
                break
            result = retry
            payload = json.loads(retry.stdout)
            rows = normalise_payload(payload, provider=target.provider, report=target.report)
        else:
            messages.append("ccusage still short after retries; storing anyway")
        import_id, row_count, created = save_import(
            db, raw=result.stdout, payload=payload, rows=rows,
            provider=target.provider, report=target.report,
            command=" ".join(command), account_id=account_id,
        )
        messages.append(f"usage {'recorded' if created else 'unchanged'} import {import_id} ({row_count} rows)")
    except (OSError, subprocess.TimeoutExpired) as error:
        if timeout is None:
            raise
        messages.append(f"usage error: {error}")
        return True
    except (UnicodeDecodeError, CcusageSchemaError, ValueError) as error:
        messages.append(f"usage error: {error}")
        return True
    return False


def watch(
    db,
    *,
    targets: Sequence[WatchTarget],
    runner: str,
    interval_seconds: float,
    once: bool = False,
    debounce_seconds: float = 60.0,
    reconcile_seconds: float = 21600.0,
    emit: Callable[[str], None] | None = None,
) -> int:
    """Watch local logs cheaply and collect only after changes or reconciliation."""
    if emit is None:
        # Under systemd or launchd stdout is a pipe, which Python buffers in blocks, so the
        # service log stayed empty for hours unless each line is flushed.
        def emit(message: str) -> None:
            print(message, flush=True)

    if interval_seconds <= 0 or debounce_seconds < 0 or reconcile_seconds <= 0:
        raise ValueError("watch intervals must be positive")

    detectors = {target.provider: LogChangeDetector(target.provider) for target in targets}
    auth_detectors = {target.provider: AuthFileDetector(target.provider) for target in targets}
    pending_since: dict[str, float] = {}
    pending_debounce: dict[str, float] = {}
    last_collected: dict[str, float] = {target.provider: 0.0 for target in targets}
    # Zero rather than now, so the first cycle derives instead of leaving the stored
    # measurements half an hour behind from the moment the collector starts.
    last_refreshed = 0.0

    # Prime the auth detectors so a switch at runtime is reported as a change,
    # without treating the initial on-disk state as one.
    for target in targets:
        auth_detectors[target.provider].scan()

    while True:
        failed = False
        now = time.monotonic()
        for target in targets:
            provider = target.provider
            if detectors[provider].scan():
                pending_since.setdefault(provider, now)
                pending_debounce[provider] = debounce_seconds
            if auth_detectors[provider].scan():
                # codex-auth switch observed: capture immediately, regardless of log activity.
                pending_since[provider] = now
                pending_debounce[provider] = 0.0
                emit(f"{datetime.now(timezone.utc).isoformat()} {provider}: active-account change detected, reconciling")

            debounce = pending_debounce.get(provider, debounce_seconds)
            due_to_change = provider in pending_since and now - pending_since[provider] >= debounce
            due_to_reconcile = now - last_collected[provider] >= reconcile_seconds
            if once or due_to_change or due_to_reconcile:
                ok, message = collect_target(db, target=target, runner=runner)
                emit(f"{datetime.now(timezone.utc).isoformat()} {message}")
                failed = failed or not ok
                last_collected[provider] = now
                pending_since.pop(provider, None)
                pending_debounce.pop(provider, None)
                retry = meter_retry_in(provider)
                if retry is not None:
                    pending_since[provider] = now
                    pending_debounce[provider] = retry

        last_refreshed = refresh_if_due(db, last_refreshed=last_refreshed, now=now, emit=emit)

        if once:
            return 1 if failed else 0
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            return 0