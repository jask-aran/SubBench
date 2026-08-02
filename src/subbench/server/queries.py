"""Turn stored rows into estimates, using the estimator the CLI uses.

Nothing here re-implements derivation. The SQL reproduces the local pipeline's numerator
-- window-bounded cost sums, paired against a cost total confirmed near the quota reading
-- and hands the result to `robust_estimates` unchanged. The functions are synchronous and
take plain row dicts, so the same code path is exercised by tests against SQLite and by
the Worker against D1.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..regression import robust_estimates
from ..weights import observations_from_windows, solve
from ..regression import MAX_COST_AGE_MINUTES
from ..timeseries import detect_regime_changes, rolling_values, window_history
from .assemble import IMPORT_DAYS_SQL, SNAPSHOTS_SQL, assemble_points
from .confidence import annotate, classify

# Mirrors store.PRICED_WINDOWS_SQL, adapted for the agent-scoped D1 tables. Kept as one
# statement so the numerator rules stay in a single place per storage backend.
REGRESSION_POINTS_SQL = """
WITH scoped AS (
    SELECT e.agent_id, e.provider, e.account_id, e.account_key, e.window, e.observed_at,
           e.used_percent, e.resets_at, e.duration_minutes,
           (SELECT u.import_key FROM usage_rows u
             WHERE u.agent_id = e.agent_id AND u.provider = e.provider
               AND u.account_key = e.account_key AND u.last_seen_at <= e.observed_at
             ORDER BY u.last_seen_at DESC LIMIT 1) AS import_key,
           (SELECT (unixepoch(e.observed_at) - unixepoch(MAX(u.last_seen_at))) / 60.0
              FROM usage_rows u
             WHERE u.agent_id = e.agent_id AND u.provider = e.provider
               AND u.account_key = e.account_key AND u.last_seen_at <= e.observed_at
           ) AS cost_age_minutes,
           DATE(e.resets_at, '-' || COALESCE(
               e.duration_minutes,
               CASE e.window WHEN 'five_hour' THEN 300 ELSE 10080 END
           ) || ' minutes') AS window_start_date,
           DATE(e.resets_at) AS window_end_date
    FROM entitlement_snapshots e
)
SELECT s.provider, s.account_id, s.window, s.observed_at, s.used_percent,
       s.resets_at, s.duration_minutes, s.cost_age_minutes,
       (SELECT COALESCE(SUM(CAST(COALESCE(u.reported_cost_usd, '0') AS REAL)), 0.0)
          FROM usage_rows u
         WHERE u.agent_id = s.agent_id AND u.import_key = s.import_key
           AND (u.period_start IS NULL OR (
                    (s.window_start_date IS NULL OR DATE(u.period_start) >= s.window_start_date)
                AND (s.window_end_date IS NULL OR DATE(u.period_start) <= s.window_end_date)
           ))
       ) AS cost_usd
FROM scoped s
WHERE s.import_key IS NOT NULL
  AND s.cost_age_minutes <= ?
  AND s.agent_id = ?
ORDER BY s.provider, s.account_id, s.window, s.resets_at, s.observed_at
"""

# Per reset window, not per account: the weights fit needs one equation per window, and
# a mix collapsed across windows carries no variation to separate the models with.
MODEL_MIX_SQL = """
WITH windows AS (
    SELECT DISTINCT provider, account_id, account_key, resets_at, window,
           COALESCE(duration_minutes,
                    CASE window WHEN 'five_hour' THEN 300 ELSE 10080 END) AS minutes
    FROM entitlement_snapshots WHERE resets_at IS NOT NULL
), newest AS (
    SELECT w.provider, w.account_id, w.account_key, w.resets_at, w.window,
           DATE(w.resets_at, '-' || w.minutes || ' minutes') AS start_date,
           DATE(w.resets_at) AS end_date,
           u.agent_id, MAX(u.last_seen_at) AS last_seen_at
    FROM windows w JOIN usage_rows u
      ON u.provider = w.provider AND u.account_key = w.account_key
    GROUP BY w.provider, w.account_key, w.resets_at, w.window, u.agent_id
)
SELECT n.provider, n.account_id, n.resets_at, n.window, u.model,
       SUM(u.input_tokens + u.output_tokens + u.cache_read_tokens
           + u.cache_write_tokens + u.reasoning_output_tokens) AS total_tokens
