from pathlib import Path

from subbench.regression import robust_estimates
from subbench.store import connect, regression_points, save_import
from subbench.timeseries import rolling_values


def _record(db, *, timestamp, used, cost, account_id, provider="codex", reset="2026-07-08T00:00:00+00:00", reset_at=None):
    cursor = db.execute(
        "INSERT INTO imports (imported_at, provider, report, account_id, command, payload_sha256, raw_json) "
        "VALUES (?, ?, 'daily', ?, NULL, ?, '{}')",
        (timestamp, provider, account_id, f"hash-{account_id}-{timestamp}"),
    )
    db.execute(
        """INSERT INTO usage_rows (
            import_id, provider, report, period_start, period_end, model,
            input_tokens, cached_input_tokens, cache_write_tokens,
            cache_read_tokens, output_tokens, reasoning_output_tokens,
            reported_cost_usd, source_path
        ) VALUES (?, ?, 'daily', NULL, NULL, 'gpt-test', 0, 0, 0, 0, 0, 0, ?, '$')""",
        (cursor.lastrowid, provider, str(cost)),
    )
    db.execute(
        """INSERT INTO entitlement_snapshots
           (observed_at, provider, account_id, window, used_percent, resets_at, duration_minutes, source)
           VALUES (?, ?, ?, 'weekly', ?, ?, 10080, 'test')""",
        (timestamp, provider, account_id, used, reset_at or reset),
    )


def test_database_to_rolling_estimate(tmp_path: Path) -> None:
    db = connect(tmp_path / "subbench.sqlite3")
    observations = [
        ("2026-07-01T00:00:00+00:00", 10.0, 1.0, "acct-A"),
        ("2026-07-01T01:00:00+00:00", 30.0, 5.0, "acct-A"),
        ("2026-07-01T02:00:00+00:00", 60.0, 11.0, "acct-A"),
    ]
    for timestamp, used, cost, account_id in observations:
        _record(db, timestamp=timestamp, used=used, cost=cost, account_id=account_id)
    db.commit()

    estimates = robust_estimates(regression_points(db, "codex"))
    assert len(estimates) == 1
    assert estimates[0].account_id == "acct-A"
    assert round(estimates[0].estimate_usd, 2) == 20.0

    current = rolling_values(estimates)
    assert len(current) == 1
    assert current[0].account_scope == "account"
    assert current[0].account_id == "acct-A"
    assert round(current[0].estimate_usd, 2) == 20.0


def test_accounts_remain_separate_regression_groups(tmp_path: Path) -> None:
    db = connect(tmp_path / "subbench.sqlite3")
    # Account A burns through quota with a higher implied full-window value than account B.
    _record(db, timestamp="2026-07-01T00:00:00+00:00", account_id="A", used=10.0, cost=1.0)
    _record(db, timestamp="2026-07-01T01:00:00+00:00", account_id="A", used=60.0, cost=11.0)
    _record(db, timestamp="2026-07-01T00:00:00+00:00", account_id="B", used=20.0, cost=1.0, reset="2026-07-09T00:00:00+00:00")
    _record(db, timestamp="2026-07-01T01:00:00+00:00", account_id="B", used=70.0, cost=4.0, reset="2026-07-09T00:00:00+00:00")
    db.commit()

    estimates = robust_estimates(regression_points(db, "codex"))
    by_account = {estimate.account_id: estimate for estimate in estimates}
    assert set(by_account) == {"A", "B"}
    # Without scoping account A's high cost would inflate B's slope; check it stays isolated.
    assert round(by_account["A"].estimate_usd, 2) == 20.0
    assert round(by_account["B"].estimate_usd, 2) == 6.0


