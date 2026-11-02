"""`subbench data check-estimates`: the estimates in a pre-upgrade backup against the live ones."""
from __future__ import annotations

import shutil

from subbench import compare
from subbench.push import VALUE_REPORT_KIND
from subbench.store import connect, save_report


def _window(reset, estimate, window="weekly"):
    return {"provider": "claude", "window": window, "account_id": None, "plan": "pro", "reset_key": reset,
            "estimate_usd": estimate, "lower_usd": estimate * 0.9, "upper_usd": estimate * 1.1}


def test_closed_windows_are_compared_and_open_ones_skipped(tmp_path, capsys):
    live = tmp_path / "live.sqlite3"
    db = connect(live)
    save_report(db, VALUE_REPORT_KIND, {"windows": [
        _window("2026-10-01T00:00:00+00:00", 100.0),
        _window("2026-10-08T00:00:00+00:00~via~five_hour", 50.0),
        _window("2026-10-15T00:00:00+00:00", 70.0),
        _window("2026-11-05T00:00:00+00:00", 10.0),  # open when the backup was taken
    ]}, "2026-10-20T00:00:00+00:00")
    db.close()
    backup = tmp_path / "backup.sqlite3"
    shutil.copy(live, backup)
    db = connect(live)
    save_report(db, VALUE_REPORT_KIND, {"windows": [
        _window("2026-10-01T00:00:00+00:00", 100.0),
        _window("2026-10-08T00:00:00+00:00~via~five_hour", 51.5),
        _window("2026-11-05T00:00:00+00:00", 30.0),
    ]}, "2026-11-02T00:00:00+00:00")
    db.close()
    result = compare.compare(backup, live)
    assert result["closed"] == 3 and result["open"] == 1
    assert [row["reset_key"] for row in result["missing"]] == ["2026-10-15T00:00:00+00:00"]
    assert [old["estimate_usd"] for old, _new, _drift in result["changed"]] == [50.0]
    assert compare.run(backup, live, tolerance=0.01) == 1  # a closed window went missing
    assert "missing  claude weekly pro reset 2026-10-15" in capsys.readouterr().out
