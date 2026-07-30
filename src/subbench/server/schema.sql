-- D1 schema for the SubBench dashboard.
--
-- account_key and model_key exist because SQLite permits NULL in any PRIMARY KEY that is
-- not INTEGER PRIMARY KEY, and two rows differing only by a NULL do not conflict. Claude
-- entitlement rows have no account_id and aggregate usage rows have no model, so keying
-- on the nullable columns would make ON CONFLICT upserts miss and turn every re-push into
-- duplicate rows instead of a no-op. The nullable columns are kept for reads.

CREATE TABLE IF NOT EXISTS agents (
    agent_id   TEXT PRIMARY KEY,
    label      TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entitlement_snapshots (
    agent_id         TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    provider         TEXT NOT NULL,
    account_id       TEXT,
    account_key      TEXT NOT NULL,
    window           TEXT NOT NULL,
    used_percent     REAL NOT NULL,
    resets_at        TEXT,
    duration_minutes INTEGER,
    source           TEXT NOT NULL,
    PRIMARY KEY (agent_id, provider, account_key, window, observed_at)
);

CREATE TABLE IF NOT EXISTS usage_rows (
    agent_id                TEXT NOT NULL,
    import_key              TEXT NOT NULL,
    imported_at             TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL,
    provider                TEXT NOT NULL,
    account_id              TEXT,
    account_key             TEXT NOT NULL,
    period_start            TEXT,
    model                   TEXT,
    model_key               TEXT NOT NULL,
    input_tokens            INTEGER NOT NULL,
    cached_input_tokens     INTEGER NOT NULL,
    cache_write_tokens      INTEGER NOT NULL,
    cache_read_tokens       INTEGER NOT NULL,
    output_tokens           INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    reported_cost_usd       TEXT,
    source_path             TEXT NOT NULL,
    PRIMARY KEY (agent_id, import_key, source_path, model_key)
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    agent_id    TEXT,
    status      INTEGER NOT NULL,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS usage_by_series
    ON usage_rows(agent_id, provider, account_key, last_seen_at);
CREATE INDEX IF NOT EXISTS usage_by_import
    ON usage_rows(agent_id, import_key);
CREATE INDEX IF NOT EXISTS entitlement_by_series
    ON entitlement_snapshots(agent_id, provider, account_key, window, observed_at);
