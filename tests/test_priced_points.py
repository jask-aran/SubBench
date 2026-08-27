"""Pricing a reading is kept rather than repeated, so the kept answer must be the same one."""
from subbench.store import (
    MAX_COST_AGE_MINUTES,
    PRICED_WINDOWS_SQL,
    connect,
    refresh_priced_points,
    regression_points,
)

DIRECT = PRICED_WINDOWS_SQL + """
    SELECT p.provider, p.account_id, p.window, p.observed_at, p.used_percent,
           p.resets_at, p.duration_minutes, p.plan, p.cost_usd, p.cost_age_minutes
    FROM priced p WHERE p.cost_age_minutes <= ?
    ORDER BY p.provider, p.account_id, p.window, p.resets_at, p.observed_at"""


def _import(db, stamp, cost, *, account_id="acct-A", digest=None):
    cursor = db.execute(
        "INSERT INTO imports (imported_at, provider, report, account_id, command, "
        "payload_sha256, raw_json, last_seen_at) VALUES (?, 'codex', 'daily', ?, NULL, ?, '{}', ?)",
        (stamp, account_id, digest or f"hash-{stamp}-{account_id}", stamp),
    )
    db.execute(
        """INSERT INTO usage_rows (import_id, provider, report, period_start, period_end, model,
            input_tokens, cached_input_tokens, cache_write_tokens, cache_read_tokens,
            output_tokens, reasoning_output_tokens, reported_cost_usd, source_path)
           VALUES (?, 'codex', 'daily', '2026-08-01', NULL, NULL, 1, 0, 0, 0, 1, 0, ?, '$')""",
        (cursor.lastrowid, str(cost)),
    )
    return cursor.lastrowid


def _reading(db, stamp, used, *, account_id="acct-A"):
    db.execute(
        """INSERT INTO entitlement_snapshots (observed_at, provider, account_id, window,
            used_percent, resets_at, duration_minutes, source, plan)
           VALUES (?, 'codex', ?, 'weekly', ?, '2026-08-05T00:00:00+00:00', 10080, 'test', 'plus')""",
        (stamp, account_id, used),
    )


def _seed(db):
    for index, (stamp, used, cost) in enumerate([
        ("2026-08-01T00:00:00+00:00", 10.0, 10.0),
        ("2026-08-01T02:00:00+00:00", 30.0, 30.0),
        ("2026-08-01T04:00:00+00:00", 50.0, 50.0),
    ]):
        _import(db, stamp, cost)
        _reading(db, stamp, used)
    db.commit()


def _direct(db):
    return [tuple(row) for row in db.execute(DIRECT, (MAX_COST_AGE_MINUTES,))]


def test_kept_prices_match_pricing_every_time(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    assert [tuple(row) for row in regression_points(db)] == _direct(db)


def test_a_second_refresh_does_no_work(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    assert refresh_priced_points(db) == (0, 3)
    assert refresh_priced_points(db) == (0, 0)


def test_a_new_reading_is_priced_without_repricing_the_rest(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    refresh_priced_points(db)
    _import(db, "2026-08-01T06:00:00+00:00", 70.0)
    _reading(db, "2026-08-01T06:00:00+00:00", 70.0)
    db.commit()
    assert refresh_priced_points(db) == (0, 1)
    assert [tuple(row) for row in regression_points(db)] == _direct(db)


def test_an_import_that_overtakes_a_reading_reprices_it(tmp_path):
    """A reading is priced from the newest import at or before it, so a later arrival that
    still predates the reading takes over, and only the readings it overtakes change."""
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    _reading(db, "2026-08-01T02:20:00+00:00", 40.0)
    db.commit()
    refresh_priced_points(db)
    before = {row["observed_at"]: row["cost_usd"] for row in regression_points(db)}

    # Seen at 02:10, so it becomes the newest import at or before the 02:20 reading. It is
    # below the timestamp mark from the last refresh, and is caught by its id instead.
    _import(db, "2026-07-31T00:00:00+00:00", 1.0, digest="overtaking")
    db.execute(
        "UPDATE imports SET last_seen_at = ? WHERE payload_sha256 = ?",
        ("2026-08-01T02:10:00+00:00", "overtaking"),
    )
    db.commit()

    stale, added = refresh_priced_points(db)
    assert (stale, added) == (1, 1)
    after = {row["observed_at"]: row["cost_usd"] for row in regression_points(db)}
    assert after["2026-08-01T02:20:00+00:00"] != before["2026-08-01T02:20:00+00:00"]
    # The reading after it was already priced from a newer import and is untouched.
    assert after["2026-08-01T04:00:00+00:00"] == before["2026-08-01T04:00:00+00:00"]
    assert [tuple(row) for row in regression_points(db)] == _direct(db)


def test_pricing_is_kept_per_account(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    _import(db, "2026-08-01T02:00:00+00:00", 99.0, account_id="acct-B")
    _reading(db, "2026-08-01T02:00:00+00:00", 20.0, account_id="acct-B")
    db.commit()
    assert [tuple(row) for row in regression_points(db)] == _direct(db)
