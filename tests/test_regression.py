from subbench.regression import (
    MARGINAL_QUOTA_SPAN_PERCENT,
    MIN_QUOTA_DELTA_PERCENT,
    estimate_progress,
    pairwise_slopes,
    robust_estimates,
)


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
    assert estimates[0].estimate_usd == 25.0


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


def test_pairwise_slopes_exposes_every_valid_contribution():
    slopes = pairwise_slopes([
        point(10, 2.0, "2026-07-27T00:00:00Z"),
        point(20, 5.0, "2026-07-27T00:10:00Z"),
        point(30, 8.0, "2026-07-27T00:20:00Z"),
    ])
    assert len(slopes) == 3
    assert all(slope.slope_usd == 30.0 for slope in slopes)
    assert slopes[0].quota_delta_percent == 10.0
    assert slopes[0].api_value_delta_usd == 3.0


def test_estimate_progress_updates_as_observations_arrive():
    progress = estimate_progress([
        point(10, 2.0, "2026-07-27T00:00:00Z"),
        point(20, 5.0, "2026-07-27T00:10:00Z"),
        point(30, 9.0, "2026-07-27T00:20:00Z"),
    ])
    assert [row.slope_count for row in progress] == [1, 3]
    assert [row.estimate_usd for row in progress] == [30.0, 35.0]


def test_short_quota_pairs_do_not_outvote_long_ones():
    # One long, accurate span plus many one-point pairs whose slopes are wild because
    # a rounded percent carries +/-0.5% error. The long span must decide the estimate.
    points = [point(0, 0.0, "2026-07-27T00:00:00Z")]
    cost = 0.0
    for index in range(1, 41):
        cost += 3.0 if index % 2 else 0.05
        points.append(point(index, cost, f"2026-07-27T{index:02d}:00:00Z"))
    estimate = robust_estimates(points)[0]
    endpoint = points[-1]["cost_usd"] / (points[-1]["used_percent"] / 100.0)
    assert abs(estimate.estimate_usd - endpoint) < endpoint * 0.35


def test_pairs_below_the_quota_floor_are_excluded():
    points = [
        point(10, 2.0, "2026-07-27T00:00:00Z"),
        point(11, 9.0, "2026-07-27T00:10:00Z"),
    ]
    assert pairwise_slopes(points) == []
    assert robust_estimates(points) == []
    assert len(pairwise_slopes(points, min_quota_delta=0.0)) == 1


def test_quota_moving_without_recorded_value_is_not_a_free_window():
    # A stale usage import reports no new spend; treating that as a zero slope would
    # drag the estimate down rather than simply carrying no information.
    slopes = pairwise_slopes([
        point(10, 2.0, "2026-07-27T00:00:00Z"),
        point(30, 2.0, "2026-07-27T00:10:00Z"),
        point(50, 8.0, "2026-07-27T00:20:00Z"),
    ])
    assert all(slope.slope_usd > 0 for slope in slopes)
    # Only the fully observed 30 -> 50 interval survives. The 10 -> 50 pair straddles
    # the unrecorded stretch, so it inherits the missing value and goes too.
    assert len(slopes) == 1
    assert slopes[0].left_used_percent == 30.0


def test_default_floor_is_wide_enough_for_integer_quota_rounding():
    assert MIN_QUOTA_DELTA_PERCENT >= 2.0


def test_quota_spent_without_local_logs_is_excluded():
    # Quota consumed off-machine moves the meter with no local tokens. That pair has an
    # incomplete numerator, and being the widest span it would otherwise weigh the most.
    points = [
        point(10, 2.00, "2026-07-27T00:00:00Z"),
        point(55, 2.03, "2026-07-27T04:00:00Z"),   # 45 points of unobserved usage
        point(65, 12.03, "2026-07-27T05:00:00Z"),
        point(75, 22.03, "2026-07-27T06:00:00Z"),
    ]
    slopes = {round(slope.slope_usd) for slope in pairwise_slopes(points)}
    assert all(value > 50 for value in slopes), slopes
    estimate = robust_estimates(points)[0]
    assert estimate.estimate_usd > 50.0


def test_marginal_estimate_follows_a_mid_window_rate_change():
    # Cheap first half, expensive second half. The window average is still dragged down
    # by the cheap stretch; the marginal estimate should already report the new rate.
    points = [point(0, 0.0, "2026-07-27T00:00:00Z")]
    cost = 0.0
    for index in range(1, 41):
        cost += 0.10 if index <= 20 else 1.00
        points.append(point(index * 2, cost, f"2026-07-27T{index:02d}:00:00Z"))
    estimate = robust_estimates(points)[0]
    assert estimate.marginal_usd is not None
    # Recent stretch runs at $1.00 per 2 quota points, i.e. $50 per full entitlement.
    assert 40.0 < estimate.marginal_usd < 60.0
    assert estimate.marginal_usd > estimate.estimate_usd
    assert estimate.marginal_slope_count > 0
    assert estimate.marginal_span_percent <= MARGINAL_QUOTA_SPAN_PERCENT


def test_marginal_matches_window_average_when_the_rate_is_steady():
    points = [point(index * 5, index * 1.0, f"2026-07-27T{index:02d}:00:00Z") for index in range(12)]
    estimate = robust_estimates(points)[0]
    assert estimate.marginal_usd == estimate.estimate_usd


def test_marginal_is_absent_when_no_recent_pair_survives():
    points = [
        point(10, 2.0, "2026-07-27T00:00:00Z"),
        point(20, 5.0, "2026-07-27T00:10:00Z"),
    ]
    estimate = robust_estimates(points, marginal_span=0.0)[0]
    assert estimate.marginal_usd is None
    assert estimate.marginal_slope_count == 0


def test_coverage_reports_how_much_of_the_window_was_measured():
    points = [
        point(10, 2.00, "2026-07-27T00:00:00Z"),
        point(55, 2.03, "2026-07-27T04:00:00Z"),   # 45 points unmeasured
        point(65, 12.03, "2026-07-27T05:00:00Z"),  # 10 points measured
    ]
    estimate = robust_estimates(points)[0]
    assert estimate.unobserved_quota_percent == 45.0
    assert estimate.covered_quota_percent == 10.0
    assert round(estimate.coverage_percent) == 18
    # The span alone would claim the window is far better evidenced than it is.
    assert estimate.quota_span_percent == 55.0


def test_reset_boundary_jitter_stays_one_window():
    # A boundary reported either side of a minute must not split the window in two.
    points = [
        point(10, 2.0, "2026-07-27T00:00:00Z", "2026-07-27T15:29:59.210910+00:00"),
        point(30, 6.0, "2026-07-27T00:10:00Z", "2026-07-27T15:30:00+00:00"),
        point(50, 10.0, "2026-07-27T00:20:00Z", "2026-07-27T15:29:00+00:00"),
    ]
    estimates = robust_estimates(points)
    assert len(estimates) == 1
    assert estimates[0].observation_count == 3


def test_genuinely_different_reset_windows_still_separate():
    points = [
        point(10, 2.0, "2026-07-27T00:00:00Z", "2026-07-27T15:30:00+00:00"),
        point(30, 6.0, "2026-07-27T00:10:00Z", "2026-07-27T15:30:00+00:00"),
        point(10, 1.0, "2026-07-27T05:00:00Z", "2026-07-27T20:30:00+00:00"),
        point(30, 3.0, "2026-07-27T05:10:00Z", "2026-07-27T20:30:00+00:00"),
    ]
    assert len(robust_estimates(points)) == 2
