"""The built-in collector: log reading, pricing, provider endpoints, the service unit.

The expected costs are what ccusage printed for the same logs (checked by running both on
these fixtures), so a database that moves from ccusage to the built-in reader keeps one
continuous spend series.
"""
from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import time
from pathlib import Path

import pytest

from subbench import config, endpoints, logs, service
from subbench.ccusage import normalise_payload
from subbench.entitlement import _normalise_claude, _normalise_codex
from subbench.prices import Prices, Tokens, _bundled
from subbench.store import connect


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("SUBBENCH_OFFLINE", "1")
    time.tzset()
    yield
    time.tzset()


def _line(record) -> str:
    return json.dumps(record, separators=(",", ":"))


def _assistant(mid, rid, model, ts, inp, write, read, out, one_hour=0):
    return {"type": "assistant", "timestamp": ts, "requestId": rid, "sessionId": "s",
            "message": {"id": mid, "model": model, "usage": {
                "input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read,
                "output_tokens": out, "cache_creation": {"ephemeral_1h_input_tokens": one_hour,
                                                        "ephemeral_5m_input_tokens": write - one_hour}}}}


def _token_count(ts, total, last):
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": total, "last_token_usage": last, "model_context_window": 272000}}}


def _usage(i, c, o, r):
    return {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o, "reasoning_output_tokens": r, "total_tokens": i + o}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    project = tmp_path / ".claude" / "projects" / "p"
    project.mkdir(parents=True)
    first = [
        _assistant("m1", "r1", "claude-haiku-5-5", "2026-10-01T10:00:00Z", 150000, 20000, 300000, 120000),
        _assistant("m2", "r2", "claude-sonnet-5-5", "2026-10-01T11:00:00Z", 10, 5000, 90000, 800, one_hour=2000),
        _assistant("m3", "r3", "claude-opus-4-5-20251101", "2026-10-02T11:00:00Z", 10, 5000, 90000, 800),
        _assistant("m4", "r4", "claude-mystery-9", "2026-10-02T12:00:00Z", 10, 5000, 90000, 800),
        {"type": "user", "timestamp": "2026-10-02T12:00:00Z", "message": {"role": "user", "content": "hi"}},
        _assistant("m6", "r6", "<synthetic>", "2026-10-02T12:00:00Z", 1, 0, 0, 1),
    ]
    # The second file repeats m2, as a resumed session does.
    second = [first[1], _assistant("m5", "r5", "claude-opus-5-5", "2026-10-02T09:00:00Z", 3, 100, 1000, 50)]
    (project / "a.jsonl").write_text("\n".join(map(_line, first)) + "\n")
    (project / "b.jsonl").write_text("\n".join(map(_line, second)) + "\n")
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "10" / "01"
    sessions.mkdir(parents=True)
    rollout = [
        {"timestamp": "2026-10-01T09:00:00Z", "type": "session_meta", "payload": {"id": "x", "cwd": "/w"}},
        {"timestamp": "2026-10-01T09:00:01Z", "type": "turn_context", "payload": {"cwd": "/w", "model": "gpt-6-sol"}},
        _token_count("2026-10-01T09:01:00Z", _usage(10000, 4000, 500, 200), _usage(10000, 4000, 500, 200)),
        # Codex repeats an event with the same total beside a rate-limit update.
        _token_count("2026-10-01T09:01:01Z", _usage(10000, 4000, 500, 200), _usage(10000, 4000, 500, 200)),
        _token_count("2026-10-01T09:02:00Z", _usage(30000, 20000, 900, 300), _usage(20000, 16000, 400, 100)),
        {"timestamp": "2026-10-01T09:03:00Z", "type": "turn_context", "payload": {"cwd": "/w", "model": "gpt-6-luna"}},
        _token_count("2026-10-01T09:04:00Z", _usage(50000, 30000, 1900, 300), _usage(20000, 10000, 1000, 0)),
    ]
    (sessions / "rollout-x.jsonl").write_text("\n".join(map(_line, rollout)) + "\n")
    return tmp_path


def _by_day(report):
    return {row["date"]: row for row in report["daily"]}


def test_claude_costs_match_ccusage(home):
    report = logs.LogReader("claude", Prices(_bundled())).daily()
    days = _by_day(report)
    # Haiku's context is past 100k tokens, so every class is billed at the long-context rate.
    assert days["2026-10-01"]["totalCost"] == pytest.approx(0.43502)
    models = {m["modelName"]: m for m in days["2026-10-01"]["modelBreakdowns"]}
    assert models["claude-haiku-5-5"]["cost"] == pytest.approx(0.4025)
    # One-hour cache writes at the one-hour rate, and m2 counted once though logged twice.
    assert models["claude-sonnet-5-5"]["cost"] == pytest.approx(0.03252)
    assert days["2026-10-02"]["totalCost"] == pytest.approx(0.098012)
    assert "<synthetic>" not in days["2026-10-02"]["modelsUsed"]


