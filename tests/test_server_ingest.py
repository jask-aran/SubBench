import json
import sqlite3
from pathlib import Path

import pytest

from subbench.push import build_payload, pending_entitlements, pending_usage
from subbench.regression import robust_estimates
from subbench.server import ingest, queries
from subbench.store import connect, regression_points

SCHEMA = (Path(__file__).resolve().parents[1] / "src/subbench/server/schema.sql").read_text()


def server_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def store(db: sqlite3.Connection, batch: ingest.Batch) -> None:
    db.execute(ingest.AGENT_UPSERT, (batch.agent_id, "now", "now"))
    db.executemany(
        ingest.ENTITLEMENT_UPSERT,
        [ingest.bind(row, ingest.ENTITLEMENT_COLUMNS) for row in batch.entitlements],
    )
    db.executemany(
        ingest.USAGE_UPSERT,
        [ingest.bind(row, ingest.USAGE_COLUMNS) for row in batch.usage],
    )
    db.commit()


def valid_payload(**overrides):
    payload = {
        "agent_id": "agent-1",
        "schema_version": 1,
        "entitlements": [{
            "observed_at": "2026-07-30T00:00:00+00:00", "provider": "codex",
            "account_id": "A", "window": "weekly", "used_percent": 10.0,
            "resets_at": "2026-08-05T00:00:00+00:00", "duration_minutes": 10080,
            "source": "test",
        }],
        "usage": [{
            "import_key": "1", "imported_at": "2026-07-30T00:00:00+00:00",
            "last_seen_at": "2026-07-30T00:00:00+00:00", "provider": "codex",
            "account_id": "A", "period_start": "2026-07-30", "model": None,
            "input_tokens": 1, "cached_input_tokens": 0, "cache_write_tokens": 0,
            "cache_read_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0,
            "reported_cost_usd": "1.50", "source_path": "$",
        }],
    }
    payload.update(overrides)
    return payload


# --- validation ---------------------------------------------------------------------

def test_valid_batch_parses():
    batch = ingest.parse(valid_payload())
    assert batch.agent_id == "agent-1"
    assert batch.entitlements[0]["account_key"] == "A"
    assert batch.usage[0]["model_key"] == ""  # aggregate row


@pytest.mark.parametrize("used", [-1.0, 100.1, 1e9])
def test_quota_outside_range_is_rejected(used):
    payload = valid_payload()
    payload["entitlements"][0]["used_percent"] = used
    with pytest.raises(ingest.IngestError) as caught:
        ingest.parse(payload)
    assert caught.value.status == 400


def test_negative_tokens_are_rejected():
    payload = valid_payload()
    payload["usage"][0]["output_tokens"] = -5
    with pytest.raises(ingest.IngestError) as caught:
        ingest.parse(payload)
    assert caught.value.status == 400


def test_non_decimal_cost_is_rejected():
    payload = valid_payload()
    payload["usage"][0]["reported_cost_usd"] = "about three dollars"
    with pytest.raises(ingest.IngestError) as caught:
        ingest.parse(payload)
    assert caught.value.status == 400


def test_unknown_provider_is_rejected():
    payload = valid_payload()
    payload["entitlements"][0]["provider"] = "gemini"
    with pytest.raises(ingest.IngestError) as caught:
        ingest.parse(payload)
    assert caught.value.status == 400


def test_future_schema_version_conflicts():
    with pytest.raises(ingest.IngestError) as caught:
        ingest.parse(valid_payload(schema_version=99))
    assert caught.value.status == 409


def test_oversized_batch_is_rejected():
    payload = valid_payload()
    payload["usage"] = payload["usage"] * (ingest.MAX_USAGE_ROWS + 1)
    with pytest.raises(ingest.IngestError) as caught:
        ingest.parse(payload)
    assert caught.value.status == 413


