from subbench.crosssolve import (
    DIVERGENCE_THRESHOLD,
    converted_estimates,
    divergences,
    window_ratios,
)
from subbench.regression import RegressionEstimate


def point(window, used, cost, observed, provider="claude", account_id=None):
    return {
        "provider": provider, "account_id": account_id, "window": window,
        "observed_at": observed, "used_percent": used, "cost_usd": cost,
        "resets_at": "2026-08-05T00:00:00+00:00",
        "duration_minutes": 300 if window == "five_hour" else 10080,
    }


def estimate(window, value, *, provider="claude", account_id=None, covered=30.0, span=None):
    return RegressionEstimate(
        provider=provider, window=window, reset_key="r1", estimate_usd=value,
        lower_usd=value * 0.9, upper_usd=value * 1.1, observation_count=20,
        slope_count=100, quota_span_percent=span if span is not None else covered,
        api_value_span_usd=value, latest_observed_at="2026-07-31T00:00:00+00:00",
        account_id=account_id, covered_quota_percent=covered, marginal_usd=value,
    )


def series():
    """A five-hour window advancing 66 points while the weekly advances 8.

    The same 4:33 proportion observed on real data, scaled so the weekly window clears
    the minimum movement a ratio needs to mean anything.
    """
    rows = []
    for index in range(12):
        stamp = f"2026-07-30T{index:02d}:00:00+00:00"
        rows.append(point("five_hour", index * 6.0, index * 1.0, stamp))
        rows.append(point("weekly", index * (8.0 / 11), index * 1.0, stamp))
    return rows


def test_ratio_is_measured_from_observed_movement():
    ratios = window_ratios(series())
    assert len(ratios) == 1
    ratio = ratios[0]
    assert ratio.short_window == "five_hour"
    assert ratio.long_window == "weekly"
    assert abs(ratio.ratio - 8.0 / 66.0) < 0.01


def test_short_window_converts_to_a_long_window_equivalent():
    # Matches the real observation that predicted $304 against a direct $299.
    ratios = window_ratios(series())
    converted = converted_estimates([estimate("five_hour", 36.88)], ratios)
    assert len(converted) == 1
    assert converted[0].window == "weekly"
    assert 280.0 < converted[0].estimate_usd < 320.0


def test_converted_evidence_is_expressed_in_long_window_terms():
    # 30 points of a five-hour window is worth far less than 30 points of a weekly one,
    # and treating it otherwise would let short windows dominate the pooled weight.
    ratios = window_ratios(series())
    converted = converted_estimates([estimate("five_hour", 36.88, covered=30.0)], ratios)
    assert converted[0].covered_quota_percent < 30.0


def test_a_ratio_needs_movement_in_both_windows():
    rows = []
    for index in range(6):
        stamp = f"2026-07-30T{index:02d}:00:00+00:00"
        rows.append(point("five_hour", index * 5.0, index * 1.0, stamp))
        rows.append(point("weekly", 1.0, index * 1.0, stamp))  # barely moves
    assert window_ratios(rows) == []


def test_ratio_uses_only_the_overlapping_period():
    rows = series()
    # A five-hour observation long after the weekly series ends must not inflate the
    # numerator against a denominator measured over a different workload.
    rows.append(point("five_hour", 99.0, 500.0, "2026-08-02T00:00:00+00:00"))
    ratio = window_ratios(rows)[0]
    assert abs(ratio.ratio - 8.0 / 66.0) < 0.02


def test_agreeing_windows_report_no_divergence():
    ratios = window_ratios(series())
    rows = [estimate("five_hour", 36.88), estimate("weekly", 300.0)]
    assert divergences(rows, ratios) == []


def test_disagreeing_windows_are_reported():
    ratios = window_ratios(series())
    rows = [estimate("five_hour", 36.88), estimate("weekly", 120.0)]
    found = divergences(rows, ratios)
    assert len(found) == 1
    assert found[0].scope == "window"
    assert found[0].difference < -DIVERGENCE_THRESHOLD


def test_accounts_on_one_plan_are_compared():
    rows = [
        estimate("weekly", 100.0, provider="codex", account_id="A"),
        estimate("weekly", 400.0, provider="codex", account_id="B"),
    ]
    found = divergences(rows, [], {"A": "plus", "B": "plus"})
    assert len(found) == 1
    assert found[0].scope == "account"
    assert "plus" in found[0].subject


def test_accounts_on_different_plans_are_never_compared():
    # Pooling a Plus account with a Pro one would average two different products, and a
    # difference between them is expected rather than a signal.
    rows = [
        estimate("weekly", 100.0, provider="codex", account_id="A"),
        estimate("weekly", 400.0, provider="codex", account_id="B"),
    ]
    assert divergences(rows, [], {"A": "plus", "B": "pro"}) == []


def test_accounts_without_a_known_plan_are_not_compared():
    rows = [
        estimate("weekly", 100.0, provider="codex", account_id="A"),
        estimate("weekly", 400.0, provider="codex", account_id="B"),
    ]
    assert divergences(rows, [], {"A": "plus"}) == []


def test_similar_accounts_report_nothing():
    rows = [
        estimate("weekly", 100.0, provider="codex", account_id="A"),
        estimate("weekly", 110.0, provider="codex", account_id="B"),
    ]
    assert divergences(rows, [], {"A": "plus", "B": "plus"}) == []


def test_a_converted_window_keeps_the_readings_behind_it():
    """A scaled band beside a claim of no readings would never reach a settled tier."""
    from subbench.crosssolve import combined_estimates
    from subbench.regression import robust_estimates

    points = []
    for index in range(20):
        stamp = f"2026-07-27T{index // 60:02d}:{index % 60:02d}:00Z"
        for window, step in (("five_hour", 4.0), ("weekly", 1.0)):
            points.append({
                "provider": "codex", "account_id": "A", "window": window,
                "resets_at": "2026-07-27T12:00:00Z", "observed_at": stamp,
                "used_percent": step * index, "cost_usd": 2.0 * index,
            })
    estimates, _ = combined_estimates(points, robust_estimates(points))
    converted = [e for e in estimates if "~via~" in e.reset_key]
    assert converted
    for estimate in converted:
        assert estimate.interval_count > 0
