import json

import pytest

from subbench.cli import expand_aliases, main
from subbench.store import connect


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """Never read the real machine's configuration during a test."""
    monkeypatch.setenv("SUBBENCH_CONFIG", str(tmp_path / "absent.env"))
    monkeypatch.delenv("SUBBENCH_PUSH_URL", raising=False)
    monkeypatch.delenv("SUBBENCH_PUSH_TOKEN", raising=False)


def _database(tmp_path):
    path = tmp_path / "s.sqlite3"
    db = connect(path)
    for index, (stamp, used, cost) in enumerate([
        ("2026-08-01T00:00:00+00:00", 10.0, 10.0),
        ("2026-08-01T02:00:00+00:00", 30.0, 30.0),
        ("2026-08-01T04:00:00+00:00", 50.0, 50.0),
    ]):
        cursor = db.execute(
            "INSERT INTO imports (imported_at, provider, report, account_id, command, "
            "payload_sha256, raw_json, last_seen_at) VALUES (?, 'codex', 'daily', 'acct-A', NULL, ?, '{}', ?)",
            (stamp, f"hash-{index}", stamp),
        )
        db.execute(
            """INSERT INTO usage_rows (import_id, provider, report, period_start, period_end, model,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_read_tokens,
                output_tokens, reasoning_output_tokens, reported_cost_usd, source_path)
               VALUES (?, 'codex', 'daily', '2026-08-01', NULL, NULL, 1, 0, 0, 0, 1, 0, ?, '$')""",
            (cursor.lastrowid, str(cost)),
        )
        db.execute(
            """INSERT INTO entitlement_snapshots (observed_at, provider, account_id, window,
                used_percent, resets_at, duration_minutes, source, plan)
               VALUES (?, 'codex', 'acct-A', 'weekly', ?, '2026-08-05T00:00:00+00:00', 10080, 'test', 'plus')""",
            (stamp, used),
        )
    db.commit()
    db.close()
    return path


def run(tmp_path, *argv):
    return main(["--database", str(_database(tmp_path)), *argv])


def test_old_command_names_still_work():
    assert expand_aliases(["push"]) == ["sync", "push"]
    assert expand_aliases(["imports"]) == ["data", "imports"]
    assert expand_aliases(["accounts", "--provider", "codex"]) == ["detail", "accounts", "--provider", "codex"]
    assert expand_aliases(["--database", "/x", "push"]) == ["--database", "/x", "sync", "push"]


def test_a_new_command_name_is_left_alone():
    assert expand_aliases(["values", "--tier", "confirmed"]) == ["values", "--tier", "confirmed"]
    assert expand_aliases(["sync", "status"]) == ["sync", "status"]
    assert expand_aliases([]) == []


def test_values_lists_the_measurements(tmp_path, capsys):
    assert run(tmp_path, "values") == 0
    out = capsys.readouterr().out
    assert "ChatGPT Plus" in out
    assert "measurement(s)" in out


def test_values_filters_by_product(tmp_path, capsys):
    assert run(tmp_path, "values", "--product", "Claude") == 0
    assert "No measurements match" in capsys.readouterr().out


def test_values_filters_by_tier(tmp_path, capsys):
    assert run(tmp_path, "values", "--tier", "confirmed") == 0
    out = capsys.readouterr().out
    assert "provisional" not in out


def test_values_emits_json(tmp_path, capsys):
    assert run(tmp_path, "values", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and {"product", "window", "tier", "estimate_usd"} <= set(rows[0])


def test_status_reports_the_database_and_where_it_sends(tmp_path, capsys):
    assert run(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "quota readings" in out
    assert "not sending anywhere" in out


def test_push_without_configuration_explains_where_to_put_it(tmp_path, capsys):
    assert run(tmp_path, "sync", "push") == 2
    assert "no endpoint configured" in capsys.readouterr().err


def test_sync_reset_refuses_without_consent(tmp_path, capsys):
    assert run(tmp_path, "sync", "reset") == 2
    assert "--yes" in capsys.readouterr().err


def test_data_reset_refuses_without_consent(tmp_path, capsys):
    assert run(tmp_path, "data", "reset") == 2
    assert "cannot be undone" in capsys.readouterr().err


def test_data_reset_empties_the_database(tmp_path, capsys):
    path = _database(tmp_path)
    assert main(["--database", str(path), "data", "reset", "--yes"]) == 0
    db = connect(path)
    assert db.execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM usage_rows").fetchone()[0] == 0


def test_data_prune_refuses_without_consent(tmp_path, capsys):
    assert run(tmp_path, "data", "prune") == 2
    assert "--yes" in capsys.readouterr().err


def test_data_prune_keeps_recent_imports(tmp_path, capsys):
    path = _database(tmp_path)
    assert main(["--database", str(path), "data", "prune", "--days", "36500", "--yes"]) == 0
    assert connect(path).execute("SELECT COUNT(*) FROM usage_rows").fetchone()[0] == 3


def test_data_backup_writes_a_readable_copy(tmp_path):
    path = _database(tmp_path)
    destination = tmp_path / "copies" / "backup.sqlite3"
    assert main(["--database", str(path), "data", "backup", str(destination)]) == 0
    assert connect(destination).execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0] == 3


def test_data_path_reports_size_and_counts(tmp_path, capsys):
    assert run(tmp_path, "data", "path") == 0
    assert "quota readings" in capsys.readouterr().out
