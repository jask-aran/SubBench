import pytest

from subbench.interval_regression import (
    interval_censored_fit,
    interval_censored_pair_aggregate,
)


def point(used, cost):
    return {"used_percent": used, "cost_usd": cost}


def test_exact_constant_rate_reports_zero_loss_range():
    fit = interval_censored_fit([
        point(0, 0),
        point(5, 10),
        point(10, 20),
    ])

    assert fit is not None
    assert fit.zero_loss
    assert fit.estimate_usd == pytest.approx(200.0)
    assert fit.lower_usd == pytest.approx(100.0 / 0.55)
    assert fit.upper_usd == pytest.approx(100.0 / 0.45)


def test_interval_fit_handles_a_rounded_constant_rate():
    # True usage is 0.4 percent per dollar. The meter reports rounded values.
    fit = interval_censored_fit([
        point(1, 2.0),
        point(5, 12.0),
        point(9, 22.0),
    ])

    assert fit is not None
    assert fit.estimate_usd == pytest.approx(250.0)
    assert fit.residual == pytest.approx(0.0)


def test_inconsistent_intervals_return_a_fit_without_false_confidence_range():
    fit = interval_censored_fit([
        point(0, 0),
        point(10, 10),
        point(20, 30),
    ])

    assert fit is not None
    assert fit.residual > 0
    assert not fit.zero_loss
    assert fit.lower_usd is None
    assert fit.upper_usd is None


def test_no_cost_span_has_no_fit():
    assert interval_censored_fit([point(1, 10), point(2, 10)]) is None


def test_pair_aggregate_preserves_the_existing_central_slope():
    aggregate = interval_censored_pair_aggregate([(10, 2.0)])

    assert aggregate is not None
    assert aggregate.estimate_usd == pytest.approx(20.0)
    assert aggregate.lower_usd == pytest.approx(200.0 / 11.0)
    assert aggregate.upper_usd == pytest.approx(200.0 / 9.0)
    assert aggregate.median_interval_width_usd == pytest.approx(
        200.0 / 9.0 - 200.0 / 11.0
    )


def test_pair_aggregate_rejects_a_one_point_delta_at_default_rounding():
    assert interval_censored_pair_aggregate([(1, 2.0)]) is None
