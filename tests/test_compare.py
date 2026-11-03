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


def test_estimate_change_is_counted_apart_from_bound_changes(tmp_path, capsys):
    # A lower bound near $0 made the old check print "+15299888461538.5%" beside an estimate
    # that moved by 31%; and a window whose estimate held still was counted as changed.
    def window(reset, estimate, lower, upper):
        return {**_window(reset, estimate), "lower_usd": lower, "upper_usd": upper}

    live = tmp_path / "live.sqlite3"
    db = connect(live)
    save_report(db, VALUE_REPORT_KIND, {"windows": [
        window("2026-08-05T04:11:00+00:00", 112.70, 1e-12, 150.0),
        window("2026-09-28T19:00:00+00:00", 294.61, 1.0, 400.0),
    ]}, "2026-10-20T00:00:00+00:00")
    db.close()
    backup = tmp_path / "backup.sqlite3"
    shutil.copy(live, backup)
    db = connect(live)
    save_report(db, VALUE_REPORT_KIND, {"windows": [
        window("2026-08-05T04:11:00+00:00", 148.19, 0.15, 200.0),
        window("2026-09-28T19:00:00+00:00", 292.71, 4.5, 400.0),
    ]}, "2026-11-02T00:00:00+00:00")
    db.close()
    result = compare.compare(backup, live)
    assert [(old["reset_key"], round(drift, 3)) for old, _new, drift in result["changed"]] == [
        ("2026-08-05T04:11:00+00:00", 0.315)]
    assert [old["reset_key"] for old, _new in result["bounds_changed"]] == ["2026-09-28T19:00:00+00:00"]
    compare.run(backup, live, tolerance=0.01)
    out = capsys.readouterr().out
    assert "1 unchanged within 1%, 1 changed, 0 missing" in out
    assert "$112.70 -> $148.19 (+31.5%)" in out
    assert "1 more with the estimate unchanged and a bound changed" in out
    assert "lower $1.00 -> $4.50" in out
    assert "e+" not in out and "15299888461538" not in out
