from subbench.regression import RegressionEstimate
from subbench.timeseries import detect_regime_changes, rolling_values


def estimate(reset: str, value: float, span: float = 40.0) -> RegressionEstimate:
    return RegressionEstimate(
        provider="codex",
        window="weekly",
        reset_key=reset,
        estimate_usd=value,
        lower_usd=value * 0.9,
        upper_usd=value * 1.1,
        observation_count=8,
        slope_count=20,
        quota_span_percent=span,
        api_value_span_usd=value * span / 100,
        latest_observed_at=reset,
    )


def test_rolling_value_prefers_windows_with_more_quota_evidence() -> None:
    rows = [
        estimate("2026-07-01", 80, 5),
        estimate("2026-07-08", 100, 80),
        estimate("2026-07-15", 105, 80),
    ]
    current = rolling_values(rows)
    assert len(current) == 1
    assert current[0].estimate_usd == 100
    assert current[0].window_count == 3


def test_regime_change_requires_three_consistent_recent_windows() -> None:
    rows = [
        estimate("2026-06-01", 100),
        estimate("2026-06-08", 98),
        estimate("2026-06-15", 102),
        estimate("2026-06-22", 101),
        estimate("2026-06-29", 99),
        estimate("2026-07-06", 145),
        estimate("2026-07-13", 148),
        estimate("2026-07-20", 146),
    ]
    changes = detect_regime_changes(rows)
    assert len(changes) == 1
    assert changes[0].status == "likely"
    assert changes[0].change_percent > 40


def test_regime_change_ignores_one_recent_outlier() -> None:
    rows = [
        estimate("2026-06-01", 100),
        estimate("2026-06-08", 101),
        estimate("2026-06-15", 99),
        estimate("2026-06-22", 100),
        estimate("2026-06-29", 101),
        estimate("2026-07-06", 145),
        estimate("2026-07-13", 101),
        estimate("2026-07-20", 99),
    ]
    assert detect_regime_changes(rows) == []


def test_low_information_windows_are_excluded() -> None:
    rows = [estimate("2026-07-01", 500, 1), estimate("2026-07-08", 100, 30)]
    current = rolling_values(rows)
    assert current[0].estimate_usd == 100
    assert current[0].window_count == 1
