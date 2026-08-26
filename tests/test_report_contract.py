"""The dashboard reads only the history report, so these fields are a public contract.

The page is static HTML with no build step and no type checking. If a field is renamed
here, nothing fails until the page is open in a browser and a number is missing, so the
contract is asserted instead.
"""
from subbench.push import build_reports
from subbench.store import connect

# Exactly what src/subbench/server/static/index.html reads from a history window row.
PAGE_FIELDS = {
    "provider",
    "account_id",
    "plan",
    "product",
    "window",
    "reset_key",
    "estimate_usd",
    "tier",
}

# Exactly what the page reads from a pooled product row.
PRODUCT_FIELDS = {"product", "window", "estimate_usd", "window_count", "account_count"}

# Exactly what the page reads from a pooled trend point.
SERIES_FIELDS = {"product", "window", "period_start", "estimate_usd", "window_count", "account_count"}


def _seed(db, *, account_id="acct-A", plan="plus", provider="codex"):
    """Three readings where both the meter and the recorded spend advance."""
    for index, (stamp, used, cost) in enumerate([
        ("2026-08-01T00:00:00+00:00", 10.0, 10.0),
        ("2026-08-01T02:00:00+00:00", 30.0, 30.0),
        ("2026-08-01T04:00:00+00:00", 50.0, 50.0),
    ]):
        cursor = db.execute(
            "INSERT INTO imports (imported_at, provider, report, account_id, command, "
            "payload_sha256, raw_json, last_seen_at) VALUES (?, ?, 'daily', ?, NULL, ?, '{}', ?)",
            (stamp, provider, account_id, f"hash-{account_id}-{index}", stamp),
        )
        db.execute(
            """INSERT INTO usage_rows (
                import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens,
                cache_read_tokens, output_tokens, reasoning_output_tokens,
                reported_cost_usd, source_path
            ) VALUES (?, ?, 'daily', '2026-08-01', NULL, NULL, 1, 0, 0, 0, 1, 0, ?, '$')""",
            (cursor.lastrowid, provider, str(cost)),
        )
        db.execute(
            """INSERT INTO entitlement_snapshots
               (observed_at, provider, account_id, window, used_percent, resets_at,
                duration_minutes, source, plan)
               VALUES (?, ?, ?, 'weekly', ?, '2026-08-05T00:00:00+00:00', 10080, 'test', ?)""",
            (stamp, provider, account_id, used, plan),
        )
    db.commit()


def test_history_rows_carry_every_field_the_page_reads(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    windows = build_reports(db)["history"]["windows"]
    assert windows
    for row in windows:
        assert PAGE_FIELDS <= set(row), PAGE_FIELDS - set(row)


def test_history_carries_a_pooled_product_block(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    assert isinstance(build_reports(db)["history"]["products"], list)


def test_the_product_name_comes_from_the_reported_plan(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db, plan="pro")
    products = {row["product"] for row in build_reports(db)["history"]["windows"]}
    assert products == {"ChatGPT Pro"}


def test_an_absent_plan_names_the_provider_alone(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db, provider="claude", account_id=None, plan=None)
    products = {row["product"] for row in build_reports(db)["history"]["windows"]}
    assert products == {"Claude"}


def test_history_carries_a_pooled_trend_series(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    assert isinstance(build_reports(db)["history"]["product_series"], list)


def test_pooled_trend_points_carry_every_field_the_page_reads(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    for row in build_reports(db)["history"]["product_series"]:
        assert SERIES_FIELDS <= set(row), SERIES_FIELDS - set(row)


def test_pooled_product_rows_carry_every_field_the_page_reads(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    # Pooling only accepts confirmed, completed windows, so the block may be empty here.
    # The contract is on the shape of a row, whenever one is present.
    for row in build_reports(db)["history"]["products"]:
        assert PRODUCT_FIELDS <= set(row), PRODUCT_FIELDS - set(row)


def test_no_source_file_contains_a_null_byte():
    """A stray NUL makes grep treat a text file as binary and print nothing.

    One reached index.html through an editing mistake and sat there through two commits:
    it was harmless at runtime, but it made later patches match nothing and fail silently,
    and it hid the file from every search that would have shown why.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    suspect = [
        path
        for folder in ("src", "tests", "worker")
        for path in (root / folder).rglob("*")
        if path.is_file() and path.suffix in {".py", ".js", ".mjs", ".html", ".sql", ".json"}
        and b"\x00" in path.read_bytes()
    ]
    assert suspect == [], suspect