def test_provider_pooled_rollup_combines_accounts(tmp_path: Path) -> None:
    db = connect(tmp_path / "subbench.sqlite3")
    # Two accounts each with informative windows of the same magnitude.
    for day in range(1, 5):
        _record(
            db,
            timestamp=f"2026-07-0{day}T00:00:00+00:00",
            account_id="A",
            used=10.0 * day,
            cost=2.0 * day,
            reset="2026-07-08T00:00:00+00:00",
        )
    for day in range(1, 5):
        _record(
            db,
            timestamp=f"2026-07-1{day}T00:00:00+00:00",
            account_id="B",
            used=10.0 * day,
            cost=2.0 * day,
            reset="2026-07-15T00:00:00+00:00",
        )
    db.commit()

    estimates = robust_estimates(regression_points(db, "codex"))
    current = rolling_values(estimates)
    scopes = {(row.account_scope, row.account_id) for row in current}
    assert ("account", "A") in scopes
    assert ("account", "B") in scopes
    assert ("plan", None) in scopes


def test_nearby_codex_reset_timestamps_stay_in_one_window(tmp_path: Path) -> None:
    db = connect(tmp_path / "subbench.sqlite3")
    # The quota span has to clear the pairwise floor; this case is about the two
    # near-identical reset timestamps grouping into one window, not the slope value.
    observations = [
        ("2026-07-01T00:00:00+00:00", 12.0, 1.0, "2026-07-08T03:12:56+00:00"),
        ("2026-07-01T00:02:00+00:00", 22.0, 11.0, "2026-07-08T03:12:55+00:00"),
    ]
    for index, (timestamp, used, cost, reset) in enumerate(observations, start=1):
        cursor = db.execute(
            "INSERT INTO imports (imported_at, provider, report, account_id, command, payload_sha256, raw_json) "
            "VALUES (?, 'codex', 'daily', 'acct-A', NULL, ?, '{}')",
            (timestamp, f"hash-{index}"),
        )
        db.execute(
            """INSERT INTO usage_rows (
                import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens,
                cache_read_tokens, output_tokens, reasoning_output_tokens,
                reported_cost_usd, source_path
            ) VALUES (?, 'codex', 'daily', NULL, NULL, NULL, 0, 0, 0, 0, 0, 0, ?, '$')""",
            (cursor.lastrowid, str(cost)),
        )
        db.execute(
            """INSERT INTO entitlement_snapshots
               (observed_at, provider, account_id, window, used_percent, resets_at, duration_minutes, source)
               VALUES (?, 'codex', 'acct-A', 'weekly', ?, ?, 10080, 'test')""",
            (timestamp, used, reset),
        )
    db.commit()

    estimates = robust_estimates(regression_points(db, "codex"))
    assert len(estimates) == 1
    assert estimates[0].observation_count == 2
    assert round(estimates[0].estimate_usd, 2) == 100.0


def test_dropped_periods_flags_a_shrinking_ccusage_report(tmp_path: Path) -> None:
    from subbench.ccusage import UsageRow
    from subbench.store import dropped_periods

    db = connect(tmp_path / "subbench.sqlite3")

    def row(period: str) -> UsageRow:
        return UsageRow(
            provider="codex", report="daily", period_start=period, period_end=None,
            model=None, input_tokens=1, cached_input_tokens=0, cache_write_tokens=0,
            cache_read_tokens=0, output_tokens=1, reasoning_output_tokens=0,
            reported_cost_usd="1.0", source_path=f"$.daily[{period}]",
        )

    full = [row("2026-07-01"), row("2026-07-02"), row("2026-07-03")]
    save_import(db, raw=b'{"a": 1}', payload={"a": 1}, rows=full,
                provider="codex", report="daily", command=None, account_id="acct-A")

    short = [row("2026-07-01"), row("2026-07-03")]
    assert dropped_periods(db, provider="codex", account_id="acct-A", rows=short) == {"2026-07-02"}
    assert dropped_periods(db, provider="codex", account_id="acct-A", rows=full) == set()
    # A brand new day appearing is growth, not shrinkage.
    grown = [*full, row("2026-07-04")]
    assert dropped_periods(db, provider="codex", account_id="acct-A", rows=grown) == set()
    # Another account's history must not be compared against this one.
    assert dropped_periods(db, provider="codex", account_id="acct-B", rows=short) == set()
