"""Codex has enabled and disabled its five-hour window before and may again.

These lock in that the change is absorbed without operator action and without renaming a
series mid-flight, since a rename orphans every observation recorded before it.
"""
from subbench.crosssolve import combined_estimates
from subbench.entitlement import _normalise_codex
from subbench.regression import robust_estimates


def limits(primary=None, secondary=None, plan="plus"):
    return {"rateLimits": {"planType": plan, "primary": primary, "secondary": secondary}}


def window(used, minutes, resets=1786027289):
    return {"usedPercent": used, "windowDurationMins": minutes, "resetsAt": resets}


def labels(payload):
    return [row.window for row in _normalise_codex(payload, account_id="A")]


def test_weekly_only_today():
    assert labels(limits(primary=window(40, 10080))) == ["weekly"]


def test_five_hour_returning_is_labelled_by_duration():
    assert sorted(labels(limits(window(40, 10080), window(73, 300)))) == ["five_hour", "weekly"]


def test_labels_do_not_depend_on_which_level_reports_which():
    # Nothing guarantees the five-hour window comes back as `secondary`.
    assert sorted(labels(limits(window(73, 300), window(40, 10080)))) == ["five_hour", "weekly"]


def test_a_duplicate_weekly_does_not_rename_the_existing_series():
    # The transition that would otherwise orphan history: if a second level appears also
    # reporting a weekly duration, the established series must keep its name and only the
    # newcomer is suffixed.
    assert labels(limits(window(40, 10080), window(40, 10080))) == ["weekly", "weekly_secondary"]


def test_plan_comes_from_the_meter():
    rows = _normalise_codex(limits(window(40, 10080), plan="pro"), account_id="A")
    assert all(row.plan == "pro" for row in rows)


def test_cross_solving_engages_as_soon_as_both_windows_move():
    # No configuration, no migration: the ratio is measured from whatever arrives.
    points = []
    for hour in range(13):
        stamp = f"2026-08-10T{hour:02d}:00:00+00:00"
        base = {"provider": "codex", "account_id": "A", "observed_at": stamp,
                "cost_usd": hour * 2.0, "resets_at": "2026-08-17T00:00:00+00:00"}
        points.append({**base, "window": "weekly", "used_percent": hour * 1.0, "duration_minutes": 10080})
        points.append({**base, "window": "five_hour", "used_percent": hour * 7.0, "duration_minutes": 300})

    direct = robust_estimates(points)
    combined, ratios = combined_estimates(points, direct)
    assert len(ratios) == 1
    assert abs(ratios[0].ratio - 12.0 / 84.0) < 0.01

    weekly_direct = next(e for e in direct if e.window == "weekly")
    converted = [e for e in combined if e.window == "weekly" and "~via~" in e.reset_key]
    assert len(converted) == 1
    # The five-hour window, converted, must reproduce the weekly measurement.
    assert abs(converted[0].estimate_usd - weekly_direct.estimate_usd) < 1.0
