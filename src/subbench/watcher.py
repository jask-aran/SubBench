from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from . import account
from .ccusage import CcusageSchemaError, normalise_payload
from .entitlement import collect_entitlements
from .incremental import AuthFileDetector, LogChangeDetector
from .push import push_all
from .store import dropped_periods, save_entitlements, save_import, upsert_account


@dataclass(frozen=True)
class WatchTarget:
    provider: str
    report: str = "daily"


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


TRUNCATION_RETRIES = 2

# Pushing is not collection. It runs on its own slow clock so a slow or unreachable
# server can never delay a quota reading, and a failure is reported without failing
# the cycle -- the local database remains the source of truth either way.
PUSH_INTERVAL_SECONDS = 3600.0


def push_if_due(db, *, last_pushed: float, now: float, emit) -> float:
    url = os.environ.get("SUBBENCH_PUSH_URL")
    token = os.environ.get("SUBBENCH_PUSH_TOKEN")
    if not url or not token or now - last_pushed < PUSH_INTERVAL_SECONDS:
        return last_pushed
    try:
        result = push_all(db, url=url, token=token)
        if result.sent_entitlements or result.sent_usage or not result.drained:
            emit(f"{datetime.now(timezone.utc).isoformat()} push: {result.message}")
    except Exception as error:  # noqa: BLE001 - collection must survive any push failure
        emit(f"{datetime.now(timezone.utc).isoformat()} push failed: {error}")
    return now


def collect_target(db, *, target: WatchTarget, runner: str) -> tuple[bool, str]:
    messages: list[str] = []
    failed = False
    command = ccusage_command(runner=runner, provider=target.provider, report=target.report)

    account_id = account.active_account_id() if target.provider == "codex" else None

    result = subprocess.run(command, check=False, capture_output=True)
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        messages.append(error or f"ccusage exited with status {result.returncode}")
        failed = True
    else:
        try:
            payload = json.loads(result.stdout)
            rows = normalise_payload(payload, provider=target.provider, report=target.report)
            # ccusage sometimes returns fewer days than its own previous run. Retry a
            # bounded number of times rather than storing a report known to be short.
            for _ in range(TRUNCATION_RETRIES):
                dropped = dropped_periods(db, provider=target.provider, account_id=account_id, rows=rows)
                if not dropped:
                    break
                messages.append(f"retrying, ccusage dropped {len(dropped)} period(s)")
                retry = subprocess.run(command, check=False, capture_output=True)
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
            state = "recorded" if created else "unchanged"
            messages.append(f"usage {state} import {import_id} ({row_count} rows)")
        except (json.JSONDecodeError, UnicodeDecodeError, CcusageSchemaError, ValueError) as error:
            messages.append(f"usage error: {error}")
            failed = True

    try:
        entitlements = collect_entitlements(target.provider)
        observed_at = datetime.now(timezone.utc).isoformat()
        count = save_entitlements(db, entitlements, observed_at)
        messages.append(f"entitlement {count} window(s)")
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


def watch(
    db,
    *,
    targets: Sequence[WatchTarget],
    runner: str,
    interval_seconds: float,
    once: bool = False,
    debounce_seconds: float = 60.0,
    reconcile_seconds: float = 21600.0,
    emit: Callable[[str], None] = print,
) -> int:
    """Watch local logs cheaply and run ccusage only after changes or reconciliation."""
    if interval_seconds <= 0 or debounce_seconds < 0 or reconcile_seconds <= 0:
        raise ValueError("watch intervals must be positive")

    detectors = {target.provider: LogChangeDetector(target.provider) for target in targets}
    auth_detectors = {target.provider: AuthFileDetector(target.provider) for target in targets}
    pending_since: dict[str, float] = {}
    pending_debounce: dict[str, float] = {}
    last_collected: dict[str, float] = {target.provider: 0.0 for target in targets}
    last_pushed = time.monotonic()

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

        last_pushed = push_if_due(db, last_pushed=last_pushed, now=now, emit=emit)

        if once:
            return 1 if failed else 0
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            return 0