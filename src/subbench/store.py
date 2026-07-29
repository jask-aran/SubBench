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
CREATE INDEX IF NOT EXISTS entitlement_lookup
    ON entitlement_snapshots(provider, window, observed_at);
CREATE INDEX IF NOT EXISTS entitlement_account_lookup
    ON entitlement_snapshots(account_id, provider, window, observed_at);
CREATE INDEX IF NOT EXISTS imports_account_lookup
    ON imports(account_id, provider, imported_at);
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
    if _has_column(db, "entitlement_snapshots", "account_id"):
        return
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
        return int(existing["id"]), int(count), False

    row_list = list(rows)
    with db:
        cursor = db.execute(
            "INSERT INTO imports (imported_at, provider, report, account_id, command, payload_sha256, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (imported_at(), provider, report, account_id, command, digest, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
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


def save_entitlements(db: sqlite3.Connection, rows: Iterable[EntitlementWindow], observed_at: str) -> int:
    values = list(rows)
    with db:
        db.executemany(
            """INSERT OR IGNORE INTO entitlement_snapshots
               (observed_at, provider, account_id, window, used_percent, resets_at, duration_minutes, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(observed_at, row.provider, row.account_id, row.window, row.used_percent,
              row.resets_at, row.duration_minutes, row.source) for row in values],
        )
    return len(values)


def regression_points(db: sqlite3.Connection, provider: str | None = None, account_id: str | None = None) -> list[sqlite3.Row]:
    where = ["e.provider = ?"]
    params: list[Any] = [provider] if provider else []
    if provider is None:
        where = []
    if account_id is not None:
        where.append("e.account_id IS ?")
        params.append(account_id)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    return list(db.execute(
        f"""WITH costs AS (
            SELECT i.id, i.provider, i.account_id, i.imported_at,
                   SUM(CAST(COALESCE(u.reported_cost_usd, '0') AS REAL)) AS cost_usd
            FROM imports i JOIN usage_rows u ON u.import_id = i.id
            GROUP BY i.id
        )
        SELECT e.provider, e.account_id, e.window, e.observed_at, e.used_percent,
               e.resets_at, e.duration_minutes,
               c.cost_usd
        FROM entitlement_snapshots e
        JOIN costs c ON c.id = (
            SELECT id FROM costs
            WHERE provider = e.provider AND account_id IS e.account_id
            AND imported_at <= e.observed_at
            ORDER BY imported_at DESC LIMIT 1
        )
        {where_clause}
        ORDER BY e.provider, e.account_id, e.window, e.resets_at, e.observed_at""",
        params,
    ))


def estimate_windows(db: sqlite3.Connection, provider: str | None = None, account_id: str | None = None) -> list[sqlite3.Row]:
    where = ["e.provider = ?"]
    params: list[Any] = [provider] if provider else []
    if provider is None:
        where = []
    if account_id is not None:
        where.append("e.account_id IS ?")
        params.append(account_id)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    return list(db.execute(
        f"""WITH costs AS (
            SELECT i.id, i.provider, i.account_id, i.imported_at,
                   SUM(CAST(COALESCE(u.reported_cost_usd, '0') AS REAL)) AS cost_usd
            FROM imports i JOIN usage_rows u ON u.import_id = i.id
            GROUP BY i.id
        ), paired AS (
            SELECT e.*,
                   LAG(e.observed_at) OVER w AS previous_at,
                   LAG(e.used_percent) OVER w AS previous_used,
                   LAG(e.resets_at) OVER w AS previous_reset
            FROM entitlement_snapshots e
            {where_clause}
            WINDOW w AS (PARTITION BY e.provider, e.account_id, e.window ORDER BY e.observed_at)
        )
        SELECT p.provider, p.account_id, p.window, p.previous_at, p.observed_at,
               p.previous_used, p.used_percent,
               (p.used_percent - p.previous_used) AS quota_delta_percent,
               c1.cost_usd - c0.cost_usd AS api_value_usd,
               CASE WHEN p.used_percent > p.previous_used
                    THEN (c1.cost_usd - c0.cost_usd) / ((p.used_percent - p.previous_used) / 100.0)
               END AS implied_full_window_usd
        FROM paired p
        JOIN costs c0 ON c0.id = (
            SELECT id FROM costs WHERE provider = p.provider AND account_id IS p.account_id
            AND imported_at <= p.previous_at ORDER BY imported_at DESC LIMIT 1
        )
        JOIN costs c1 ON c1.id = (
            SELECT id FROM costs WHERE provider = p.provider AND account_id IS p.account_id
            AND imported_at <= p.observed_at ORDER BY imported_at DESC LIMIT 1
        )
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
          AND c1.cost_usd >= c0.cost_usd
        ORDER BY p.observed_at DESC""",
        params,
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