FROM newest n JOIN usage_rows u
  ON u.agent_id = n.agent_id AND u.provider = n.provider
 AND u.account_key = n.account_key AND u.last_seen_at = n.last_seen_at
WHERE u.model IS NOT NULL
  AND (u.period_start IS NULL
       OR (DATE(u.period_start) >= n.start_date AND DATE(u.period_start) <= n.end_date))
GROUP BY n.provider, n.account_id, n.resets_at, u.model
HAVING total_tokens > 0
ORDER BY n.provider, n.resets_at, total_tokens DESC
"""

HEALTH_SQL = """
SELECT (SELECT COUNT(*) FROM entitlement_snapshots) AS entitlement_rows,
       (SELECT COUNT(*) FROM agents) AS agents,
       (SELECT MAX(last_seen) FROM agents) AS last_ingest_at,
       (SELECT COUNT(*) FROM ingest_log WHERE status >= 400) AS rejected
"""


def points_params(agent_id: str, max_cost_age_minutes: float = MAX_COST_AGE_MINUTES) -> list[Any]:
    return [max_cost_age_minutes, agent_id]


def weights_payload(points, mix) -> dict[str, Any]:
    """Per-model quota weights, or why they cannot yet be fitted."""
    estimates = robust_estimates(points)
    observations = observations_from_windows(estimates, mix)
    providers = sorted({str(row["provider"]) for row in observations})
    return {"providers": [solve(observations, provider=name).as_dict() for name in providers]}


def estimates_from_points(points: Iterable[Mapping[str, Any]]):
    return robust_estimates(points)


def current_payload(points: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    estimates = robust_estimates(points)
    rolling = [row.as_dict() for row in rolling_values(estimates)]
    # A rolling value covers one or more windows; take the strongest tier among the
    # windows it actually aggregated rather than re-deriving confidence from a summary.
    by_series: dict[tuple[str, str, str | None], list] = {}
    for estimate in estimates:
        by_series.setdefault((estimate.provider, estimate.window, estimate.account_id), []).append(estimate)
    for row in rolling:
        key = (row["provider"], row["window"], row.get("account_id"))
        members = by_series.get(key, [])
        if members:
            best = max(members, key=lambda item: item.covered_quota_percent)
            row.update(classify(best, estimates).as_dict())
        else:
            row.update({"tier": "provisional", "reason": "no window-level estimate"})
    return {
        "current": rolling,
        "regime_changes": [row.as_dict() for row in detect_regime_changes(estimates)],
    }


def history_payload(points: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    estimates = robust_estimates(points)
    tiers = {
        (row["provider"], row["window"], row.get("account_id"), row["reset_key"]): row
        for row in annotate(estimates)
    }
    rows = []
    for row in window_history(estimates):
        key = (row["provider"], row["window"], row.get("account_id"), row["reset_key"])
        annotated = tiers.get(key, {})
        rows.append({**row, "tier": annotated.get("tier", "provisional"), "reason": annotated.get("reason", "")})
    return {"windows": rows}


def models_payload(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    records = [dict(row) for row in rows]
    totals: dict[tuple[str, str | None], int] = {}
    for row in records:
        key = (row["provider"], row.get("account_id"))
        totals[key] = totals.get(key, 0) + int(row["total_tokens"])
    for row in records:
        key = (row["provider"], row.get("account_id"))
        total = totals[key]
        row["share_percent"] = 100.0 * int(row["total_tokens"]) / total if total else 0.0
    return {"models": records}