def test_rejection_writes_nothing():
    db = server_db()
    payload = valid_payload()
    payload["entitlements"][0]["used_percent"] = 500.0
    with pytest.raises(ingest.IngestError):
        store(db, ingest.parse(payload))
    assert db.execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM usage_rows").fetchone()[0] == 0


# --- idempotency --------------------------------------------------------------------

def test_repeated_push_does_not_duplicate():
    db = server_db()
    batch = ingest.parse(valid_payload())
    store(db, batch)
    store(db, batch)
    assert db.execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM usage_rows").fetchone()[0] == 1


def test_null_account_and_model_still_deduplicate():
    # The defect this guards: SQLite allows NULL in a non-integer PRIMARY KEY and does not
    # treat NULL-differing rows as conflicting, so keying on the nullable columns would
    # make every re-push insert duplicates instead of updating.
    db = server_db()
    payload = valid_payload()
    payload["entitlements"][0]["account_id"] = None
    payload["usage"][0]["account_id"] = None
    payload["usage"][0]["model"] = None
    batch = ingest.parse(payload)
    store(db, batch)
    store(db, batch)
    assert db.execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM usage_rows").fetchone()[0] == 1


# --- round trip ---------------------------------------------------------------------

def test_server_derives_the_same_estimates_as_the_local_pipeline(tmp_path: Path) -> None:
    """The test that catches the estimator forking.

    Evidence is collected locally, pushed, stored server-side and re-derived. If the two
    paths ever disagree -- a numerator rule applied in one place and not the other, a
    staleness bound that drifts -- the estimates diverge here.
    """
    local = connect(tmp_path / "local.sqlite3")
    for index in range(8):
        stamp = f"2026-07-30T0{index}:00:00+00:00"
        cursor = local.execute(
            "INSERT INTO imports (imported_at, provider, report, account_id, command, "
            "payload_sha256, raw_json, last_seen_at) VALUES (?, 'codex', 'daily', 'A', NULL, ?, '{}', ?)",
            (stamp, f"hash-{index}", stamp),
        )
        local.execute(
            """INSERT INTO usage_rows (
                import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens,
                cache_read_tokens, output_tokens, reasoning_output_tokens,
                reported_cost_usd, source_path
            ) VALUES (?, 'codex', 'daily', '2026-07-30', NULL, NULL, 1, 0, 0, 0, 1, 0, ?, '$')""",
            (cursor.lastrowid, str(2.0 + index * 1.5)),
        )
        local.execute(
            """INSERT INTO entitlement_snapshots
               (observed_at, provider, account_id, window, used_percent, resets_at,
                duration_minutes, source)
               VALUES (?, 'codex', 'A', 'weekly', ?, '2026-08-05T00:00:00+00:00', 10080, 'test')""",
            (stamp, float(index * 9)),
        )
    local.commit()

    local_estimates = robust_estimates(regression_points(local, "codex"))
    assert local_estimates, "fixture must produce at least one estimate"

    payload = build_payload(
        "agent-1",
        pending_entitlements(local, None, 5000),
        pending_usage(local, None, 5000),
    )
    remote = server_db()
    store(remote, ingest.parse(json.loads(json.dumps(payload))))

    points = [dict(row) for row in remote.execute(
        queries.REGRESSION_POINTS_SQL, queries.points_params("agent-1")
    )]
    remote_estimates = robust_estimates(points)

    assert len(remote_estimates) == len(local_estimates)
    for expected, actual in zip(local_estimates, remote_estimates):
        assert round(actual.estimate_usd, 6) == round(expected.estimate_usd, 6)
        assert actual.slope_count == expected.slope_count
        assert actual.covered_quota_percent == expected.covered_quota_percent
        assert actual.observation_count == expected.observation_count


def test_current_payload_carries_a_tier(tmp_path: Path) -> None:
    db = server_db()
    store(db, ingest.parse(valid_payload()))
    payload = queries.current_payload([])
    assert payload["current"] == []
    assert payload["regime_changes"] == []
