from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .ccusage import UsageRow, imported_at, payload_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    imported_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    report TEXT NOT NULL,
    command TEXT,
    payload_sha256 TEXT NOT NULL UNIQUE,
    raw_json TEXT NOT NULL
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
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    return db


def save_import(
    db: sqlite3.Connection,
    *,
    raw: bytes,
    payload: Any,
    rows: Iterable[UsageRow],
    provider: str,
    report: str,
    command: str | None,
) -> tuple[int, int, bool]:
    digest = payload_digest(raw)
    existing = db.execute(
        "SELECT id FROM imports WHERE payload_sha256 = ?", (digest,)
    ).fetchone()
    if existing:
        count = db.execute(
            "SELECT COUNT(*) FROM usage_rows WHERE import_id = ?", (existing["id"],)
        ).fetchone()[0]
        return int(existing["id"]), int(count), False

    row_list = list(rows)
    with db:
        cursor = db.execute(
            "INSERT INTO imports (imported_at, provider, report, command, payload_sha256, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                imported_at(),
                provider,
                report,
                command,
                digest,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        import_id = int(cursor.lastrowid)
        db.executemany(
            """INSERT INTO usage_rows (
                import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens,
                cache_read_tokens, output_tokens, reasoning_output_tokens,
                reported_cost_usd, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    import_id, row.provider, row.report, row.period_start,
                    row.period_end, row.model, row.input_tokens,
                    row.cached_input_tokens, row.cache_write_tokens,
                    row.cache_read_tokens, row.output_tokens,
                    row.reasoning_output_tokens, row.reported_cost_usd,
                    row.source_path,
                )
                for row in row_list
            ],
        )
    return import_id, len(row_list), True


def list_imports(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(db.execute(
        """SELECT i.id, i.imported_at, i.provider, i.report, i.command,
                  i.payload_sha256, COUNT(u.id) AS row_count
           FROM imports i LEFT JOIN usage_rows u ON u.import_id = i.id
           GROUP BY i.id ORDER BY i.id DESC"""
    ))
