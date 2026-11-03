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
