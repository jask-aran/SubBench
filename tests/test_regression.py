from subbench.regression import robust_estimates


def point(used, cost, observed, reset="reset-a", account_id=None):
    return {
        "provider": "codex",
        "window": "five_hour",
        "resets_at": reset,
        "used_percent": used,
        "cost_usd": cost,
        "observed_at": observed,
        "account_id": account_id,
    }


def test_regression_uses_all_positive_cumulative_slopes():
    estimates = robust_estimates([
        point(10, 2.0, "2026-07-27T00:00:00Z"),
        point(20, 5.0, "2026-07-27T00:10:00Z"),
        point(30, 8.0, "2026-07-27T00:20:00Z"),
    ])
    assert len(estimates) == 1
    estimate = estimates[0]
    assert estimate.estimate_usd == 30.0
    assert estimate.observation_count == 3
    assert estimate.slope_count == 3
    assert estimate.quota_span_percent == 20.0


def test_regression_ignores_duplicate_rounded_quota_points():
    estimates = robust_estimates([
        point(10, 2.0, "2026-07-27T00:00:00Z"),
        point(10, 2.5, "2026-07-27T00:05:00Z"),
        point(20, 5.0, "2026-07-27T00:10:00Z"),
    ])
    assert estimates[0].slope_count == 2
    assert estimates[0].estimate_usd == 27.5


def test_regression_separates_reset_windows():
    estimates = robust_estimates([
        point(10, 2.0, "2026-07-27T00:00:00Z", "reset-a"),
        point(20, 5.0, "2026-07-27T00:10:00Z", "reset-a"),
        point(5, 5.5, "2026-07-27T05:00:00Z", "reset-b"),
        point(15, 7.5, "2026-07-27T05:10:00Z", "reset-b"),
    ])
    assert len(estimates) == 2
    assert {estimate.reset_key for estimate in estimates} == {"reset-a", "reset-b"}


def test_regression_separates_accounts_with_same_reset_key():
    estimates = robust_estimates([
        point(10, 2.0, "2026-07-27T00:00:00Z", "reset-a", account_id="A"),
        point(20, 5.0, "2026-07-27T00:10:00Z", "reset-a", account_id="A"),
        point(10, 1.0, "2026-07-27T00:00:00Z", "reset-a", account_id="B"),
        point(20, 2.5, "2026-07-27T00:10:00Z", "reset-a", account_id="B"),
    ])
    assert len(estimates) == 2
    by_account = {estimate.account_id: estimate for estimate in estimates}
    assert set(by_account) == {"A", "B"}
    assert round(by_account["A"].estimate_usd, 2) == 30.0
    assert round(by_account["B"].estimate_usd, 2) == 15.0


def test_median_slope_limits_single_interval_outlier():
    estimates = robust_estimates([
        point(0, 0.0, "2026-07-27T00:00:00Z"),
        point(10, 3.0, "2026-07-27T00:10:00Z"),
        point(20, 6.0, "2026-07-27T00:20:00Z"),
        point(21, 20.0, "2026-07-27T00:21:00Z"),
    ])
    assert estimates[0].estimate_usd < 100.0
