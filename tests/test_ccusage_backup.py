from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from subbench import doctor, watcher
from subbench.ccusage import compare_daily_tokens
from subbench.store import connect


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SUBBENCH_CCUSAGE", raising=False)
    monkeypatch.setattr(watcher.shutil, "which", lambda name: "/tools/ccusage")
    monkeypatch.setattr(watcher, "_CCUSAGE_CHECKS", {})
    monkeypatch.setattr(watcher, "collect_entitlements", lambda provider: [])
    monkeypatch.setattr(watcher.account, "active_account_id", lambda: None)
    monkeypatch.setattr(watcher, "reader", lambda provider: SimpleNamespace(hourly=lambda: {}, prices=None))
    monkeypatch.setattr(watcher.time, "monotonic", lambda: 0.0)

    def no_process(*args, **kwargs):
        pytest.fail("unexpected process")

    monkeypatch.setattr(watcher.subprocess, "run", no_process)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    payload = {"daily": [{"date": "2026-10-01", "inputTokens": 10, "outputTokens": 20,
                          "cacheCreationTokens": 30, "cacheReadTokens": 40, "totalCost": 1.0}]}
    monkeypatch.setattr(watcher, "native_report", lambda *args: (json.dumps(payload).encode(), payload))
    db = connect(Path(":memory:"))
    yield db, payload
    db.close()


def process(monkeypatch, payload=None, error=None, status=0):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        expected = {"check": False, "capture_output": True}
        if command[0] == "/tools/ccusage":
            expected["timeout"] = 120
        assert kwargs == expected
        if error:
            raise error
        return subprocess.CompletedProcess(command, status, json.dumps(payload).encode(), b"tool failed")

    monkeypatch.setattr(watcher.subprocess, "run", run)
    return calls


def collect(db, provider="claude", runner="native"):
    return watcher.collect_target(db, target=watcher.WatchTarget(provider), runner=runner)


def stored(db, provider="claude"):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (f"ccusage.check.{provider}",)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize("value", ["off", "OFF", "0", "False", "NO"])
def test_detection_off(monkeypatch, value):
    monkeypatch.setenv("SUBBENCH_CCUSAGE", value)
    assert watcher.ccusage_executable() is None


def test_detection_path(tmp_path, monkeypatch):
    path = tmp_path / "ccusage"
    monkeypatch.setenv("SUBBENCH_CCUSAGE", str(path))
    assert watcher.ccusage_executable() is None
    path.write_text("fixture")
    assert watcher.ccusage_executable() is None
    path.chmod(0o700)
    assert watcher.ccusage_executable() == str(path)
    monkeypatch.setenv("SUBBENCH_CCUSAGE", str(tmp_path))
    assert watcher.ccusage_executable() is None


def test_detection_which_and_absent(monkeypatch):
    names = []
    monkeypatch.setattr(watcher.shutil, "which", lambda name: names.append(name) or "/tools/ccusage")
    assert watcher.ccusage_executable() == "/tools/ccusage"
    assert names == ["ccusage"]
    monkeypatch.setattr(watcher.shutil, "which", lambda name: None)
    assert watcher.ccusage_executable() is None


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_agrees_without_extra_import(evidence, monkeypatch, provider):
    db, payload = evidence
    other = deepcopy(payload)
    other["daily"][0]["totalCost"] = 999
    calls = process(monkeypatch, other)
    ok, message = collect(db, provider)
    assert ok and "ccusage agrees (1 days)" in message
    assert calls == [["/tools/ccusage", provider, "daily", "--json", "--offline"]]
    imports = db.execute("SELECT command FROM imports").fetchall()
    assert [row[0] for row in imports] == [f"subbench {provider} logs"]
    check = stored(db, provider)
    assert check == {"at": check["at"], "version": None, "days": 1, "differing": [], "error": None}
    assert check["at"].endswith("+00:00")


@pytest.mark.parametrize("field", ["inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens"])
def test_differs(evidence, monkeypatch, field):
    db, payload = evidence
    other = deepcopy(payload)
    other["daily"][0][field] += 1
    process(monkeypatch, other)
    ok, message = collect(db)
    assert ok and "ccusage differs on 1 day(s): 2026-10-01" in message
    assert stored(db)["differing"] == ["2026-10-01"]


def test_comparison_counts_missing_days_and_ignores_costs():
    a = {"daily": [{"date": "2026-10-01", "inputTokens": 1}]}
    b = {"daily": [{"date": "2026-10-02", "inputTokens": 1}]}
    assert compare_daily_tokens(a, b) == (2, ["2026-10-01", "2026-10-02"])
    assert compare_daily_tokens({"daily": []}, {"daily": []}) == (0, [])
    with pytest.raises(ValueError):
        compare_daily_tokens({}, {})


