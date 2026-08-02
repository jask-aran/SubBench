import urllib.error
from pathlib import Path

import pytest

from subbench import push as push_module
from subbench.push import (
    MAX_USAGE_ROWS_PER_BATCH,
    push_all,
    push_once,
    push_state,
)
from subbench.store import connect

URL = "https://example.invalid/ingest"


def _seed_at(db, stamp, *, provider="codex", account_id="acct-A"):
    cursor = db.execute(
        "INSERT INTO imports (imported_at, provider, report, account_id, command, "
        "payload_sha256, raw_json, last_seen_at) VALUES (?, ?, 'daily', ?, NULL, ?, '{}', ?)",
        (stamp, provider, account_id, f"hash-{stamp}-{account_id}", stamp),
    )
    db.execute(
        """INSERT INTO usage_rows (
            import_id, provider, report, period_start, period_end, model,
            input_tokens, cached_input_tokens, cache_write_tokens,
            cache_read_tokens, output_tokens, reasoning_output_tokens,
            reported_cost_usd, source_path
        ) VALUES (?, ?, 'daily', '2026-07-30', NULL, NULL, 1, 0, 0, 0, 1, 0, '1.0', '$')""",
        (cursor.lastrowid, provider),
    )
    db.execute(
        """INSERT INTO entitlement_snapshots
           (observed_at, provider, account_id, window, used_percent, resets_at,
            duration_minutes, source)
           VALUES (?, ?, ?, 'weekly', 50.0, '2026-08-05T00:00:00+00:00', 10080, 'test')""",
        (stamp, provider, account_id),
    )
    db.commit()


def _seed(db, *, count=3, provider="codex", account_id="acct-A"):
    for index in range(count):
        stamp = f"2026-07-30T00:{index:02d}:00+00:00"
        cursor = db.execute(
            "INSERT INTO imports (imported_at, provider, report, account_id, command, "
            "payload_sha256, raw_json, last_seen_at) VALUES (?, ?, 'daily', ?, NULL, ?, '{}', ?)",
            (stamp, provider, account_id, f"hash-{index}", stamp),
        )
        db.execute(
            """INSERT INTO usage_rows (
                import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens,
                cache_read_tokens, output_tokens, reasoning_output_tokens,
                reported_cost_usd, source_path
            ) VALUES (?, ?, 'daily', '2026-07-30', NULL, NULL, 1, 0, 0, 0, 1, 0, ?, '$')""",
            (cursor.lastrowid, provider, str(index + 1.0)),
        )
        db.execute(
            """INSERT INTO entitlement_snapshots
               (observed_at, provider, account_id, window, used_percent, resets_at,
                duration_minutes, source)
               VALUES (?, ?, ?, 'weekly', ?, '2026-08-05T00:00:00+00:00', 10080, 'test')""",
            (stamp, provider, account_id, float(index * 10)),
        )
    db.commit()


class Recorder:
    def __init__(self, fail=False):
        self.payloads = []
        self.fail = fail

    def __call__(self, url, token, payload, timeout):
        if self.fail:
            raise urllib.error.URLError("connection refused")
        self.payloads.append(payload)
        return {"accepted": {"entitlements": len(payload["entitlements"]), "usage": len(payload["usage"])}}


def test_agent_id_is_stable_across_calls(tmp_path: Path) -> None:
    db = connect(tmp_path / "s.sqlite3")
    first = push_state(db, URL)
    assert push_state(db, URL).agent_id == first.agent_id


def test_default_push_sends_measurements_and_reports_without_raw_usage(tmp_path: Path) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    sender = Recorder()
    result = push_once(db, url=URL, token="t", sender=sender)
    assert (result.sent_entitlements, result.sent_usage) == (3, 0)
    assert result.drained
    payload = sender.payloads[0]
    assert payload["schema_version"] == 1
    assert payload["agent_id"] == push_state(db, URL).agent_id
    assert payload["usage"] == []
    assert set(payload["reports"]) == {"current", "history", "models", "weights", "series"}


def test_raw_usage_requires_explicit_opt_in(tmp_path: Path, monkeypatch) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    monkeypatch.setenv("SUBBENCH_PUSH_RAW_USAGE", "1")
    sender = Recorder()
    result = push_once(db, url=URL, token="t", sender=sender)
    assert (result.sent_entitlements, result.sent_usage) == (3, 3)
    assert result.drained
    payload = sender.payloads[0]
    assert payload["schema_version"] == 1
    assert payload["agent_id"] == push_state(db, URL).agent_id
    # Raw payloads are deliberately not sent.
    assert "raw_json" not in payload["usage"][0]


def test_normal_push_leaves_existing_raw_cursor_unchanged(tmp_path: Path, monkeypatch) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db, count=1)
    monkeypatch.setenv("SUBBENCH_PUSH_RAW_USAGE", "1")
    sender = Recorder()
    push_once(db, url=URL, token="t", sender=sender)
    before = push_state(db, URL).usage_cursor

    monkeypatch.delenv("SUBBENCH_PUSH_RAW_USAGE")
    _seed_at(db, "2026-07-31T10:00:00+00:00", account_id="acct-B")
    push_once(db, url=URL, token="t", sender=sender)

    assert push_state(db, URL).usage_cursor == before


