"""Build regression points from evidence contributed by several agents.

ccusage reads one machine's logs. Two machines on one subscription each see only their own
sessions, so the entitlement's true spend is the *sum* across agents, not any one agent's
view. This is the same gap that shows up on a single agent as unobserved usage -- quota
advancing with no local tokens beside it -- except that here the missing evidence exists
and simply lives on another machine.

Merging is therefore per account, not per agent: every agent reporting the same
(provider, account) is describing one meter and one allowance. Pooling across *accounts*
is a different operation and does not belong here -- those are separate entitlements, so
they are combined at the estimate level by `timeseries.rolling_values`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from ..regression import MAX_COST_AGE_MINUTES


def _moment(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _window_bounds(snapshot: Mapping[str, Any]) -> tuple[str | None, str | None]:
    resets_at = snapshot.get("resets_at")
    if not resets_at:
        return None, None
    end = _moment(str(resets_at))
    if end is None:
        return None, None
    minutes = snapshot.get("duration_minutes")
    if minutes is None:
        minutes = 300 if str(snapshot.get("window")) == "five_hour" else 10080
    from datetime import timedelta

    start = end - timedelta(minutes=int(minutes))
    return start.date().isoformat(), end.date().isoformat()


def assemble_points(
    snapshots: Iterable[Mapping[str, Any]],
    imports: Iterable[Mapping[str, Any]],
    *,
    max_cost_age_minutes: float = MAX_COST_AGE_MINUTES,
) -> list[dict[str, Any]]:
    """One regression point per entitlement snapshot, priced across every agent.

    `imports` carries one row per (agent, import, day) with that day's cost, so a window
    bound can be applied without another query. Each agent contributes the newest import
    it had confirmed at or before the snapshot; an agent that has never reported for this
    account contributes nothing rather than zero, since absent evidence is not evidence of
    absent spend.

    The staleness bound is applied to the *oldest* contributing agent. A point is only as
    fresh as its least fresh input, and pairing a current meter reading against one
    machine's hours-old total is what makes quota look free.
    """
    by_account: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in imports:
        key = (str(row["provider"]), str(row.get("account_key") or ""))
        by_account.setdefault(key, []).append(dict(row))
    for rows in by_account.values():
        rows.sort(key=lambda row: str(row["last_seen_at"]))

    points: list[dict[str, Any]] = []
    for snapshot in snapshots:
        provider = str(snapshot["provider"])
        account_key = str(snapshot.get("account_key") or "")
        observed_at = str(snapshot["observed_at"])
        start_date, end_date = _window_bounds(snapshot)

        latest: dict[str, dict[str, Any]] = {}
        for row in by_account.get((provider, account_key), []):
            if str(row["last_seen_at"]) > observed_at:
                break  # sorted, so nothing later can qualify either
            agent = str(row["agent_id"])
            current = latest.get(agent)
            if current is None or str(row["last_seen_at"]) >= str(current["last_seen_at"]):
                if current is None or str(row["import_key"]) != str(current["import_key"]):
                    latest[agent] = {"import_key": row["import_key"], "last_seen_at": row["last_seen_at"], "cost": 0.0}
                    current = latest[agent]
                current["last_seen_at"] = row["last_seen_at"]
            elif str(row["import_key"]) != str(current["import_key"]):
                continue
            in_window = (
                row.get("period_start") is None
                or ((start_date is None or str(row["period_start"]) >= start_date)
                    and (end_date is None or str(row["period_start"]) <= end_date))
            )
            if in_window and str(row["import_key"]) == str(current["import_key"]):
                current["cost"] += float(row["cost_usd"] or 0.0)

        if not latest:
            continue

        observed = _moment(observed_at)
        ages = []
        for entry in latest.values():
            seen = _moment(str(entry["last_seen_at"]))
            ages.append((observed - seen).total_seconds() / 60.0 if observed and seen else 0.0)
        # As fresh as the least fresh contributor.
        cost_age = max(ages) if ages else 0.0
        if cost_age > max_cost_age_minutes:
            continue

        points.append({
            "provider": provider,
            "account_id": snapshot.get("account_id"),
            "window": snapshot["window"],
            "observed_at": observed_at,
            "used_percent": float(snapshot["used_percent"]),
            "resets_at": snapshot.get("resets_at"),
            "duration_minutes": snapshot.get("duration_minutes"),
            "cost_usd": sum(entry["cost"] for entry in latest.values()),
            "cost_age_minutes": cost_age,
            "contributing_agents": len(latest),
        })
    return points


SNAPSHOTS_SQL = """
SELECT provider, account_id, account_key, window, observed_at, used_percent,
       resets_at, duration_minutes
FROM entitlement_snapshots
ORDER BY provider, account_key, window, resets_at, observed_at
"""

IMPORT_DAYS_SQL = """
SELECT agent_id, import_key, provider, account_key, last_seen_at, period_start,
       SUM(CAST(COALESCE(reported_cost_usd, '0') AS REAL)) AS cost_usd
FROM usage_rows
GROUP BY agent_id, import_key, provider, account_key, last_seen_at, period_start
ORDER BY last_seen_at
"""