@pytest.mark.parametrize("error,status,payload", [
    (None, 1, None), (OSError("missing tool"), 0, None),
    (subprocess.TimeoutExpired("ccusage", 120), 0, None),
    (RuntimeError("unexpected failure"), 0, None), (None, 0, {}),
])
def test_check_failure_keeps_entitlements(evidence, monkeypatch, error, status, payload):
    db, _ = evidence
    process(monkeypatch, payload, error, status)
    ok, message = collect(db)
    assert ok and "ccusage check failed:" in message and "entitlement 0 window(s)" in message
    assert stored(db)["error"]
    assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


def native_failure(*args):
    raise OSError("broken log")


def test_fallback_saves_import(evidence, monkeypatch):
    db, payload = evidence
    monkeypatch.setattr(watcher, "native_report", native_failure)
    process(monkeypatch, payload)
    ok, message = collect(db)
    assert ok and "usage from ccusage (built-in reader failed: broken log)" in message
    assert db.execute("SELECT command FROM imports").fetchone()[0] == "/tools/ccusage claude daily --json --offline"
    assert stored(db) is None


@pytest.mark.parametrize("available,error", [(False, None), (True, OSError("missing tool")),
                                               (True, subprocess.TimeoutExpired("ccusage", 120))])
def test_failed_fallback(evidence, monkeypatch, available, error):
    db, payload = evidence
    monkeypatch.setattr(watcher, "native_report", native_failure)
    monkeypatch.setattr(watcher.shutil, "which", lambda name: "/tools/ccusage" if available else None)
    process(monkeypatch, payload, error)
    ok, message = collect(db)
    assert not ok and "usage error: broken log" in message and "entitlement 0 window(s)" in message
    assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_hourly_throttle_per_provider(evidence, monkeypatch):
    db, payload = evidence
    calls = process(monkeypatch, payload)
    collect(db)
    monkeypatch.setattr(watcher.time, "monotonic", lambda: 3599.0)
    collect(db)
    collect(db, "codex")
    assert len(calls) == 2
    monkeypatch.setattr(watcher.time, "monotonic", lambda: 3600.0)
    collect(db)
    assert len(calls) == 3


def test_failed_check_is_throttled(evidence, monkeypatch):
    db, _ = evidence
    calls = process(monkeypatch, status=1)
    collect(db)
    collect(db)
    assert len(calls) == 1


@pytest.mark.parametrize("value,status,detail", [
    (None, "ok", "installed at /tools/ccusage; not checked yet"),
    ({"at": "now", "days": 2, "differing": [], "error": None}, "ok", "agrees on 2 days (checked now)"),
    ({"at": "now", "days": 2, "differing": ["2026-10-01"], "error": None}, "warn", "differs on 1 day(s): 2026-10-01"),
    ({"at": "now", "days": 0, "differing": [], "error": "tool failed"}, "warn", "tool failed"),
])
def test_doctor_states(evidence, monkeypatch, value, status, detail):
    db, _ = evidence
    if value:
        with db:
            db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("ccusage.check.claude", json.dumps(value)))
    assert doctor._ccusage_check(db, "claude") == doctor.Check("claude ccusage", status, detail)
    monkeypatch.setenv("SUBBENCH_CCUSAGE", "off")
    assert doctor._ccusage_check(db, "claude") == doctor.Check("claude ccusage", "ok", "not installed (optional backup)")


def test_doctor_order(evidence, tmp_path, monkeypatch):
    db, _ = evidence
    monkeypatch.setattr(doctor, "_login_check", lambda provider: doctor.Check(f"{provider} login", "ok", "present"))
    monkeypatch.setattr(doctor, "_codex_auth_check", lambda: doctor.Check("codex active account", "ok", "present"))
    monkeypatch.setattr(doctor, "discover_logs", lambda provider: [])
    names = [check.name for check in doctor.run_doctor(db, tmp_path / "db.sqlite3", ("claude", "codex"))]
    for provider in ("claude", "codex"):
        assert names.index(f"{provider} logs") < names.index(f"{provider} ccusage") < names.index(f"{provider} latest usage")


@pytest.mark.parametrize("value", ["off", None])
def test_no_backup_runs_without_executable(evidence, monkeypatch, value):
    db, _ = evidence
    monkeypatch.setattr(watcher.shutil, "which", lambda name: None)
    if value:
        monkeypatch.setenv("SUBBENCH_CCUSAGE", value)
    ok, message = collect(db)
    assert ok and "ccusage" not in message
    assert stored(db) is None