def test_unknown_model_is_free_and_named(home):
    reader = logs.LogReader("claude", Prices(_bundled()))
    reader.daily()
    assert reader.prices.unknown == {"claude-mystery-9"}


def test_codex_counts_changes_in_the_running_total(home):
    report = logs.LogReader("codex", Prices(_bundled())).daily()
    day = _by_day(report)["2026-10-01"]
    assert day["totalCost"] == pytest.approx(0.0346)
    models = {m["modelName"]: m for m in day["modelBreakdowns"]}
    # Cached tokens are split out of input; reasoning stays inside output.
    assert models["gpt-6-sol"] == {"modelName": "gpt-6-sol", "inputTokens": 10000, "cacheReadTokens": 20000,
                                   "outputTokens": 900, "reasoningOutputTokens": 300, "cacheCreationTokens": 0,
                                   "cost": pytest.approx(0.033), "totalTokens": 30900, "isFallback": False}
    assert models["gpt-6-luna"]["inputTokens"] == 10000 and models["gpt-6-luna"]["cacheReadTokens"] == 10000


def test_report_normalises_like_ccusage(home):
    report = logs.LogReader("claude", Prices(_bundled())).daily()
    rows = normalise_payload(report, provider="claude", report="daily")
    aggregate = [r for r in rows if r.model is None]
    assert {r.period_start for r in aggregate} == {"2026-10-01", "2026-10-02"}
    assert sum(float(r.reported_cost_usd) for r in aggregate) == pytest.approx(0.43502 + 0.098012)
    # Per-model rows carry tokens, the day row carries the cost: what ccusage's shape produced.
    assert all(r.reported_cost_usd is None for r in rows if r.model)
    assert logs.raw(report) == logs.raw(json.loads(logs.raw(report)))


def test_reading_is_incremental(home):
    reader = logs.LogReader("codex", Prices(_bundled()))
    before = reader.daily()["totals"]["totalCost"]
    rollout = next((home / ".codex" / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as handle:
        handle.write(_line(_token_count("2026-10-01T09:05:00Z", _usage(60000, 30000, 2900, 300), _usage(10000, 0, 1000, 0))) + "\n")
        handle.write('{"timestamp": "2026-10-01T09:06:00Z", "type": "event_')  # half-written line
    after = reader.daily()["totals"]["totalCost"]
    # 10k uncached input and 1k output on gpt-6-luna.
    assert after - before == pytest.approx(10000 * 1e-7 + 1000 * 5e-7)
    state = next(iter(reader._codex._files.values()))
    assert state.offset < rollout.stat().st_size  # the partial line is left for the next pass


def test_replaced_file_is_read_again(home):
    reader = logs.LogReader("claude", Prices(_bundled()))
    reader.daily()
    target = home / ".claude" / "projects" / "p" / "b.jsonl"
    target.write_text(_line(_assistant("m9", "r9", "claude-opus-5-5", "2026-10-03T09:00:00Z", 1, 0, 0, 1)) + "\n")
    days = _by_day(reader.daily())
    assert "2026-10-03" in days
    assert "claude-opus-5-5" not in days["2026-10-02"]["modelsUsed"]


def test_price_lookup_variants():
    prices = Prices(_bundled())
    assert prices.lookup("claude-opus-4-5-20991231") is prices.lookup("claude-opus-4-5")
    assert prices.lookup("claude-sonnet-5-5[1m]") is prices.lookup("claude-sonnet-5-5")
    assert prices.lookup("gpt-6-sol") is not None
    assert prices.cost("claude-opus-5-5", Tokens(input=1_000_000)) == pytest.approx(4.0)


# ---------------------------------------------------------------- endpoints

def test_wham_response_becomes_the_app_server_shape():
    payload = {"plan_type": "plus", "rate_limit": {
        "primary_window": {"used_percent": 12.5, "limit_window_seconds": 18000, "reset_at": 1_790_000_000},
        "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800, "reset_after_seconds": 3600},
    }}
    protocol = endpoints.wham_to_protocol(payload, now=1_789_000_000)
    rows = {row.window: row for row in _normalise_codex(protocol, account_id="acct")}
    assert rows["five_hour"].used_percent == 12.5 and rows["five_hour"].plan == "plus"
    assert rows["weekly"].used_percent == 40 and rows["weekly"].duration_minutes == 10080
    assert rows["weekly"].resets_at.startswith("2026-09-10")  # now + 3600 s, to the minute


def test_wham_without_windows_is_an_error():
    with pytest.raises(endpoints.EndpointError):
        endpoints.wham_to_protocol({"plan_type": "plus", "rate_limit": {}})


def test_claude_usage_attaches_the_live_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "tok", "expiresAt": (time.time() + 3600) * 1000, "subscriptionType": "pro"}}))
    seen = []

    def fake_get(url, headers, **kw):
        seen.append((url, headers["Authorization"]))
        if url.endswith("/profile"):
            return {"organization": {"rate_limit_tier": "default_claude_max_5x"}}
        return {"five_hour": {"utilization": 31, "resets_at": "2026-11-02T15:00:12Z"},
                "seven_day": {"utilization": 0.5, "resets_at": "2026-11-05T03:00:00Z"}}

    monkeypatch.setattr(endpoints, "_get", fake_get)
    rows = {row.window: row for row in _normalise_claude(endpoints.claude_usage())}
    assert rows["five_hour"].plan == "max_5x" and rows["five_hour"].used_percent == 31
    assert rows["five_hour"].resets_at == "2026-11-02T15:00:00+00:00"
    assert rows["weekly"].used_percent == 50
    assert all(auth == "Bearer tok" for _url, auth in seen)


