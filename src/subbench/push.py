"""Send collected evidence to a SubBench server.

The local database stays the source of truth. A push is best-effort and incremental: it
sends evidence recorded since the server last acknowledged, and advances the cursor only
on success, so a lost acknowledgement costs a duplicate send rather than a gap. Collection
never waits on the network.

Raw ccusage payloads are not sent. They are most of the local database by size and the
estimator never reads them; the server receives the normalised rows it actually consumes.
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable

SCHEMA_VERSION = 1
# urllib's default User-Agent is rejected by Cloudflare's bot rules with a 403 before the
# request ever reaches the Worker, which surfaces as an opaque "error code: 1010".
# Identifying the client properly is the fix, and is better manners anyway.
USER_AGENT = "subbench/0.2.0 (+https://github.com/jask-aran/SubBench)"
MAX_USAGE_ROWS_PER_BATCH = 5000
MAX_ENTITLEMENT_ROWS_PER_BATCH = 5000
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class PushState:
    agent_id: str
    entitlement_cursor: str | None
    usage_cursor: str | None


@dataclass(frozen=True)
class PushResult:
    sent_entitlements: int
    sent_usage: int
    drained: bool
    message: str


def push_state(db: sqlite3.Connection, endpoint: str) -> PushState:
    row = db.execute(
        "SELECT agent_id, entitlement_cursor, usage_cursor FROM push_state WHERE endpoint = ?",
        (endpoint,),
    ).fetchone()
    if row is not None:
        return PushState(str(row["agent_id"]), row["entitlement_cursor"], row["usage_cursor"])
    # The identifier is random rather than derived from anything about the machine or
    # account, so it carries no meaning beyond "these rows came from one collector".
    agent_id = str(uuid.uuid4())
    with db:
        db.execute(
            "INSERT INTO push_state (endpoint, agent_id) VALUES (?, ?)",
            (endpoint, agent_id),
        )
    return PushState(agent_id, None, None)


def pending_entitlements(db: sqlite3.Connection, cursor: str | None, limit: int) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT observed_at, provider, account_id, window, used_percent,
                  resets_at, duration_minutes, source
           FROM entitlement_snapshots
           WHERE ? IS NULL OR observed_at > ?
           ORDER BY observed_at LIMIT ?""",
        (cursor, cursor, limit),
    )
    return [dict(row) for row in rows]


def pending_usage(db: sqlite3.Connection, cursor: str | None, limit: int) -> list[dict[str, Any]]:
    """Usage rows from imports confirmed since the cursor, batched by whole import.

    Ordered by last_seen_at, not imported_at: an unchanged ccusage payload is deduplicated
    locally, so imported_at stops advancing while last_seen_at keeps recording that the
    figures were confirmed. The server needs the latter to apply its staleness bound.

    Every row of one import shares that import's last_seen_at, so a row-limited batch
    could end mid-import; the cursor would then advance past that timestamp and the
    remaining rows would never be sent. Whole imports are selected first and only then
    expanded into rows, which makes splitting impossible. An import larger than the limit
    is still sent whole -- an oversized batch is recoverable, a silent gap is not.
    """
    imports = [
        (row["id"], row["imported_at"], row["last_seen_at"], row["account_id"], row["row_count"])
        for row in db.execute(
            """SELECT i.id, i.imported_at,
                      COALESCE(i.last_seen_at, i.imported_at) AS last_seen_at,
                      i.account_id, COUNT(u.id) AS row_count
               FROM imports i JOIN usage_rows u ON u.import_id = i.id
               WHERE ? IS NULL OR COALESCE(i.last_seen_at, i.imported_at) > ?
               GROUP BY i.id
               ORDER BY last_seen_at, i.id""",
            (cursor, cursor),
        )
    ]
    selected: list[tuple] = []
    total = 0
    for entry in imports:
        if selected and total + entry[4] > limit:
            break
        selected.append(entry)
        total += entry[4]
    if not selected:
        return []

    placeholders = ",".join("?" for _ in selected)
    by_id = {entry[0]: entry for entry in selected}
    rows = db.execute(
        f"""SELECT u.import_id, u.provider, u.period_start, u.model,
                   u.input_tokens, u.cached_input_tokens, u.cache_write_tokens,
                   u.cache_read_tokens, u.output_tokens, u.reasoning_output_tokens,
                   u.reported_cost_usd, u.source_path
            FROM usage_rows u WHERE u.import_id IN ({placeholders})
            ORDER BY u.import_id, u.id""",
        [entry[0] for entry in selected],
    )
    payload: list[dict[str, Any]] = []
    for row in rows:
        entry = by_id[row["import_id"]]
        record = dict(row)
        record.pop("import_id")
        payload.append({
            "import_key": str(entry[0]),
            "imported_at": entry[1],
            "last_seen_at": entry[2],
            "account_id": entry[3],
            **record,
        })
    return payload


def build_payload(
    agent_id: str,
    entitlements: list[dict[str, Any]],
    usage: list[dict[str, Any]],
    reports: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "agent_id": agent_id,
        "schema_version": SCHEMA_VERSION,
        "entitlements": entitlements,
        "usage": usage,
    }
    if reports:
        payload["reports"] = reports
    return payload