def test_differing_message_lists_only_three_dates(evidence, monkeypatch):
    db, _ = evidence
    dates = [f"2026-10-0{index}" for index in range(1, 6)]
    process(monkeypatch, {"daily": [{"date": date, "inputTokens": 99} for date in dates]})
    ok, message = collect(db)
    assert ok and "ccusage differs on 5 day(s): " + ", ".join(dates[:3]) in message
    assert dates[3] not in message
    assert stored(db)["days"] == 5 and stored(db)["differing"] == dates


def test_invalid_json_is_a_check_failure(evidence, monkeypatch):
    db, _ = evidence
    monkeypatch.setattr(watcher.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args, 0, b"invalid", b""))
    ok, message = collect(db)
    assert ok and "ccusage check failed:" in message and "entitlement 0 window(s)" in message
    assert stored(db)["error"].startswith("Expecting value")


def test_check_database_failure_keeps_entitlements(evidence, monkeypatch):
    db, payload = evidence
    process(monkeypatch, payload)
    db.execute("CREATE TEMP TRIGGER reject_check BEFORE INSERT ON meta "
               "BEGIN SELECT RAISE(FAIL, 'check write failed'); END")
    ok, message = collect(db)
    assert ok and "ccusage check failed: check write failed" in message
    assert "entitlement 0 window(s)" in message


@pytest.mark.parametrize("timeout", [False, True])
def test_fallback_retries_dropped_periods(evidence, monkeypatch, timeout):
    db, payload = evidence
    process(monkeypatch, payload)
    collect(db)
    monkeypatch.setattr(watcher, "native_report", native_failure)
    responses = iter([payload, payload])
    checks = iter([{"2026-09-30"}, set()])
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if timeout and len(calls) == 2:
            raise subprocess.TimeoutExpired(command, 120)
        return subprocess.CompletedProcess(command, 0, json.dumps(next(responses)).encode(), b"")

    monkeypatch.setattr(watcher.subprocess, "run", run)
    monkeypatch.setattr(watcher, "dropped_periods", lambda *args, **kwargs: next(checks))
    before = db.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    ok, message = collect(db)
    assert ok is not timeout
    assert len(calls) == 2 and calls[0] == calls[1]
    assert "retrying, ccusage dropped 1 period(s)" in message
    if timeout:
        assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == before


@pytest.mark.parametrize("runner,prefix", [
    ("npx", ["npx", "--yes", "ccusage@latest"]),
    ("bunx", ["bunx", "ccusage"]), ("pnpm", ["pnpm", "dlx", "ccusage"]),
])
def test_explicit_runners_keep_commands(evidence, monkeypatch, runner, prefix):
    db, payload = evidence
    calls = process(monkeypatch, payload)
    ok, _ = collect(db, runner=runner)
    assert ok and calls == [[*prefix, "claude", "daily", "--json"]]
    assert stored(db) is None


@pytest.mark.parametrize("raw", ["{", "null", "{}", '{"error":null,"differing":"bad","days":1,"at":"now"}'])
def test_doctor_reports_invalid_check(evidence, raw):
    db, _ = evidence
    with db:
        db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("ccusage.check.claude", raw))
    assert doctor._ccusage_check(db, "claude") == doctor.Check("claude ccusage", "warn", "invalid stored ccusage check")


def test_doctor_without_meta_table():
    import sqlite3

    db = sqlite3.connect(":memory:")
    assert doctor._ccusage_check(db, "claude") == doctor.Check("claude ccusage", "ok", "installed at /tools/ccusage; not checked yet")
    db.close()


def test_native_processing_failure_does_not_run_backup(evidence, monkeypatch):
    db, _ = evidence
    monkeypatch.setattr(watcher, "save_hourly", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("hourly failed")))
    ok, message = collect(db)
    assert not ok and "usage error: hourly failed" in message
    assert "usage from ccusage" not in message
    assert db.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


@pytest.mark.parametrize("runner", ["npx", "bunx", "pnpm"])
def test_explicit_runner_failure_keeps_original_message(evidence, monkeypatch, runner):
    db, _ = evidence
    def run(command, **kwargs):
        assert kwargs == {"check": False, "capture_output": True}
        return subprocess.CompletedProcess(command, 1, b"", b"original error\n")
    monkeypatch.setattr(watcher.subprocess, "run", run)
    ok, message = collect(db, runner=runner)
    assert not ok and message == "claude: original error; entitlement 0 window(s)"
