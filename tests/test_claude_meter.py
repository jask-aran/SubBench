"""The Claude quota meter: a 429 backs off, reads are spaced, the profile is cached."""
from __future__ import annotations

import json
import time

import pytest

from subbench import endpoints, entitlement


@pytest.fixture
def login(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SUBBENCH_CLAUDE_USAGE_COMMAND", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "tok", "expiresAt": (time.time() + 3600) * 1000}}))
    clock = {"now": 1000.0}
    monkeypatch.setattr(entitlement.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(endpoints.time, "monotonic", lambda: clock["now"])
    return clock


def _server(monkeypatch, statuses):
    calls = []

    def fake_get(url, headers, **kw):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/profile"):
            return {"account": {"uuid": "u"}, "organization": {"rate_limit_tier": "default_claude_pro"}}
        status = statuses.pop(0)
        if status != 200:
            raise endpoints.EndpointError(f"{url} returned {status}: limited", status=status)
        return {"five_hour": {"utilization": 10, "resets_at": "2026-11-02T15:00:00Z"}}

    monkeypatch.setattr(endpoints, "_get", fake_get)
    return calls


def test_a_429_backs_off_and_doubles(login, monkeypatch):
    calls = _server(monkeypatch, [429, 429, 200])
    with pytest.raises(RuntimeError, match="429"):
        entitlement.collect_claude()
    with pytest.raises(entitlement.MeterDeferred):
        entitlement.collect_claude()          # inside the first backoff: no request
    assert calls.count("usage") == 1
    login["now"] += entitlement.CLAUDE_BACKOFF_FIRST_SECONDS
    with pytest.raises(RuntimeError, match="429"):
        entitlement.collect_claude()
    login["now"] += entitlement.CLAUDE_BACKOFF_FIRST_SECONDS
    with pytest.raises(entitlement.MeterDeferred):
        entitlement.collect_claude()          # the second backoff is twice as long
    login["now"] += entitlement.CLAUDE_BACKOFF_FIRST_SECONDS
    assert entitlement.collect_claude()[0].used_percent == 10
    assert calls.count("usage") == 3


def test_reads_are_spaced_and_the_profile_is_cached(login, monkeypatch):
    calls = _server(monkeypatch, [200, 200])
    entitlement.collect_claude()
    with pytest.raises(entitlement.MeterDeferred):
        entitlement.collect_claude()
    login["now"] += entitlement.CLAUDE_METER_MIN_SECONDS
    entitlement.collect_claude()
    assert calls == ["usage", "profile", "usage"]


def test_a_deferred_read_is_reported_but_not_as_unavailable(login, monkeypatch, tmp_path):
    from subbench import watcher
    from subbench.store import connect

    _server(monkeypatch, [200])
    monkeypatch.setattr(watcher, "native_report", lambda provider, report: (b"{}", {"daily": []}))
    monkeypatch.setattr(watcher, "save_hourly", lambda *a, **k: 0)
    monkeypatch.setattr(watcher, "reader", lambda provider: type("R", (), {"hourly": lambda self: [], "prices": None})())
    db = connect(tmp_path / "db.sqlite3")
    watcher.collect_target(db, target=watcher.WatchTarget("claude"), runner="native")
    ok, message = watcher.collect_target(db, target=watcher.WatchTarget("claude"), runner="native")
    assert ok and "entitlement deferred" in message and "unavailable" not in message


def test_a_deferred_read_is_retried_when_the_wait_ends(monkeypatch):
    # Without a retry, a deferred reading waited for the next log change, which can be in
    # the next window when the agent goes idle; the end of a burst of use was lost.
    from subbench import watcher

    clock = {"now": 5000.0}
    monkeypatch.setattr(watcher.time, "monotonic", lambda: clock["now"])
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds
        if len(sleeps) >= 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(watcher.time, "sleep", fake_sleep)
    monkeypatch.setattr(watcher, "refresh_if_due", lambda db, **kw: kw["now"])
    monkeypatch.setattr(watcher.LogChangeDetector, "scan", lambda self: False)
    monkeypatch.setattr(watcher.AuthFileDetector, "scan", lambda self: False)
    collected = []

    def collect(db, *, target, runner):
        collected.append(clock["now"])
        if len(collected) == 1:
            entitlement._CLAUDE_METER.update(next=clock["now"] + 3, deferred=1.0)
        else:
            entitlement._CLAUDE_METER.update(deferred=0.0)
        return True, "claude: ok"

    monkeypatch.setattr(watcher, "collect_target", collect)
    watcher.watch(object(), targets=[watcher.WatchTarget("claude")], runner="native", interval_seconds=2,
                  reconcile_seconds=4000, emit=lambda message: None, debounce_seconds=60)
    # The first pass collects (reconcile due at start), is deferred for 3 s, and the loop
    # collects again once those 3 s have passed, with no log change.
    assert len(collected) == 2 and collected[1] - collected[0] >= 3


def _claude_code_cache(home, *, fetched, account="u", percent=40):
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": "u", "organizationRateLimitTier": "default_claude_max_5x"},
        "cachedUsageUtilization": {"fetchedAtMs": fetched * 1000, "accountUuid": account, "utilization": {
            "seven_day": {"utilization": percent, "resets_at": "2026-11-16T19:00:00Z"},
            "limits": [{"kind": "weekly_scoped", "percent": 80, "resets_at": "2026-11-16T19:00:00Z",
                        "scope": {"model": {"display_name": "Fable"}}}],
            "weekly_scoped_shares": [{"limit_index": 0, "allowance_percent_of_weekly": 50}]}}}))


def test_a_429_falls_back_to_claude_codes_cached_meter(login, monkeypatch, tmp_path):
    _server(monkeypatch, [429])
    fetched = time.time() - 120
    _claude_code_cache(tmp_path, fetched=fetched)
    rows = {row.window: row for row in entitlement.collect_claude()}
    assert set(rows) == {"weekly", "weekly_fable"}
    assert rows["weekly"].used_percent == 40 and rows["weekly"].plan == "max_5x"
    assert rows["weekly"].source == "claude-code-cache"
    # The reading keeps the moment Claude Code fetched it, not the moment SubBench read the file.
    assert rows["weekly"].observed_at.startswith(time.strftime("%Y-%m-%dT%H:%M", time.gmtime(fetched)))
    assert rows["weekly_fable"].allowance_percent == 50
    # The same copy is not taken twice, and the backoff still stands.
    with pytest.raises(entitlement.MeterDeferred):
        entitlement.collect_claude()


def test_claude_codes_cache_is_skipped_when_stale_or_for_another_account(login, monkeypatch, tmp_path):
    _claude_code_cache(tmp_path, fetched=time.time() - entitlement.CLAUDE_CODE_CACHE_MAX_AGE_SECONDS - 60)
    assert entitlement.claude_code_cached() == []
    _claude_code_cache(tmp_path, fetched=time.time() - 60, account="someone-else")
    assert entitlement.claude_code_cached() == []
