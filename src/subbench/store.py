from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .account import Account
from .ccusage import UsageRow, imported_at, payload_digest
from .entitlement import EntitlementWindow

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    imported_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    report TEXT NOT NULL,
    account_id TEXT,
    command TEXT,
    payload_sha256 TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    last_seen_at TEXT,
    UNIQUE(payload_sha256, account_id)
);
CREATE TABLE IF NOT EXISTS usage_rows (
    id INTEGER PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES imports(id),
    provider TEXT NOT NULL,
    report TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    reported_cost_usd TEXT,
    source_path TEXT NOT NULL,
    UNIQUE(import_id, source_path, model)
);
CREATE TABLE IF NOT EXISTS entitlement_snapshots (
    id INTEGER PRIMARY KEY,
    observed_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    account_id TEXT,
    window TEXT NOT NULL,
    used_percent REAL NOT NULL,
    resets_at TEXT,
    duration_minutes INTEGER,
    source TEXT NOT NULL,
    -- The plan the provider reported beside this meter. Stored per observation, not per
    -- account: a plan change resizes the entitlement, so quota points before and after
    -- measure different things and must never be mixed into one estimate.
    plan TEXT,
    UNIQUE(provider, account_id, window, observed_at)
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    alias TEXT,
    email TEXT,
    plan TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_state (
    endpoint            TEXT PRIMARY KEY,
    agent_id            TEXT NOT NULL,
    entitlement_cursor  TEXT,
    usage_cursor        TEXT,
    last_pushed_at      TEXT,
    last_error          TEXT
);
-- Quota readings with the spend recorded beside them, worked out once and kept. Pricing a
-- reading asks which import was current at that moment and sums the days inside its window,
-- and the answer cannot change once both are in the past. import_seen_at records which
-- import supplied the figure, so the one case that does change is detectable: an import
-- whose last_seen_at moves forward becomes eligible for readings it was not eligible for
-- before, and only those readings are dropped and priced again.
CREATE TABLE IF NOT EXISTS priced_points (
    provider         TEXT NOT NULL,
    account_key      TEXT NOT NULL,
    window           TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    account_id       TEXT,
    used_percent     REAL NOT NULL,
    resets_at        TEXT,
    duration_minutes INTEGER,
    plan             TEXT,
    cost_usd         REAL NOT NULL,
    cost_age_minutes REAL NOT NULL,
    import_seen_at   TEXT NOT NULL,
    PRIMARY KEY (provider, account_key, window, observed_at)
);

-- The derived measurements, kept so reading them is not re-deriving them. Every push
-- refreshes this, and the command line reads it rather than replaying the estimator over
-- the whole history to print one table.
CREATE TABLE IF NOT EXISTS reports (
    kind         TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    payload      TEXT NOT NULL
);
-- Invalidation is driven by the handful of imports that moved, and finds the readings they
-- affect by provider, account and moment.
CREATE INDEX IF NOT EXISTS priced_by_reading
    ON priced_points(provider, account_key, observed_at);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- One row per closed reset window: its estimate, and what it was computed from. A window
-- whose readings (evidence) and reference peers (peers) are unchanged is read back rather
-- than recomputed, so each cycle only estimates the open windows. Keyed by estimator
-- version: a changed estimator recomputes everything once, and older rows stay for audit.
CREATE TABLE IF NOT EXISTS window_estimates (
    provider          TEXT NOT NULL,
    account_key       TEXT NOT NULL,
    plan_key          TEXT NOT NULL,
    window            TEXT NOT NULL,
    reset_key         TEXT NOT NULL,
    estimator_version TEXT NOT NULL,
    evidence          TEXT NOT NULL,
    peers             TEXT NOT NULL,
    estimate          TEXT,
    computed_at       TEXT NOT NULL,
    PRIMARY KEY (provider, account_key, plan_key, window, reset_key, estimator_version)
);
-- Spend per hour (UTC), per model, from the built-in log reader. One row per hour and
-- model, updated in place as the hour fills, rather than a snapshot per import: an hour
-- that has closed does not change again. account_id is the account active when the hour
-- last changed, which is the account that spent it. Additive: older databases gain the
-- table on their next open, and nothing reads it in place of the daily imports.
CREATE TABLE IF NOT EXISTS usage_hourly (
    provider                TEXT NOT NULL,
    hour                    TEXT NOT NULL,
    model                   TEXT NOT NULL,
    account_id              TEXT,
    input_tokens            INTEGER NOT NULL,
    cache_write_tokens      INTEGER NOT NULL,
    cache_read_tokens       INTEGER NOT NULL,
    output_tokens           INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    requests                INTEGER NOT NULL,
    cost_usd                REAL NOT NULL,
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (provider, hour, model)
);
CREATE INDEX IF NOT EXISTS entitlement_lookup
    ON entitlement_snapshots(provider, window, observed_at);
CREATE INDEX IF NOT EXISTS entitlement_account_lookup
    ON entitlement_snapshots(account_id, provider, window, observed_at);
CREATE INDEX IF NOT EXISTS imports_account_lookup
    ON imports(account_id, provider, imported_at);
-- Pricing a window asks, for every quota reading, which import was current at that moment.
-- The answer orders imports by COALESCE(last_seen_at, imported_at), which no index over
-- the plain columns can serve, so SQLite sorted the account's whole import history into a
-- temporary b-tree once per usage row it scanned. Indexing the expression itself removes
-- the sort: a month of observations took 294 seconds to price and now takes about one.
CREATE INDEX IF NOT EXISTS imports_seen_lookup
    ON imports(provider, account_id, COALESCE(last_seen_at, imported_at));
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _has_column(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in db.execute(f"PRAGMA table_info({table})"))


def _needs_imports_rebuild(db: sqlite3.Connection) -> bool:
    """True if the imports table still has the old single-column UNIQUE on payload_sha256."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='imports'").fetchone()
    if not row:
        return False
    sql = str(row["sql"]).replace(" ", "").lower()
    return "payload_sha256" in sql and "unique(payload_sha256," not in sql


def _migrate(db: sqlite3.Connection) -> None:
    """Add account_id columns to pre-account-scoping databases; preserve existing data."""
    if not _has_column(db, "imports", "account_id"):
        db.execute("ALTER TABLE imports ADD COLUMN account_id TEXT")
    if _needs_imports_rebuild(db):
        db.executescript(
            """
            ALTER TABLE imports RENAME TO imports_legacy;
            CREATE TABLE imports (
                id INTEGER PRIMARY KEY,
                imported_at TEXT NOT NULL,
                provider TEXT NOT NULL,
                report TEXT NOT NULL,
                account_id TEXT,
                command TEXT,
                payload_sha256 TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                UNIQUE(payload_sha256, account_id)
            );
            INSERT INTO imports
                (id, imported_at, provider, report, account_id, command, payload_sha256, raw_json)
            SELECT id, imported_at, provider, report, NULL, command, payload_sha256, raw_json
            FROM imports_legacy;
            DROP TABLE imports_legacy;
            """
        )
    if not _has_column(db, "imports", "last_seen_at"):
        # An unchanged ccusage payload is deduplicated, so imported_at stops advancing
        # and a current cost reading looks arbitrarily old. last_seen_at records when
        # the payload was last confirmed, which separates "unchanged" from "not collected".
        db.executescript(
            """
            ALTER TABLE imports ADD COLUMN last_seen_at TEXT;
            UPDATE imports SET last_seen_at = imported_at WHERE last_seen_at IS NULL;
            """
        )
    _round_stored_resets(db)
    if not _has_column(db, "entitlement_snapshots", "account_id"):
        _rebuild_entitlement_snapshots(db)
    if not _has_column(db, "entitlement_snapshots", "plan"):
        # Rows recorded before this column stay NULL. A NULL plan is a product in its own
        # right, so old rows group together and never merge with a named plan. This runs
        # after the rebuild above, which recreates the table without the column.
        db.execute("ALTER TABLE entitlement_snapshots ADD COLUMN plan TEXT")


def _rebuild_entitlement_snapshots(db: sqlite3.Connection) -> None:
    # Older table used UNIQUE(provider, window, observed_at), which cannot hold
    # two accounts at the same timestamp. Rebuild with the account-aware unique constraint.
    db.executescript(
        """
        ALTER TABLE entitlement_snapshots ADD COLUMN account_id TEXT;
        ALTER TABLE entitlement_snapshots RENAME TO entitlement_snapshots_legacy;
        CREATE TABLE entitlement_snapshots (
            id INTEGER PRIMARY KEY,
            observed_at TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_id TEXT,
            window TEXT NOT NULL,
            used_percent REAL NOT NULL,
            resets_at TEXT,
            duration_minutes INTEGER,
            source TEXT NOT NULL,
            UNIQUE(provider, account_id, window, observed_at)
        );
        INSERT INTO entitlement_snapshots
            (id, observed_at, provider, account_id, window, used_percent, resets_at, duration_minutes, source)
        SELECT id, observed_at, provider, NULL, window, used_percent, resets_at, duration_minutes, source
        FROM entitlement_snapshots_legacy;
        DROP TABLE entitlement_snapshots_legacy;
        CREATE INDEX IF NOT EXISTS entitlement_lookup
            ON entitlement_snapshots(provider, window, observed_at);
        CREATE INDEX IF NOT EXISTS entitlement_account_lookup
            ON entitlement_snapshots(account_id, provider, window, observed_at);
        CREATE INDEX IF NOT EXISTS imports_account_lookup
            ON imports(account_id, provider, imported_at);
        """
    )


def _round_stored_resets(db: sqlite3.Connection) -> None:
    """Consolidate reset timestamps stored before they were rounded.

    Claude reported a stable boundary with drifting sub-second noise, so one window was
    stored under a different key on every read and every grouping fragmented.
    """
    from datetime import datetime, timedelta

    updates: list[tuple[str, str]] = []
    for row in db.execute("SELECT DISTINCT resets_at FROM entitlement_snapshots WHERE resets_at IS NOT NULL"):
        raw = str(row["resets_at"])
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        rounded = (parsed + timedelta(seconds=30)).replace(second=0, microsecond=0).isoformat()
        if rounded != raw:
            updates.append((rounded, raw))
    if updates:
        with db:
            db.executemany("UPDATE entitlement_snapshots SET resets_at = ? WHERE resets_at = ?", updates)


def upsert_account(db: sqlite3.Connection, account: Account) -> None:
    now = imported_at()
    with db:
        db.execute(
            """INSERT INTO accounts (account_id, alias, email, plan, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                   alias = COALESCE(excluded.alias, accounts.alias),
                   email = COALESCE(excluded.email, accounts.email),
                   plan = COALESCE(excluded.plan, accounts.plan),
                   last_seen = excluded.last_seen""",
            (account.account_id, account.alias, account.email, account.plan, now, now),
        )


def dropped_periods(
    db: sqlite3.Connection, *, provider: str, account_id: str | None, rows: Iterable[UsageRow]
) -> set[str]:
    """Periods the previous import reported that this one does not.

    ccusage has been observed returning fewer days than a previous run on the same
    account with the same command. A truncated read looks like a window whose spend
    stopped moving, which then trips the unobserved-usage filter and discards good
    evidence. Only shrinkage counts: a day that never had usage is simply absent.
    """
    latest = db.execute(
        """SELECT id FROM imports WHERE provider = ? AND account_id IS ?
           ORDER BY imported_at DESC LIMIT 1""",
        (provider, account_id),
    ).fetchone()
    if latest is None:
        return set()
    previous = {
        str(row["period_start"])
        for row in db.execute(
            "SELECT DISTINCT period_start FROM usage_rows WHERE import_id = ? AND period_start IS NOT NULL",
            (latest["id"],),
        )
    }
    incoming = {str(row.period_start) for row in rows if row.period_start}
    return previous - incoming


def save_import(
    db: sqlite3.Connection,
    *,
    raw: bytes,
    payload: Any,
    rows: Iterable[UsageRow],
    provider: str,
    report: str,
    command: str | None,
    account_id: str | None = None,
) -> tuple[int, int, bool]:
    digest = payload_digest(raw)
    existing = db.execute(
        "SELECT id FROM imports WHERE payload_sha256 = ? AND account_id IS ?",
        (digest, account_id),
    ).fetchone()
    if existing:
        count = db.execute("SELECT COUNT(*) FROM usage_rows WHERE import_id = ?", (existing["id"],)).fetchone()[0]
        with db:
            db.execute("UPDATE imports SET last_seen_at = ? WHERE id = ?", (imported_at(), existing["id"]))
        return int(existing["id"]), int(count), False

    row_list = list(rows)
    with db:
        cursor = db.execute(
            "INSERT INTO imports (imported_at, provider, report, account_id, command, payload_sha256, raw_json, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now := imported_at(), provider, report, account_id, command, digest, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )
        import_id = int(cursor.lastrowid)
        db.executemany(
            """INSERT INTO usage_rows (
                import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens,
                cache_read_tokens, output_tokens, reasoning_output_tokens,
                reported_cost_usd, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(
                import_id, row.provider, row.report, row.period_start, row.period_end,
                row.model, row.input_tokens, row.cached_input_tokens,
                row.cache_write_tokens, row.cache_read_tokens, row.output_tokens,
                row.reasoning_output_tokens, row.reported_cost_usd, row.source_path,
            ) for row in row_list],
        )
    return import_id, len(row_list), True


def plan_moves(db: sqlite3.Connection, rows: Iterable[EntitlementWindow]) -> list[str]:
    """Name a plan change on the same account. Call before the new readings are stored.

    The series key already splits on plan, so the readings are not mixed. This is the
    notice that the split happened. A provider that has not named a plan yet cannot
    be a change: absence is not a move.
    """
    messages: list[str] = []
    seen: set[tuple[str, str | None, str]] = set()
    for row in rows:
        if not row.plan:
            continue
        key = (row.provider, row.account_id, row.window)
        if key in seen:
            continue
        seen.add(key)
        previous = db.execute(
            """SELECT plan FROM entitlement_snapshots
               WHERE provider = ? AND account_id IS ? AND window = ? AND plan IS NOT NULL
               ORDER BY observed_at DESC LIMIT 1""",
            (row.provider, row.account_id, row.window),
        ).fetchone()
        if previous and previous["plan"] != row.plan:
            messages.append(f"plan changed {previous['plan']} -> {row.plan} ({row.window})")
    return messages


def save_entitlements(db: sqlite3.Connection, rows: Iterable[EntitlementWindow], observed_at: str) -> int:
    values = list(rows)
    with db:
        db.executemany(
            """INSERT OR IGNORE INTO entitlement_snapshots
               (observed_at, provider, account_id, window, used_percent, resets_at,
                duration_minutes, source, plan)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(observed_at, row.provider, row.account_id, row.window, row.used_percent,
              row.resets_at, row.duration_minutes, row.source, row.plan) for row in values],
        )
    return len(values)


# ccusage re-reports its whole history on every run and is not stable between runs:
# identical commands on the same account have returned reports differing by several
# whole days. Differencing a lifetime total therefore manufactures large phantom
# deltas. Summing only the days inside the entitlement window makes the numerator
# depend on the days being valued and nothing else. The window opens mid-day, so the
# first day carries some pre-window spend; that offset is constant within a window
# and cancels in every pairwise difference.
_PRICED_WINDOWS_TEMPLATE = """
    WITH scoped AS (
        SELECT e.provider, e.account_id, e.window, e.observed_at, e.used_percent,
               e.resets_at, e.duration_minutes, e.plan,
               -- Both columns must describe the same row, so pick that row once by the
               -- moment its contents were last confirmed, and measure the age of that
               -- same confirmation. Ranking by imported_at while ageing by last_seen_at
               -- would let the age describe an import that was not the one priced.
               (SELECT i.id FROM imports i
                 WHERE i.provider = e.provider AND i.account_id IS e.account_id
                   AND COALESCE(i.last_seen_at, i.imported_at) <= e.observed_at
                 ORDER BY COALESCE(i.last_seen_at, i.imported_at) DESC LIMIT 1) AS import_id,
               (SELECT (unixepoch(e.observed_at)
                        - unixepoch(COALESCE(i.last_seen_at, i.imported_at))) / 60.0
                  FROM imports i
                 WHERE i.provider = e.provider AND i.account_id IS e.account_id
                   AND COALESCE(i.last_seen_at, i.imported_at) <= e.observed_at
                 ORDER BY COALESCE(i.last_seen_at, i.imported_at) DESC LIMIT 1) AS cost_age_minutes,
               (SELECT COALESCE(i.last_seen_at, i.imported_at) FROM imports i
                 WHERE i.provider = e.provider AND i.account_id IS e.account_id
                   AND COALESCE(i.last_seen_at, i.imported_at) <= e.observed_at
                 ORDER BY COALESCE(i.last_seen_at, i.imported_at) DESC LIMIT 1) AS import_seen_at,
               DATE(e.resets_at, '-' || COALESCE(
                   e.duration_minutes,
                   CASE e.window WHEN 'five_hour' THEN 300 ELSE 10080 END
               ) || ' minutes') AS window_start_date,
               DATE(e.resets_at) AS window_end_date
        FROM entitlement_snapshots e
        {unpriced_only}
    ), priced AS (
        SELECT s.*,
               (SELECT COALESCE(SUM(CAST(COALESCE(u.reported_cost_usd, '0') AS REAL)), 0.0)
                  FROM usage_rows u
                 WHERE u.import_id = s.import_id
                   -- An undated row cannot be placed in or out of the window. Counting it
                   -- keeps the numerator whole; dropping it would silently report $0.
                   AND (u.period_start IS NULL OR (
                            (s.window_start_date IS NULL OR DATE(u.period_start) >= s.window_start_date)
                        AND (s.window_end_date IS NULL OR DATE(u.period_start) <= s.window_end_date)
                   ))
               ) AS cost_usd
          FROM scoped s
         WHERE s.import_id IS NOT NULL
    )
"""

# Every reading, for callers that summarise the lot.
PRICED_WINDOWS_SQL = _PRICED_WINDOWS_TEMPLATE.format(unpriced_only="")

# Only the readings not priced yet. The filter belongs inside the query rather than after
# it: pricing every reading and then discarding the ones already stored costs the same as
# not having stored them.
_UNPRICED_ONLY = """
        WHERE e.id IN (SELECT id FROM unpriced_ids)"""

# Which readings still need pricing, decided by index lookups alone. Naming them first and
# pricing only those is what makes a refresh cheap: leaving the test inside the pricing
# query let SQLite run the import lookup and the usage sum for every reading before
# discarding the ones already stored.
_UNPRICED_IDS_SQL = """
    CREATE TEMP TABLE unpriced_ids AS
    SELECT e.id FROM entitlement_snapshots e
     WHERE NOT EXISTS (
         SELECT 1 FROM priced_points q
          WHERE q.provider = e.provider
            AND q.account_key = COALESCE(e.account_id, '')
            AND q.window = e.window
            AND q.observed_at = e.observed_at
     )"""
_PRICE_UNPRICED_SQL = _PRICED_WINDOWS_TEMPLATE.format(unpriced_only=_UNPRICED_ONLY)


# Defined with the estimator so the server can import it without dragging in this
# module, which reaches subprocess through the collectors and would not load on the
# Workers runtime.
from .regression import MAX_COST_AGE_MINUTES  # noqa: E402  (re-exported for callers)


def regression_points(
    db: sqlite3.Connection,
    provider: str | None = None,
    account_id: str | None = None,
    *,
    max_cost_age_minutes: float = MAX_COST_AGE_MINUTES,
) -> list[sqlite3.Row]:
    where = ["p.cost_age_minutes <= ?"]
    params: list[Any] = [max_cost_age_minutes]
    if provider is not None:
        where.append("p.provider = ?")
        params.append(provider)
    if account_id is not None:
        where.append("p.account_id IS ?")
        params.append(account_id)
    where_clause = "WHERE " + " AND ".join(where)
    refresh_priced_points(db)
    return list(db.execute(
        f"""SELECT p.provider, p.account_id, p.window, p.observed_at, p.used_percent,
               p.resets_at, p.duration_minutes, p.plan, p.cost_usd, p.cost_age_minutes
        FROM priced_points p
        {where_clause}
        ORDER BY p.provider, p.account_id, p.window, p.resets_at, p.observed_at""",
        params,
    ))


# The newest import seen at the last refresh. Confirming a payload sets last_seen_at to
# now, so anything that moved sits above this mark.
_IMPORT_WATERMARK = "priced_points.import_watermark"
# The highest import id at the last refresh. An import written with a back-dated timestamp
# would sit below the mark above while still being new, and this catches it.
_IMPORT_ID_WATERMARK = "priced_points.import_id_watermark"

# Columns of priced_points, in the order the pricing query produces them.
_PRICED_COLUMNS = (
    "provider, account_key, window, observed_at, account_id, used_percent, resets_at, "
    "duration_minutes, plan, cost_usd, cost_age_minutes, import_seen_at"
)


def refresh_priced_points(db: sqlite3.Connection) -> tuple[int, int]:
    """Price the readings that are not priced yet, and re-price the few that changed.

    Returns how many rows were dropped as stale and how many were added. Readings are only
    ever appended, so in the steady state this prices the handful collected since the last
    call rather than all of them again.
    """
    with db:
        # An import whose last_seen_at moved forward is now the current one for readings it
        # was not current for before. Those readings, and only those, are priced again.
        #
        # Driven by the imports rather than by the readings. Asking every stored reading
        # whether some import overtook it scanned the whole table for an answer that is
        # almost always no; only an import seen since the last refresh can overtake
        # anything, and moving last_seen_at forward sets it to now, so nothing that
        # changed can sit below the mark.
        marks = {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM meta")}
        seen_before = marks.get(_IMPORT_WATERMARK, "")
        id_before = int(marks.get(_IMPORT_ID_WATERMARK, 0) or 0)
        stale = db.execute(
            """DELETE FROM priced_points WHERE rowid IN (
                   SELECT p.rowid
                     FROM imports i
                     JOIN priced_points p
                       ON p.provider = i.provider
                      AND p.account_key = COALESCE(i.account_id, '')
                    WHERE (COALESCE(i.last_seen_at, i.imported_at) > ? OR i.id > ?)
                      AND COALESCE(i.last_seen_at, i.imported_at) <= p.observed_at
                      AND COALESCE(i.last_seen_at, i.imported_at) > p.import_seen_at
               )""",
            (seen_before, id_before),
        ).rowcount
        db.execute("DROP TABLE IF EXISTS temp.unpriced_ids")
        db.execute(_UNPRICED_IDS_SQL)
        pending = db.execute("SELECT COUNT(*) FROM unpriced_ids").fetchone()[0]
        added = 0
        if pending:
            db.execute(
                f"""{_PRICE_UNPRICED_SQL}
                INSERT OR REPLACE INTO priced_points ({_PRICED_COLUMNS})
                SELECT p.provider, COALESCE(p.account_id, ''), p.window, p.observed_at,
                       p.account_id, p.used_percent, p.resets_at, p.duration_minutes, p.plan,
                       p.cost_usd, p.cost_age_minutes, p.import_seen_at
                  FROM priced p
                 WHERE p.import_seen_at IS NOT NULL"""
            )
            added = db.execute("SELECT changes()").fetchone()[0]
        db.execute("DROP TABLE IF EXISTS temp.unpriced_ids")
        watermark, highest_id = db.execute(
            "SELECT COALESCE(MAX(COALESCE(last_seen_at, imported_at)), ''), COALESCE(MAX(id), 0) "
            "FROM imports"
        ).fetchone()
        db.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ((_IMPORT_WATERMARK, watermark), (_IMPORT_ID_WATERMARK, str(highest_id))),
        )
    return stale, added


def model_mix(
    db: sqlite3.Connection,
    provider: str | None = None,
    account_id: str | None = None,
) -> list[sqlite3.Row]:
    """Per-model token share of each entitlement window, from its newest import.

    Quota-per-dollar varies several-fold within a single window, and the most likely
    cause is which models the workload used. Solving for per-model quota weights needs
    many windows with a *varying* mix -- well more than the number of distinct models --
    so the fit is deliberately not attempted here. This records the inputs it will need,
    because unlike a derivation it cannot be reconstructed after the fact.
    """
    where = ["p.provider = ?"] if provider else []
    params: list[Any] = [provider] if provider else []
    if account_id is not None:
        where.append("p.account_id IS ?")
        params.append(account_id)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    return list(db.execute(
        f"""{PRICED_WINDOWS_SQL}, latest AS (
            SELECT p.provider, p.account_id, p.window, p.resets_at,
                   MAX(p.observed_at) AS observed_at
            FROM priced p {where_clause}
            GROUP BY p.provider, p.account_id, p.window, p.resets_at
        )
        SELECT l.provider, l.account_id, l.window, l.resets_at, u.model,
               -- Per-model cost is not available: ccusage reports cost on the aggregate
               -- row and leaves the model breakdowns null, so only tokens are recorded.
               -- Reasoning is a subset of output in both providers' ccusage JSON, so it
               -- is stored but not added. cache_read is Codex's split cache, not a subset
               -- of input in that JSON.
               SUM(u.input_tokens + u.output_tokens + u.cache_read_tokens
                   + u.cache_write_tokens) AS total_tokens
        FROM latest l
        JOIN priced p ON p.provider = l.provider AND p.account_id IS l.account_id
                     AND p.window = l.window AND p.resets_at IS l.resets_at
                     AND p.observed_at = l.observed_at
        JOIN usage_rows u ON u.import_id = p.import_id
        WHERE u.model IS NOT NULL
          AND (p.window_start_date IS NULL OR DATE(u.period_start) >= p.window_start_date)
          AND (p.window_end_date IS NULL OR DATE(u.period_start) <= p.window_end_date)
        GROUP BY l.provider, l.account_id, l.window, l.resets_at, u.model
        HAVING total_tokens > 0
        ORDER BY l.provider, l.account_id, l.window, l.resets_at, total_tokens DESC""",
        params,
    ))


def estimate_windows(
    db: sqlite3.Connection,
    provider: str | None = None,
    account_id: str | None = None,
    *,
    max_cost_age_minutes: float = MAX_COST_AGE_MINUTES,
) -> list[sqlite3.Row]:
    where = ["p.provider = ?"]
    params: list[Any] = [provider] if provider else []
    if provider is None:
        where = []
    if account_id is not None:
        where.append("p.account_id IS ?")
        params.append(account_id)
    where_clause = ("AND " + " AND ".join(where)) if where else ""
    return list(db.execute(
        f"""{PRICED_WINDOWS_SQL}, paired AS (
            SELECT p.*,
                   LAG(p.observed_at) OVER w AS previous_at,
                   LAG(p.used_percent) OVER w AS previous_used,
                   LAG(p.resets_at) OVER w AS previous_reset,
                   LAG(p.cost_usd) OVER w AS previous_cost
            FROM priced p
            WINDOW w AS (PARTITION BY p.provider, p.account_id, p.window ORDER BY p.observed_at)
        )
        SELECT p.provider, p.account_id, p.window, p.previous_at, p.observed_at,
               p.previous_used, p.used_percent,
               (p.used_percent - p.previous_used) AS quota_delta_percent,
               p.cost_usd - p.previous_cost AS api_value_usd,
               CASE WHEN p.used_percent > p.previous_used
                    THEN (p.cost_usd - p.previous_cost) / ((p.used_percent - p.previous_used) / 100.0)
               END AS implied_full_window_usd
        FROM paired p
        WHERE p.previous_at IS NOT NULL
          AND (
              p.resets_at = p.previous_reset
              OR (
                  p.resets_at IS NOT NULL AND p.previous_reset IS NOT NULL
                  AND ABS(unixepoch(p.resets_at) - unixepoch(p.previous_reset)) < 60
              )
              OR (p.resets_at IS NULL AND p.previous_reset IS NULL)
          )
          AND p.used_percent > p.previous_used
          AND p.cost_usd > p.previous_cost
          AND p.cost_age_minutes <= ?
          {where_clause}
        ORDER BY p.observed_at DESC""",
        [max_cost_age_minutes, *params],
    ))


def list_imports(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(db.execute(
        """SELECT i.id, i.imported_at, i.provider, i.report, i.account_id, i.command,
                  i.payload_sha256, COUNT(u.id) AS row_count
           FROM imports i LEFT JOIN usage_rows u ON u.import_id = i.id
           GROUP BY i.id ORDER BY i.id DESC"""
    ))


def list_accounts(db: sqlite3.Connection, provider: str | None = None) -> list[sqlite3.Row]:
    if provider:
        return list(db.execute(
            """SELECT DISTINCT e.account_id AS account_id, a.alias, a.email, a.plan
               FROM entitlement_snapshots e LEFT JOIN accounts a ON a.account_id = e.account_id
               WHERE e.provider = ? ORDER BY a.email, e.account_id""",
            (provider,),
        ))
    return list(db.execute(
        """SELECT DISTINCT e.account_id AS account_id, a.alias, a.email, a.plan
           FROM entitlement_snapshots e LEFT JOIN accounts a ON a.account_id = e.account_id
           ORDER BY COALESCE(a.email, e.account_id)"""
    ))

def save_report(db: sqlite3.Connection, kind: str, payload: dict[str, Any], generated_at: str) -> None:
    with db:
        db.execute(
            """INSERT INTO reports (kind, generated_at, payload) VALUES (?, ?, ?)
               ON CONFLICT(kind) DO UPDATE SET
                 generated_at = excluded.generated_at, payload = excluded.payload""",
            (kind, generated_at, json.dumps(payload)),
        )


def load_report(db: sqlite3.Connection, kind: str) -> tuple[str, dict[str, Any]] | None:
    """The stored report and when it was computed, or None if it never has been."""
    row = db.execute("SELECT generated_at, payload FROM reports WHERE kind = ?", (kind,)).fetchone()
    if row is None:
        return None
    try:
        return str(row["generated_at"]), json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


HOURLY_FIELDS = ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens",
                 "reasoning_output_tokens", "requests", "cost_usd")


def save_hourly(db: sqlite3.Connection, *, provider: str, account_id: str | None,
                buckets: dict[tuple[str, str], dict[str, float]], now: str) -> int:
    """Write the hours whose figures changed. Returns how many rows were written.

    Only a changed hour is written, so an hour keeps the account it was spent on even after
    another account becomes active.
    """
    current = {
        (row["hour"], row["model"]): tuple(row[f] for f in HOURLY_FIELDS)
        for row in db.execute(f"SELECT hour, model, {', '.join(HOURLY_FIELDS)} FROM usage_hourly WHERE provider = ?", (provider,))
    }
    changed = []
    for (hour, model), values in buckets.items():
        row = tuple(values[f] for f in HOURLY_FIELDS)
        old = current.get((hour, model))
        if old is None or any(abs(float(a) - float(b)) > 1e-9 for a, b in zip(old, row)):
            changed.append((provider, hour, model, account_id, *row, now))
    if changed:
        with db:
            db.executemany(
                f"""INSERT INTO usage_hourly (provider, hour, model, account_id, {', '.join(HOURLY_FIELDS)}, updated_at)
                    VALUES ({', '.join('?' for _ in range(5 + len(HOURLY_FIELDS)))})
                    ON CONFLICT (provider, hour, model) DO UPDATE SET
                    account_id = excluded.account_id, {', '.join(f'{f} = excluded.{f}' for f in HOURLY_FIELDS)},
                    updated_at = excluded.updated_at""",
                changed,
            )
    return len(changed)


def hourly(db: sqlite3.Connection, *, provider: str | None = None, since: str | None = None) -> list[sqlite3.Row]:
    return list(db.execute(
        """SELECT * FROM usage_hourly
            WHERE (? IS NULL OR provider = ?) AND (? IS NULL OR hour >= ?)
            ORDER BY hour, provider, model""",
        (provider, provider, since, since),
    ))


class WindowEstimateStore:
    """regression.robust_estimates' store: closed windows' estimates, read and written in bulk."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._loaded: dict[str, dict[tuple, tuple[str, str, Any]]] = {}
        self._pending: list[tuple] = []

    @staticmethod
    def _key(key: tuple) -> tuple:
        provider, account_id, plan, window, reset_key = key
        return (provider, account_id or "", plan or "", window, reset_key)

    def get(self, key: tuple, version: str):
        if version not in self._loaded:
            from .regression import RegressionEstimate

            rows = {}
            for row in self.db.execute(
                "SELECT provider, account_key, plan_key, window, reset_key, evidence, peers, estimate "
                "FROM window_estimates WHERE estimator_version = ?", (version,)
            ):
                estimate = RegressionEstimate(**json.loads(row["estimate"])) if row["estimate"] else None
                rows[(row["provider"], row["account_key"], row["plan_key"], row["window"], row["reset_key"])] = (
                    row["evidence"], row["peers"], estimate)
            self._loaded[version] = rows
        return self._loaded[version].get(self._key(key))

    def put(self, key: tuple, version: str, evidence: str, peers: str, estimate) -> None:
        from dataclasses import asdict

        stored = json.dumps(asdict(estimate)) if estimate is not None else None
        self._loaded.setdefault(version, {})[self._key(key)] = (evidence, peers, estimate)
        self._pending.append((*self._key(key), version, evidence, peers, stored, imported_at()))

    def flush(self) -> None:
        if not self._pending:
            return
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO window_estimates (provider, account_key, plan_key, window, reset_key, "
                "estimator_version, evidence, peers, estimate, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._pending,
            )
        self._pending = []
