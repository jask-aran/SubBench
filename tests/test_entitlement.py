from subbench.entitlement import _normalise_claude, _normalise_codex


def test_normalise_codex_windows():
    rows = _normalise_codex({
        "rateLimits": {
            "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1780000000},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1780500000},
        }
    })
    assert [(row.window, row.used_percent) for row in rows] == [("five_hour", 25.0), ("weekly", 40.0)]


def test_normalise_claude_fractional_utilisation():
    rows = _normalise_claude({
        "five_hour": {"utilization": 0.12, "resets_at": "2026-07-27T10:00:00Z"},
        "seven_day": {"utilization": 0.34, "resets_at": "2026-08-01T10:00:00Z"},
    })
    assert [(row.window, row.used_percent) for row in rows] == [("five_hour", 12.0), ("weekly", 34.0)]
