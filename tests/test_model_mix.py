from pathlib import Path

from subbench.store import connect, model_mix


def test_mix_does_not_add_reasoning_already_inside_output(tmp_path: Path) -> None:
    db = connect(tmp_path / "s.sqlite3")
    stamp = "2026-08-01T04:00:00+00:00"
    cursor = db.execute(
        "INSERT INTO imports (imported_at, provider, report, account_id, command, "
        "payload_sha256, raw_json, last_seen_at) VALUES (?, 'codex', 'daily', 'acct-A', "
        "NULL, 'h', '{}', ?)",
        (stamp, stamp),
    )
    db.execute(
        """INSERT INTO usage_rows (import_id, provider, report, period_start, period_end, model,
            input_tokens, cached_input_tokens, cache_write_tokens, cache_read_tokens,
            output_tokens, reasoning_output_tokens, reported_cost_usd, source_path)
           VALUES (?, 'codex', 'daily', '2026-08-01', NULL, 'gpt-5.6-sol',
                   100, 0, 0, 500, 20, 8, NULL, '$')""",
        (cursor.lastrowid,),
    )
    db.execute(
        """INSERT INTO entitlement_snapshots (observed_at, provider, account_id, window,
            used_percent, resets_at, duration_minutes, source, plan)
           VALUES (?, 'codex', 'acct-A', 'weekly', 50.0, '2026-08-05T00:00:00+00:00',
                   10080, 'test', 'plus')""",
        (stamp,),
    )
    db.commit()

    rows = model_mix(db, provider="codex")
    assert len(rows) == 1
    # 100 fresh + 500 cache read + 20 output. The 8 reasoning tokens are already in output.
    assert int(rows[0]["total_tokens"]) == 620
