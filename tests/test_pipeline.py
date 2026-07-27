from pathlib import Path

from subbench.regression import robust_estimates
from subbench.store import connect, regression_points
from subbench.timeseries import rolling_values


def test_database_to_rolling_estimate(tmp_path: Path) -> None:
    db = connect(tmp_path / "subbench.sqlite3")
    observations = [
        ("2026-07-01T00:00:00+00:00", 10.0, 1.0),
        ("2026-07-01T01:00:00+00:00", 30.0, 5.0),
        ("2026-07-01T02:00:00+00:00", 60.0, 11.0),
    ]
    for index, (timestamp, used, cost) in enumerate(observations, start=1):
        cursor = db.execute(
            "INSERT INTO imports (imported_at, provider, report, command, payload_sha256, raw_json) VALUES (?, 'codex', 'daily', NULL, ?, '{}')",
            (timestamp, f"hash-{index}"),
        )
        db.execute(
            """INSERT INTO usage_rows (
                import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens,
                cache_read_tokens, output_tokens, reasoning_output_tokens,
                reported_cost_usd, source_path
            ) VALUES (?, 'codex', 'daily', NULL, NULL, 'gpt-test', 0, 0, 0, 0, 0, 0, ?, '$')""",
            (cursor.lastrowid, str(cost)),
        )
        db.execute(
            """INSERT INTO entitlement_snapshots
               (observed_at, provider, window, used_percent, resets_at, duration_minutes, source)
               VALUES (?, 'codex', 'weekly', ?, '2026-07-08T00:00:00+00:00', 10080, 'test')""",
            (timestamp, used),
        )
    db.commit()

    estimates = robust_estimates(regression_points(db, "codex"))
    assert len(estimates) == 1
    assert round(estimates[0].estimate_usd, 2) == 20.0

    current = rolling_values(estimates)
    assert len(current) == 1
    assert round(current[0].estimate_usd, 2) == 20.0