def test_claude_readings_carry_the_account_uuid(tmp_path, monkeypatch):
    # Without an account id, the server cannot tell two devices on one Claude account from
    # two different users. The profile names the user; ~/.claude.json is the fallback.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "tok", "expiresAt": (time.time() + 3600) * 1000}}))
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid": "file-uuid", "organizationRateLimitTier": "default_claude_pro"}}))
    usage = {"five_hour": {"utilization": 10, "resets_at": "2026-11-02T15:00:00Z"}}
    profile = {"account": {"uuid": "profile-uuid"}, "organization": {"uuid": "org-uuid", "rate_limit_tier": "default_claude_pro"}}

    monkeypatch.setattr(endpoints, "_get", lambda url, headers, **kw: dict(profile) if url.endswith("/profile") else dict(usage))
    # The local id wins: usage imports carry the same one, and pricing joins on it.
    assert {row.account_id for row in _normalise_claude(endpoints.claude_usage())} == {"file-uuid"}
    (tmp_path / ".claude.json").unlink()
    assert {row.account_id for row in _normalise_claude(endpoints.claude_usage())} == {"profile-uuid"}


def test_expired_claude_login_is_reported_not_refreshed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000}}))
    monkeypatch.setattr(endpoints, "_get", lambda *a: pytest.fail("no request with an expired login"))
    with pytest.raises(endpoints.EndpointError, match="expired"):
        endpoints.claude_usage()


def test_codex_falls_back_to_app_server(tmp_path, monkeypatch):
    from subbench import entitlement

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "a.e30.b", "account_id": "acct"}}))

    def refuse(url, headers, **kw):
        raise endpoints.EndpointError("401")

    monkeypatch.setattr(endpoints, "_get", refuse)
    monkeypatch.setattr(endpoints, "codex_app_server", lambda: {"rateLimits": {"planType": "pro", "primary": {
        "usedPercent": 5, "windowDurationMins": 300, "resetsAt": 1_790_000_000}}})
    rows = entitlement.collect_codex_for("acct")
    assert rows[0].window == "five_hour" and rows[0].plan == "pro"


# ---------------------------------------------------------------- collector, service, config

def test_watch_once_collects_natively(home, monkeypatch, tmp_path):
    from subbench import watcher

    watcher._READERS.clear()
    monkeypatch.setattr(watcher, "collect_entitlements", lambda provider: [])
    db = connect(tmp_path / "db.sqlite3")
    ok, message = watcher.collect_target(db, target=watcher.WatchTarget("claude"), runner="native")
    assert ok and "usage recorded" in message and "no price for claude-mystery-9" in message
    ok, message = watcher.collect_target(db, target=watcher.WatchTarget("claude"), runner="native")
    assert "usage unchanged" in message
    watcher._READERS.clear()


def test_claude_imports_carry_the_same_account_as_the_readings(home, monkeypatch, tmp_path):
    from subbench import account, watcher

    watcher._READERS.clear()
    monkeypatch.setattr(account, "active_claude_account_id", lambda: "claude-uuid")
    monkeypatch.setattr(watcher, "collect_entitlements", lambda provider: [])
    db = connect(tmp_path / "db.sqlite3")
    watcher.collect_target(db, target=watcher.WatchTarget("claude"), runner="native")
    assert {row[0] for row in db.execute("SELECT account_id FROM imports")} == {"claude-uuid"}
    assert {row[0] for row in db.execute("SELECT account_id FROM usage_hourly")} == {"claude-uuid"}
    watcher._READERS.clear()


