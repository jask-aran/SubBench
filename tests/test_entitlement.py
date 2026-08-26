from subbench.entitlement import _normalise_claude, _normalise_codex


def test_normalise_codex_windows():
    rows = _normalise_codex({
        "rateLimits": {
            "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1780000000},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1780500000},
        }
    })
    assert [(row.window, row.used_percent) for row in rows] == [("five_hour", 25.0), ("weekly", 40.0)]


def test_normalise_codex_rounds_reset_time_to_the_minute():
    rows = _normalise_codex({
        "rateLimits": {
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1780500059},
        }
    })
    assert rows[0].resets_at.endswith(":00+00:00")


def test_normalise_codex_keeps_same_duration_levels_separate():
    rows = _normalise_codex({
        "rateLimits": {
            "primary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1780500000},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1780500000},
        }
    })
    # Separate series, but the first keeps the plain name. A provider enabling a second
    # level mid-window would otherwise rename the series already being measured and
    # orphan every observation recorded before the change.
    assert [row.window for row in rows] == ["weekly", "weekly_secondary"]


def test_normalise_claude_fractional_utilisation():
    rows = _normalise_claude({
        "five_hour": {"utilization": 0.12, "resets_at": "2026-07-27T10:00:00Z"},
        "seven_day": {"utilization": 0.34, "resets_at": "2026-08-01T10:00:00Z"},
    })
    assert [(row.window, row.used_percent) for row in rows] == [("five_hour", 12.0), ("weekly", 34.0)]


def test_a_declared_plan_fills_in_for_a_provider_that_reports_none(monkeypatch):
    """Claude's usage endpoint returns meters and nothing about the subscription."""
    from subbench import entitlement as module

    monkeypatch.setattr(module, "collect_claude", lambda: [
        module.EntitlementWindow(
            provider="claude", window="weekly", used_percent=4.0,
            resets_at=None, duration_minutes=10080, source="test", plan=None,
        )
    ])
    monkeypatch.setenv("SUBBENCH_PLAN_CLAUDE", "pro")
    assert module.collect_entitlements("claude")[0].plan == "pro"


def test_a_reported_plan_always_beats_a_declared_one(monkeypatch):
    """A declaration is asserted, not measured, so it never overrides the provider."""
    from subbench import entitlement as module

    monkeypatch.setattr(module, "collect_codex", lambda: [
        module.EntitlementWindow(
            provider="codex", window="weekly", used_percent=4.0,
            resets_at=None, duration_minutes=10080, source="test", plan="plus",
        )
    ])
    monkeypatch.setenv("SUBBENCH_PLAN_CODEX", "pro")
    assert module.collect_entitlements("codex")[0].plan == "plus"
