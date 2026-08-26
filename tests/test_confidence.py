from subbench.regression import RegressionEstimate
from subbench.server.confidence import (
    CONFIRMED,
    LIKELY,
    PROVISIONAL,
    classify,
    corroboration,
    relative_band_width,
)


def estimate(
    *,
    value=100.0,
    lower=80.0,
    upper=120.0,
    covered=40.0,
    unobserved=0.0,
    slopes=200,
    intervals=40,
    window="weekly",
    span=None,
    account_id="A",
    provider="codex",
) -> RegressionEstimate:
    return RegressionEstimate(
        provider=provider,
        window=window,
        reset_key="2026-08-05T04:11:00+00:00",
        estimate_usd=value,
        lower_usd=lower,
        upper_usd=upper,
        observation_count=50,
        slope_count=slopes,
        quota_span_percent=span if span is not None else covered + unobserved,
        api_value_span_usd=value,
        latest_observed_at="2026-07-30T12:00:00+00:00",
        account_id=account_id,
        covered_quota_percent=covered,
        unobserved_quota_percent=unobserved,
        interval_count=intervals,
    )


def test_tight_band_over_good_coverage_is_confirmed():
    assert classify(estimate()).tier == CONFIRMED


def test_too_little_measured_quota_is_provisional():
    result = classify(estimate(covered=10.0))
    assert result.tier == PROVISIONAL
    assert "10 points" in result.reason


def test_mostly_unmeasured_quota_is_provisional_however_many_pairs():
    # The failure this guards: a window can carry thousands of slopes and still rest on
    # half the quota it appears to. Pair count must not buy its way past coverage.
    result = classify(estimate(covered=45.0, unobserved=45.0, slopes=4842))
    assert result.tier == PROVISIONAL
    assert "no recorded spend" in result.reason


def test_too_few_independent_readings_is_provisional():
    result = classify(estimate(intervals=10))
    assert result.tier == PROVISIONAL
    assert "10 independent readings" in result.reason


def test_pairs_cannot_buy_their_way_past_the_reading_count():
    """Pairs grow as the square of readings, so a large pair count is not more evidence."""
    result = classify(estimate(slopes=4842, intervals=6))
    assert result.tier == PROVISIONAL
    assert "6 independent readings" in result.reason


def test_an_unbounded_estimate_is_provisional():
    """Too few increments to resample means no interval, which is not a narrow one."""
    result = classify(estimate(intervals=0, slopes=200))
    assert result.tier == PROVISIONAL


def test_wide_band_stops_at_likely():
    result = classify(estimate(lower=20.0, upper=200.0))
    assert result.tier == LIKELY


def test_cross_window_agreement_is_reported_but_never_promotes():
    """Both windows divide the same recorded spend, so agreement cannot vouch for it.

    weekly: 100 points of quota worth $300. five_hour: 12 points worth $36. That is
    12/100 of the weekly window per five-hour window, so the predicted weekly value is
    36/0.12 = $300 and the two agree exactly. The band is still wide, so it stays likely.
    """
    weekly = estimate(value=300.0, lower=100.0, upper=600.0, covered=100.0, window="weekly")
    short = estimate(value=36.0, covered=12.0, window="five_hour")
    result = classify(weekly, [weekly, short])
    assert result.tier == LIKELY
    assert "five_hour" in result.reason


def test_cross_window_disagreement_is_not_reported():
    weekly = estimate(value=300.0, lower=100.0, upper=600.0, covered=100.0, window="weekly")
    short = estimate(value=10.0, covered=12.0, window="five_hour")
    result = classify(weekly, [weekly, short])
    assert result.tier == LIKELY
    assert "five_hour" not in result.reason


def test_corroboration_ignores_other_accounts_and_providers():
    weekly = estimate(value=300.0, lower=100.0, upper=600.0, covered=100.0, window="weekly")
    other_account = estimate(value=36.0, covered=12.0, window="five_hour", account_id="B")
    other_provider = estimate(value=36.0, covered=12.0, window="five_hour", provider="claude")
    assert corroboration(weekly, [other_account, other_provider]) is None


def test_relative_band_width():
    assert relative_band_width(estimate(value=100.0, lower=80.0, upper=120.0)) == 0.4
    assert relative_band_width(estimate(value=0.0)) is None
    assert relative_band_width(estimate(intervals=0, lower=0.0, upper=0.0)) is None


# The three live series as of 2026-07-31. These are regression cases: a threshold change
# that silently reclassifies real collected data should fail here.
def test_live_codex_weekly_is_provisional():
    codex = estimate(
        value=113.04, lower=61.10, upper=148.59,
        covered=45.0, unobserved=44.0, slopes=4842, intervals=98, window="weekly",
    )
    assert classify(codex).tier == PROVISIONAL


def test_live_claude_five_hour_is_likely():
    """Confirmed under the old spread-of-pairs band, likely under a real interval.

    The reclassification is the point of the change rather than a casualty of it. The
    interval runs from $25.24 to $47.71 on an estimate of $36.88, which is 61% of the
    estimate. The old band narrowed as the square of the readings and called that settled.
    """
    claude = estimate(
        value=36.88, lower=25.24, upper=47.71,
        covered=33.0, slopes=110, intervals=21, window="five_hour",
        provider="claude", account_id=None,
    )
    assert classify(claude).tier == LIKELY


def test_live_claude_weekly_is_provisional():
    claude = estimate(
        value=299.72, lower=224.34, upper=383.06,
        covered=4.0, slopes=37, intervals=14, window="weekly",
        provider="claude", account_id=None,
    )
    assert classify(claude).tier == PROVISIONAL