def test_service_units_run_this_executable(monkeypatch):
    monkeypatch.setattr(service, "executable", lambda: ["/opt/subbench"])
    unit = service.systemd_unit(["/opt/subbench", "watch", "--provider", "all"])
    assert "ExecStart=/opt/subbench watch --provider all" in unit
    assert f"EnvironmentFile=-{config.config_path()}" in unit
    plist = plistlib.loads(service.launchd_plist(["/opt/subbench", "watch", "--provider", "all"]))
    assert plist["ProgramArguments"] == ["/opt/subbench", "watch", "--provider", "all"] and plist["RunAtLoad"]


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_service_install_replaces_a_symlink_and_keeps_its_target(tmp_path, monkeypatch, system):
    # A unit or plist installed by hand is often a symlink into the checkout. Writing through
    # the link would overwrite the tracked file in the repository.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(service.platform, "system", lambda: system)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service, "executable", lambda: ["/opt/subbench"])
    monkeypatch.setattr(service, "_run", lambda *a: subprocess.CompletedProcess(a, 0, "", ""))
    target = tmp_path / "checkout" / "subbench.service"
    target.parent.mkdir()
    target.write_text("tracked file\n")
    path = service.unit_path() if system == "Linux" else service.plist_path()
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    assert service.install("all") == 0
    assert not path.is_symlink()
    assert b"/opt/subbench" in path.read_bytes()
    assert target.read_text() == "tracked file\n"


def test_config_write_keeps_other_lines_and_is_private(tmp_path):
    path = tmp_path / "push.env"
    path.write_text("# comment\nSUBBENCH_PUSH_URL=https://a\nOTHER=1\n")
    config.write(path, "SUBBENCH_PUSH_URL", "https://b")
    config.write(path, "OTHER", None)
    assert path.read_text() == "# comment\nSUBBENCH_PUSH_URL=https://b\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(ValueError):
        config.write(path, "BAD KEY", "x")


# ---------------------------------------------------------------- hourly

def test_hourly_buckets_add_up_to_the_daily_report(home):
    reader = logs.LogReader("claude", Prices(_bundled()))
    hours = reader.hourly()
    daily = reader.daily()
    assert sum(b["cost_usd"] for b in hours.values()) == pytest.approx(daily["totals"]["totalCost"])
    assert ("2026-10-01T10:00:00+00:00", "claude-haiku-5-5") in hours
    # m2 was logged twice and is one request.
    assert hours[("2026-10-01T11:00:00+00:00", "claude-sonnet-5-5")]["requests"] == 1


def test_hours_are_utc_whatever_the_local_zone(home, monkeypatch):
    monkeypatch.setenv("TZ", "Australia/Sydney")
    time.tzset()
    reader = logs.LogReader("codex", Prices(_bundled()))
    assert {hour for hour, _model in reader.hourly()} == {"2026-10-01T09:00:00+00:00"}
    assert [row["date"] for row in reader.daily()["daily"]] == ["2026-10-01"]  # 19:00 in Sydney


def test_a_closed_hour_keeps_the_account_that_spent_it(home, tmp_path):
    from subbench.store import hourly, save_hourly

    db = connect(tmp_path / "db.sqlite3")
    reader = logs.LogReader("codex", Prices(_bundled()))
    assert save_hourly(db, provider="codex", account_id="acct-A", buckets=reader.hourly(), now="t1") == 2
    # Another account becomes active; nothing old changed, so nothing is rewritten.
    assert save_hourly(db, provider="codex", account_id="acct-B", buckets=reader.hourly(), now="t2") == 0
    rollout = next((home / ".codex" / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as handle:
        handle.write(_line(_token_count("2026-10-01T10:05:00Z", _usage(60000, 30000, 2900, 300), _usage(10000, 0, 1000, 0))) + "\n")
    assert save_hourly(db, provider="codex", account_id="acct-B", buckets=reader.hourly(), now="t3") == 1
    accounts = {(row["hour"][11:13], row["account_id"]) for row in hourly(db)}
    assert accounts == {("09", "acct-A"), ("10", "acct-B")}


def test_an_unwritable_database_is_a_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    import sqlite3

    from subbench import cli

    def refuse(path):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(cli, "connect", refuse)
    assert cli.main(["--database", str(tmp_path / "db.sqlite3"), "doctor"]) == 2
    assert "needs write access" in capsys.readouterr().err
