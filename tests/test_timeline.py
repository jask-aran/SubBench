from datetime import datetime, timedelta, timezone

import pytest

from subbench import crosssolve, regression
from subbench.timeline import (
    TARGET_WINDOWS,
    UNVERSIONED_CONSTANTS,
    VERSIONED_CONSTANTS,
    build_series,
    estimator_version,
    replay_series,
    settled_timeline,
)

START = datetime(2026, 7, 20, tzinfo=timezone.utc)


def point(minutes, used, cost, *, window="weekly", resets_at="2026-07-27T00:00:00+00:00",
          provider="codex", account_id="a"):
    return {
        "provider": provider,
        "account_id": account_id,
        "window": window,
        "resets_at": resets_at,
        "observed_at": (START + timedelta(minutes=minutes)).isoformat(),
        "used_percent": float(used),
        "cost_usd": float(cost),
        "duration_minutes": 300 if window == "five_hour" else 10080,
        "cost_age_minutes": 0.0,
    }


def ramp(count, *, per_point_quota=4.0, usd_per_quota=2.0, **kwargs):
    """A window consumed at a constant value per quota point."""
    return [
        point(index * 60, index * per_point_quota, index * per_point_quota * usd_per_quota, **kwargs)
        for index in range(count)
    ]


def test_settled_timeline_recovers_the_rate_it_was_built_from():
    rows = ramp(8, usd_per_quota=2.0)
    timeline = settled_timeline(rows, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert len(timeline) == 1
    # 100 quota points at $2 each.
    assert timeline[0].estimate_usd == pytest.approx(200.0, rel=0.05)
    assert timeline[0].window == "weekly"
    assert timeline[0].source_window == "weekly"
    assert timeline[0].scale == 1.0


def test_open_window_is_kept_but_flagged_unsettled():
    rows = ramp(8)
    before_reset = settled_timeline(rows, now=datetime(2026, 7, 22, tzinfo=timezone.utc))
    after_reset = settled_timeline(rows, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    # The live value is still shown -- but a step into a window that is still
    # accumulating is convergence, not the provider changing anything.
    assert [row.settled for row in before_reset] == [False]
    assert [row.settled for row in after_reset] == [True]


def test_weekly_series_never_invents_a_five_hour_cap():
    """A weekly meter evidences a weekly total, not a five-hour restriction."""
    rows = ramp(8, window="weekly")
    assert settled_timeline(rows, target_window="five_hour") == []


def test_five_hour_windows_get_their_own_series():
    rows = []
    for index in range(3):
        reset = (START + timedelta(hours=5 * (index + 1))).isoformat()
        rows.extend(ramp(
            6, per_point_quota=6.0, usd_per_quota=1.0,
            window="five_hour", resets_at=reset, provider="claude", account_id=None,
        ))
        # Distinct windows need distinct observation times.
        for offset, row in enumerate(rows[-6:]):
            row["observed_at"] = (START + timedelta(hours=5 * index, minutes=offset * 45)).isoformat()

    timeline = settled_timeline(rows, target_window="five_hour",
                                now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert len(timeline) == 3
    assert {row.window for row in timeline} == {"five_hour"}
    assert all(row.scale == 1.0 for row in timeline)
    assert all(row.estimate_usd == pytest.approx(100.0, rel=0.1) for row in timeline)


def test_short_windows_convert_up_into_weekly_terms():
    rows = ramp(8, per_point_quota=4.0, usd_per_quota=2.0, provider="claude", account_id=None)
    # Over the same period the five-hour meter advances 70 points and the weekly one 28,
    # so a five-hour window's spend is worth 2.5 of itself in weekly terms.
    for index in range(8):
        rows.append(point(
            index * 60, index * 10.0, index * 20.0, window="five_hour",
            resets_at="2026-07-20T20:00:00+00:00", provider="claude", account_id=None,
        ))
    converted = [
        row for row in settled_timeline(rows, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        if row.source_window == "five_hour"
    ]
    assert converted, "five-hour evidence should reach the weekly series"
    assert converted[0].window == "weekly"
    assert converted[0].scale == pytest.approx(2.5, rel=0.05)


def test_replay_ends_on_the_value_the_rest_of_the_page_reports():
    rows = ramp(12)
    replayed = replay_series(rows)
    settled = settled_timeline(rows, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert replayed[-1].at == max(row["observed_at"] for row in rows)
    assert replayed[-1].estimate_usd == pytest.approx(settled[0].estimate_usd)


def test_replay_samples_are_bounded():
    rows = ramp(60)
    assert len(replay_series(rows, max_samples=10)) <= 12


def test_estimator_version_moves_when_a_decision_constant_moves(monkeypatch):
    before = estimator_version()
    monkeypatch.setattr(regression, "MIN_QUOTA_DELTA_PERCENT", 99.0)
    assert estimator_version() != before


def test_estimator_version_covers_the_cross_solve_threshold(monkeypatch):
    """The weekly series is partly converted short windows, so this constant moves it."""
    before = estimator_version()
    monkeypatch.setattr(crosssolve, "MIN_RATIO_QUOTA_PERCENT", 42.0)
    assert estimator_version() != before


def test_every_decision_constant_is_versioned():
    """A threshold added without being versioned would move the line invisibly, and a
    step in the line would then be unattributable to provider or estimator."""
    versioned = {
        (module.__name__, name)
        for module, name in VERSIONED_CONSTANTS + UNVERSIONED_CONSTANTS
    }
    declared = {
        (module.__name__, name)
        for module in (regression, crosssolve)
        for name in vars(module)
        if name.isupper() and isinstance(getattr(module, name), float)
    }
    assert declared - versioned == set(), "unversioned estimator constants"


def test_build_series_emits_both_windows_flat():
    rows = ramp(8, provider="claude", account_id=None)
    for index in range(8):
        rows.append(point(
            index * 60, index * 10.0, index * 20.0, window="five_hour",
            resets_at="2026-07-20T20:00:00+00:00", provider="claude", account_id=None,
        ))
    payload = build_series(rows, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert set(payload) == {"estimator_version", "settled", "ratios", "replay"}
    assert {row["window"] for row in payload["settled"]} == set(TARGET_WINDOWS)
    assert payload["ratios"], "the short-to-long ratio is what makes the two comparable"
    assert all(row["estimator_version"] == payload["estimator_version"] for row in payload["settled"])


def test_thin_windows_are_kept_out_of_the_settled_series():
    """Eight quota points cannot evidence what an allowance is worth, and plotted beside
    fuller windows a thin point reads as a step that never happened."""
    thin = ramp(3, per_point_quota=2.0)
    assert settled_timeline(thin, now=datetime(2026, 8, 1, tzinfo=timezone.utc)) == []


def test_direct_and_converted_weekly_points_are_distinguishable():
    """They are two measurements of one quantity, so they share provider, window and
    timestamp -- only source_window separates the series."""
    rows = ramp(8, provider="claude", account_id=None)
    for index in range(8):
        rows.append(point(
            index * 60, index * 10.0, index * 20.0, window="five_hour",
            resets_at="2026-07-20T20:00:00+00:00", provider="claude", account_id=None,
        ))
    weekly = [
        row for row in settled_timeline(rows, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        if row.window == "weekly"
    ]
    assert len(weekly) == 2
    assert len({row.source_window for row in weekly}) == 2