def test_second_push_sends_nothing_new(tmp_path: Path) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    sender = Recorder()
    push_once(db, url=URL, token="t", sender=sender)
    result = push_once(db, url=URL, token="t", sender=sender)
    assert (result.sent_entitlements, result.sent_usage) == (0, 0)
    assert len(sender.payloads) == 1


def test_only_new_evidence_is_sent_after_a_cursor_advance(tmp_path: Path, monkeypatch) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db, count=2)
    monkeypatch.setenv("SUBBENCH_PUSH_RAW_USAGE", "1")
    sender = Recorder()
    push_once(db, url=URL, token="t", sender=sender)
    _seed_at(db, "2026-07-31T10:00:00+00:00", account_id="acct-B")
    result = push_once(db, url=URL, token="t", sender=sender)
    assert (result.sent_entitlements, result.sent_usage) == (1, 1)
    assert [row["account_id"] for row in sender.payloads[1]["usage"]] == ["acct-B"]


def test_a_batch_never_splits_an_import(tmp_path: Path) -> None:
    # Every row of an import shares its last_seen_at. If a row-limited batch ended
    # mid-import the cursor would advance past that timestamp and the remaining rows
    # would never be sent again.
    db = connect(tmp_path / "s.sqlite3")
    cursor = db.execute(
        "INSERT INTO imports (imported_at, provider, report, account_id, command, "
        "payload_sha256, raw_json, last_seen_at) VALUES "
        "('2026-07-30T00:00:00+00:00','codex','daily','acct-A',NULL,'h','{}','2026-07-30T00:00:00+00:00')"
    )
    for index in range(30):
        db.execute(
            """INSERT INTO usage_rows (
                import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens,
                cache_read_tokens, output_tokens, reasoning_output_tokens,
                reported_cost_usd, source_path
            ) VALUES (?, 'codex', 'daily', '2026-07-30', NULL, NULL, 1, 0, 0, 0, 1, 0, '1.0', ?)""",
            (cursor.lastrowid, f"$.daily[{index}]"),
        )
    db.commit()
    from subbench.push import pending_usage
    # A limit far below the import size must still yield the whole import.
    assert len(pending_usage(db, None, limit=5)) == 30


def test_failed_opt_in_push_leaves_the_cursor_untouched(tmp_path: Path, monkeypatch) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    monkeypatch.setenv("SUBBENCH_PUSH_RAW_USAGE", "1")
    failing = Recorder(fail=True)
    result = push_once(db, url=URL, token="t", sender=failing)
    assert result.sent_entitlements == 0
    assert not result.drained
    assert "push failed" in result.message
    # The same evidence must still be pending, or a network blip becomes a permanent gap.
    working = Recorder()
    retry = push_once(db, url=URL, token="t", sender=working)
    assert (retry.sent_entitlements, retry.sent_usage) == (3, 3)


def test_failure_is_recorded_for_diagnosis(tmp_path: Path) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db)
    push_once(db, url=URL, token="t", sender=Recorder(fail=True))
    row = db.execute("SELECT last_error FROM push_state WHERE endpoint = ?", (URL,)).fetchone()
    assert row["last_error"]


def test_push_all_drains_in_batches(tmp_path: Path, monkeypatch) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db, count=4)
    monkeypatch.setenv("SUBBENCH_PUSH_RAW_USAGE", "1")
    sender = Recorder()
    result = push_all(db, url=URL, token="t", sender=sender)
    assert result.drained
    assert result.sent_usage == 4


def test_opt_in_push_all_preserves_usage_batching(tmp_path: Path, monkeypatch) -> None:
    db = connect(tmp_path / "s.sqlite3")
    _seed(db, count=5)
    monkeypatch.setenv("SUBBENCH_PUSH_RAW_USAGE", "1")
    monkeypatch.setattr(push_module, "MAX_USAGE_ROWS_PER_BATCH", 2)
    sender = Recorder()

    result = push_all(db, url=URL, token="t", sender=sender)

    assert result.drained
    assert result.sent_usage == 5
    assert [len(payload["usage"]) for payload in sender.payloads] == [2, 2, 1]
    assert push_state(db, URL).usage_cursor == "2026-07-30T00:04:00+00:00"


def test_nothing_to_push_is_not_an_error(tmp_path: Path) -> None:
    db = connect(tmp_path / "s.sqlite3")
    result = push_once(db, url=URL, token="t", sender=Recorder())
    assert result.drained
    assert result.message == "nothing to push"


def test_opt_in_usage_is_ordered_by_confirmation_not_import_time(tmp_path: Path, monkeypatch) -> None:
    # A deduplicated ccusage payload stops advancing imported_at while last_seen_at keeps
    # moving. Cursoring on imported_at would strand the re-confirmation.
    db = connect(tmp_path / "s.sqlite3")
    _seed(db, count=1)
    monkeypatch.setenv("SUBBENCH_PUSH_RAW_USAGE", "1")
    sender = Recorder()
    push_once(db, url=URL, token="t", sender=sender)
    db.execute("UPDATE imports SET last_seen_at = '2026-07-30T09:00:00+00:00'")
    db.commit()
    result = push_once(db, url=URL, token="t", sender=sender)
    assert result.sent_usage == 1
    assert sender.payloads[-1]["usage"][0]["last_seen_at"] == "2026-07-30T09:00:00+00:00"


def test_batch_size_is_bounded() -> None:
    assert MAX_USAGE_ROWS_PER_BATCH <= 5000