def build_reports(db: sqlite3.Connection) -> dict[str, Any]:
    """Everything the dashboard displays, computed here rather than on the server.

    The server stores these verbatim. Deriving them there would mean a second estimator
    implementation, and every subtle defect this project has hit lived in that code --
    two copies would disagree with no way to tell which was right. Raw evidence is pushed
    alongside, so moving derivation server-side later needs computation added, not
    history backfilled.
    """
    from .crosssolve import account_plans, combined_estimates, divergences
    from .regression import robust_estimates
    from .server.confidence import classify
    from .store import model_mix, regression_points
    from .timeline import build_series
    from .timeseries import detect_regime_changes, rolling_values, window_history
    from .weights import observations_from_windows, solve

    points = [dict(row) for row in regression_points(db)]
    estimates, ratios = combined_estimates(points, robust_estimates(points))
    plans = account_plans(db)
    mix = [dict(row) for row in model_mix(db)]

    current = []
    by_series: dict[tuple, list] = {}
    for estimate in estimates:
        by_series.setdefault((estimate.provider, estimate.window, estimate.account_id), []).append(estimate)
    for row in (value.as_dict() for value in rolling_values(estimates)):
        members = by_series.get((row["provider"], row["window"], row.get("account_id")), [])
        if members:
            best = max(members, key=lambda item: item.covered_quota_percent)
            row.update(classify(best, estimates).as_dict())
        else:
            row.update({"tier": "provisional", "reason": "no window-level estimate"})
        current.append(row)

    tiers = {
        (e.provider, e.window, e.account_id, e.reset_key): classify(e, estimates).as_dict()
        for e in estimates
    }
    history = []
    for row in window_history(estimates):
        key = (row["provider"], row["window"], row.get("account_id"), row["reset_key"])
        history.append({**row, **tiers.get(key, {"tier": "provisional", "reason": ""})})

    totals: dict[tuple, int] = {}
    for row in mix:
        key = (row["provider"], row.get("account_id"), str(row.get("resets_at")))
        totals[key] = totals.get(key, 0) + int(row["total_tokens"])
    models = []
    for row in mix:
        key = (row["provider"], row.get("account_id"), str(row.get("resets_at")))
        total = totals[key]
        models.append({**row, "share_percent": 100.0 * int(row["total_tokens"]) / total if total else 0.0})

    observations = observations_from_windows(estimates, mix)
    providers = sorted({str(row["provider"]) for row in observations})

    return {
        "current": {
            "current": current,
            "regime_changes": [row.as_dict() for row in detect_regime_changes(estimates)],
            "divergences": [row.as_dict() for row in divergences(estimates, ratios, plans)],
            "window_ratios": [
                {"provider": r.provider, "account_id": r.account_id,
                 "short_window": r.short_window, "long_window": r.long_window,
                 "ratio": r.ratio, "per_long_window": 1.0 / r.ratio}
                for r in ratios
            ],
        },
        "history": {"windows": history},
        "series": build_series(points),
        "models": {"models": models},
        "weights": {"providers": [solve(observations, provider=name).as_dict() for name in providers]},
    }


def _advance(db: sqlite3.Connection, endpoint: str, entitlements, usage, error: str | None) -> None:
    from .ccusage import imported_at

    with db:
        db.execute(
            """UPDATE push_state
                  SET entitlement_cursor = COALESCE(?, entitlement_cursor),
                      usage_cursor = COALESCE(?, usage_cursor),
                      last_pushed_at = ?, last_error = ?
                WHERE endpoint = ?""",
            (
                entitlements[-1]["observed_at"] if entitlements else None,
                usage[-1]["last_seen_at"] if usage else None,
                imported_at(),
                error,
                endpoint,
            ),
        )


def _post(url: str, token: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def push_once(
    db: sqlite3.Connection,
    *,
    url: str,
    token: str,
    sender: Callable[[str, str, dict[str, Any], float], dict[str, Any]] = _post,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> PushResult:
    """Send one batch. Returns whether the local backlog is now drained."""
    state = push_state(db, url)
    entitlements = pending_entitlements(db, state.entitlement_cursor, MAX_ENTITLEMENT_ROWS_PER_BATCH)
    usage = pending_usage(db, state.usage_cursor, MAX_USAGE_ROWS_PER_BATCH)
    if not entitlements and not usage:
        return PushResult(0, 0, True, "nothing to push")

    # Reports go with the first batch of a push so the dashboard is never showing
    # numbers derived from evidence the server has not finished receiving.
    payload = build_payload(state.agent_id, entitlements, usage, reports=build_reports(db))
    try:
        sender(url, token, payload, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as error:
        detail = getattr(error, "reason", None) or str(error)
        if isinstance(error, urllib.error.HTTPError):
            detail = f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:200]}"
        # The cursor stays put, so rejected evidence is retried once the cause is fixed.
        _advance(db, url, [], [], str(detail))
        return PushResult(0, 0, False, f"push failed: {detail}")

    _advance(db, url, entitlements, usage, None)
    drained = (
        len(entitlements) < MAX_ENTITLEMENT_ROWS_PER_BATCH
        and len(usage) < MAX_USAGE_ROWS_PER_BATCH
    )
    return PushResult(len(entitlements), len(usage), drained, f"pushed {len(entitlements)} entitlement, {len(usage)} usage row(s)")


def push_all(
    db: sqlite3.Connection,
    *,
    url: str,
    token: str,
    sender: Callable[[str, str, dict[str, Any], float], dict[str, Any]] = _post,
    max_batches: int = 50,
) -> PushResult:
    """Push until drained, a batch fails, or the batch limit is reached."""
    total_entitlements = total_usage = 0
    message = "nothing to push"
    for _ in range(max_batches):
        result = push_once(db, url=url, token=token, sender=sender)
        total_entitlements += result.sent_entitlements
        total_usage += result.sent_usage
        message = result.message
        if result.drained or result.sent_entitlements + result.sent_usage == 0:
            return PushResult(total_entitlements, total_usage, result.drained, message)
    return PushResult(total_entitlements, total_usage, False, f"{message}; batch limit reached")
