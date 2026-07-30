from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from . import account
from .incremental import discover_logs


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_doctor(db: sqlite3.Connection, database_path: Path, providers: Iterable[str]) -> list[Check]:
    checks: list[Check] = []
    checks.append(_executable_check("python", shutil.which("python3") or shutil.which("python")))
    checks.append(_executable_check("ccusage runner", _first_executable("npx", "bunx", "pnpm")))

    selected = tuple(providers)
    if "codex" in selected:
        checks.append(_executable_check("codex", shutil.which("codex"), version_args=("--version",)))
        checks.append(_codex_auth_check())
    if "claude" in selected:
        checks.append(_executable_check("claude", shutil.which("claude"), version_args=("--version",)))
        helper = os.environ.get("SUBBENCH_CLAUDE_USAGE_COMMAND")
        # Without the helper, Claude token costs accumulate but no quota is ever
        # observed, so no Claude estimate can exist. That is a hard failure, not a
        # warning to be lost in one journal line per collection cycle.
        checks.append(Check(
            "Claude entitlement helper",
            "ok" if helper else "error",
            helper or "SUBBENCH_CLAUDE_USAGE_COMMAND is not set; Claude usage is priced but never valued",
        ))

    checks.append(_database_check(db, database_path))
    for provider in selected:
        logs = list(discover_logs(provider))
        checks.append(Check(f"{provider} logs", "ok" if logs else "warn", f"{len(logs)} JSONL files detected" if logs else "no JSONL logs detected"))
        checks.extend(_freshness_checks(db, provider))
        unobserved = _unobserved_quota_check(db, provider)
        if unobserved is not None:
            checks.append(unobserved)
    return checks


def exit_code(checks: Iterable[Check]) -> int:
    return 1 if any(check.status == "error" for check in checks) else 0


def _first_executable(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _executable_check(name: str, path: str | None, version_args: tuple[str, ...] = ()) -> Check:
    if not path:
        return Check(name, "error", "not found on PATH")
    detail = path
    if version_args:
        try:
            result = subprocess.run((path, *version_args), check=False, capture_output=True, text=True, timeout=5)
            version = (result.stdout or result.stderr).strip().splitlines()
            if version:
                detail = f"{path} ({version[0]})"
        except (OSError, subprocess.TimeoutExpired):
            detail = f"{path} (version check failed)"
    return Check(name, "ok", detail)


def _database_check(db: sqlite3.Connection, path: Path) -> Check:
    try:
        db.execute("SELECT 1").fetchone()
        db.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as error:
        return Check("database", "error", str(error))
    size = path.stat().st_size if path.exists() else 0
    return Check("database", "ok", f"{path} ({size} bytes)")


def _freshness_checks(db: sqlite3.Connection, provider: str) -> list[Check]:
    usage = db.execute("SELECT MAX(imported_at) FROM imports WHERE provider = ?", (provider,)).fetchone()[0]
    quota = db.execute("SELECT MAX(observed_at) FROM entitlement_snapshots WHERE provider = ?", (provider,)).fetchone()[0]
    checks = [
        Check(f"{provider} latest usage", "ok" if usage else "warn", usage or "none recorded"),
        Check(f"{provider} latest entitlement", "ok" if quota else "warn", quota or "none recorded"),
    ]
    if usage and not quota:
        # Half-instrumented: the numerator is accumulating and the denominator never
        # will, which looks healthy in the logs but can never produce an estimate.
        checks.append(Check(
            f"{provider} valuation",
            "error",
            "usage is recorded but no entitlement snapshot exists, so no estimate can be produced",
        ))
    if provider == "codex":
        distinct = db.execute(
            "SELECT COUNT(DISTINCT account_id) FROM entitlement_snapshots WHERE provider = ? AND account_id IS NOT NULL",
            (provider,),
        ).fetchone()[0]
        checks.append(Check("codex distinct accounts", "ok" if distinct else "warn", str(distinct) if distinct else "no account-tagged snapshots yet"))
    return checks


def _unobserved_quota_check(db: sqlite3.Connection, provider: str) -> Check | None:
    """Quota that moved while locally recorded spend did not.

    ccusage only sees this machine's logs. Usage from a provider's web or cloud runner,
    or from another machine on the same account, still burns quota but leaves no local
    tokens, so those stretches make the entitlement look far cheaper than it is. They
    cannot be corrected for, only reported.
    """
    from .store import regression_points

    rows = sorted(
        (dict(row) for row in regression_points(db, provider=provider)),
        key=lambda row: str(row["observed_at"]),
    )
    worst_quota = 0.0
    for left, right in zip(rows, rows[1:]):
        if str(left["resets_at"]) != str(right["resets_at"]):
            continue
        quota_delta = float(right["used_percent"]) - float(left["used_percent"])
        value_delta = float(right["cost_usd"]) - float(left["cost_usd"])
        # Quota moving several points while spend barely moves is not a cheap window.
        if quota_delta >= 5.0 and value_delta < quota_delta * 0.01:
            worst_quota = max(worst_quota, quota_delta)
    if worst_quota <= 0:
        return None
    return Check(
        f"{provider} unobserved usage",
        "warn",
        f"{worst_quota:.0f} percentage points of quota moved with almost no locally recorded "
        "spend; that stretch and every pair spanning it is excluded, so the window rests "
        "on less evidence than its quota span suggests",
    )


def _codex_auth_check() -> Check:
    active = account.active_account_id()
    if not active:
        return Check("codex active account", "warn", "no auth.json or no tokens.account_id")
    info = account.lookup_account(active)
    if info and info.email:
        return Check("codex active account", "ok", info.email)
    return Check("codex active account", "ok", active[:8])
