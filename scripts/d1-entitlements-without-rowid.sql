-- Rebuild entitlement_snapshots as WITHOUT ROWID (once, on a database made before the
-- change in schema.sql). It copies every reading, so it costs one row write per reading:
-- run it early in the UTC day. D1 runs the file as one transaction.
--   wrangler d1 execute subbench --remote --file scripts/d1-entitlements-without-rowid.sql
CREATE TABLE entitlement_snapshots_new (
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
    plan             TEXT,
    PRIMARY KEY (agent_id, provider, account_key, window, observed_at)
) WITHOUT ROWID;
INSERT INTO entitlement_snapshots_new
    (agent_id, observed_at, provider, account_id, account_key, window,
     used_percent, resets_at, duration_minutes, source, plan)
SELECT agent_id, observed_at, provider, account_id, account_key, window,
       used_percent, resets_at, duration_minutes, source, plan
FROM entitlement_snapshots;
DROP TABLE entitlement_snapshots;
ALTER TABLE entitlement_snapshots_new RENAME TO entitlement_snapshots;
