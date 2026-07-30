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


def test_too_few_pairs_is_provisional():
    result = classify(estimate(slopes=10))
    assert result.tier == PROVISIONAL
    assert "10 valid pairs" in result.reason


def test_wide_band_stops_at_likely():
    result = classify(estimate(lower=20.0, upper=200.0))
    assert result.tier == LIKELY


def test_cross_window_agreement_confirms_a_wide_band():
    # weekly: 100 points of quota worth $300. five_hour: 12 points worth $36.
    # 12/100 of the weekly window per five-hour window -> predicted weekly 36/0.12 = $300.
    weekly = estimate(value=300.0, lower=100.0, upper=600.0, covered=100.0, window="weekly")
    short = estimate(value=36.0, covered=12.0, window="five_hour")
    result = classify(weekly, [weekly, short])
    assert result.tier == CONFIRMED
    assert "five_hour" in result.reason


def test_cross_window_disagreement_does_not_confirm():
    weekly = estimate(value=300.0, lower=100.0, upper=600.0, covered=100.0, window="weekly")
    short = estimate(value=10.0, covered=12.0, window="five_hour")
    assert classify(weekly, [weekly, short]).tier == LIKELY


def test_corroboration_ignores_other_accounts_and_providers():
    weekly = estimate(value=300.0, lower=100.0, upper=600.0, covered=100.0, window="weekly")
    other_account = estimate(value=36.0, covered=12.0, window="five_hour", account_id="B")
    other_provider = estimate(value=36.0, covered=12.0, window="five_hour", provider="claude")
    assert corroboration(weekly, [other_account, other_provider]) is None


def test_relative_band_width():
    assert relative_band_width(estimate(value=100.0, lower=80.0, upper=120.0)) == 0.4
    assert relative_band_width(estimate(value=0.0)) is None


# The three live series as of 2026-07-31. These are regression cases: a threshold change
# that silently reclassifies real collected data should fail here.
def test_live_codex_weekly_is_provisional():
    codex = estimate(
        value=113.04, lower=61.10, upper=148.59,
        covered=45.0, unobserved=44.0, slopes=4842, window="weekly",
    )
    assert classify(codex).tier == PROVISIONAL


def test_live_claude_five_hour_is_confirmed():
    claude = estimate(
        value=36.88, lower=25.24, upper=47.71,
        covered=33.0, slopes=110, window="five_hour", provider="claude", account_id=None,
    )
    assert classify(claude).tier == CONFIRMED


def test_live_claude_weekly_is_provisional():
    claude = estimate(
        value=299.72, lower=224.34, upper=383.06,
        covered=4.0, slopes=37, window="weekly", provider="claude", account_id=None,
    )
    assert classify(claude).tier == PROVISIONAL
