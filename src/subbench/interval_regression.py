"""Experimental regression for whole-percent entitlement observations.

The provider's meter reports a rounded percent. Treating that value as exact makes
small quota deltas produce unstable full-window values. This module keeps the
reported value as an interval and fits a monotone percent-per-dollar relationship.

This is deliberately separate from :mod:`subbench.regression` until it has been
compared with real windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Iterable, Mapping

from .regression import weighted_quantile


@dataclass(frozen=True)
class IntervalCensoredFit:
    """Fit result for one reset window."""

    slope_percent_per_usd: float
    estimate_usd: float
    residual: float
    observation_count: int
    feasible_slope_low: float | None = None
    feasible_slope_high: float | None = None

    @property
    def zero_loss(self) -> bool:
        return self.feasible_slope_low is not None and self.feasible_slope_high is not None

    @property
    def lower_usd(self) -> float | None:
        if self.feasible_slope_high is None or self.feasible_slope_high <= 0:
            return None
        return 100.0 / self.feasible_slope_high

    @property
    def upper_usd(self) -> float | None:
        if self.feasible_slope_low is None or self.feasible_slope_low <= 0:
            return None
        return 100.0 / self.feasible_slope_low


@dataclass(frozen=True)
class PairIntervalAggregate:
    """Quantization band aggregated from already-valid pairwise slopes."""

    estimate_usd: float
    lower_usd: float
    upper_usd: float
    pair_count: int
    median_interval_width_usd: float


def interval_censored_pair_aggregate(
    pairs: Iterable[tuple[float, float]], *, rounding_half_width: float = 0.5
) -> PairIntervalAggregate | None:
    """Add rounded-meter bounds to valid pairwise full-window estimates.

    Each pair is ``(quota_delta_percent, api_value_delta_usd)``. If both endpoint
    readings are rounded to the nearest whole percent, the true quota delta is within
    ``delta +/- 2 * rounding_half_width``. The central estimate therefore remains the
    existing ``100 * value_delta / reported_delta``; interval censoring adds an
    uncertainty band without inventing a new denominator. ``lower_usd`` and
    ``upper_usd`` are the weighted medians of the lower and upper bounds. They
    describe meter-rounding uncertainty only, not a confidence interval for missing
    usage or model-mix changes.
    """
    if rounding_half_width < 0:
        raise ValueError("rounding_half_width must be non-negative")

    intervals: list[tuple[float, float, float, float]] = []
    for quota_delta, value_delta in pairs:
        quota_delta = float(quota_delta)
        value_delta = float(value_delta)
        if not isfinite(quota_delta) or not isfinite(value_delta):
            continue
        if quota_delta <= 0 or value_delta <= 0:
            continue
        uncertainty = 2.0 * rounding_half_width
        low_delta = quota_delta - uncertainty
        high_delta = quota_delta + uncertainty
        if low_delta <= 0 or high_delta <= 0:
            continue
        lower = 100.0 * value_delta / high_delta
        upper = 100.0 * value_delta / low_delta
        center = 100.0 * value_delta / quota_delta
        intervals.append((lower, center, upper, quota_delta))

    if not intervals:
        return None
    weights = [item[3] for item in intervals]
    return PairIntervalAggregate(
        estimate_usd=weighted_quantile([item[1] for item in intervals], weights, 0.5),
        lower_usd=weighted_quantile([item[0] for item in intervals], weights, 0.5),
        upper_usd=weighted_quantile([item[2] for item in intervals], weights, 0.5),
        pair_count=len(intervals),
        median_interval_width_usd=sorted(item[2] - item[0] for item in intervals)[len(intervals) // 2],
    )


def interval_censored_fit(
    points: list[Mapping[str, Any]], *, rounding_half_width: float = 0.5
) -> IntervalCensoredFit | None:
    """Fit a bounded percent response against cumulative API value.

    ``used_percent`` is treated as a rounded observation with bounds
    ``used_percent +/- rounding_half_width``. The fitted response is
    ``intercept + slope_percent_per_usd * cost_usd``. A full-window value is
    ``100 / slope``.

    The optional feasible slope range is the set of slopes that place every fitted
    observation inside its interval with one common intercept. It is useful when
    the data are exactly consistent with a constant rate. Real workloads with model
    mix changes usually have no zero-loss range; the fitted point then remains only
    an experiment result, not a confidence interval.
    """
    if rounding_half_width < 0:
        raise ValueError("rounding_half_width must be non-negative")

    observations = _observations(points, rounding_half_width)
    if len({x for x, _, _ in observations}) < 2:
        return None

    feasible_low, feasible_high = _feasible_slope_range(observations)
    if feasible_low is not None and feasible_high is not None:
        slope = (feasible_low + feasible_high) / 2.0
        intercept, residual = _fit_intercept(observations, slope)
        del intercept
    else:
        slope = _fit_slope(observations)
        _, residual = _fit_intercept(observations, slope)

    if slope <= 0 or not isfinite(slope):
        return None
    return IntervalCensoredFit(
        slope_percent_per_usd=slope,
        estimate_usd=100.0 / slope,
        residual=residual,
        observation_count=len(observations),
        feasible_slope_low=feasible_low,
        feasible_slope_high=feasible_high,
    )


def _observations(
    points: list[Mapping[str, Any]], rounding_half_width: float
) -> list[tuple[float, float, float]]:
    values: list[tuple[float, float, float]] = []
    for point in points:
        try:
            x = float(point["cost_usd"])
            used = float(point["used_percent"])
        except (KeyError, TypeError, ValueError):
            continue
        if not isfinite(x) or not isfinite(used):
            continue
        values.append((x, used - rounding_half_width, used + rounding_half_width))
    if not values:
        return []
    origin = min(x for x, _, _ in values)
    return [(x - origin, lower, upper) for x, lower, upper in values]


def _feasible_slope_range(
    observations: list[tuple[float, float, float]]
) -> tuple[float | None, float | None]:
    """Return the common-intercept slope range with zero interval loss."""
    low = 0.0
    high = float("inf")
    ordered = sorted(observations)
    for index, (left_x, left_low, left_high) in enumerate(ordered):
        for right_x, right_low, right_high in ordered[index + 1 :]:
            delta_x = right_x - left_x
            if delta_x <= 0:
                continue
            # The two shifted response intervals must overlap for some intercept.
            low = max(low, (right_low - left_high) / delta_x)
            high = min(high, (right_high - left_low) / delta_x)
    high = max(high, 0.0)
    if low <= high:
        return low, high
    return None, None


def _fit_slope(observations: list[tuple[float, float, float]]) -> float:
    """Minimise squared distance outside the response intervals."""
    max_slope = 1.0
    ordered = sorted(observations)
    for index, (left_x, _, _) in enumerate(ordered):
        for right_x, _, right_high in ordered[index + 1 :]:
            delta_x = right_x - left_x
            if delta_x > 0:
                max_slope = max(max_slope, (right_high - ordered[index][1]) / delta_x)
    upper = max(max_slope * 2.0, 1.0)

    # The profile loss is convex in the slope. Ternary search is sufficient for this
    # diagnostic and avoids adding a numerical optimisation dependency.
    low = 0.0
    for _ in range(90):
        left = low + (upper - low) / 3.0
        right = upper - (upper - low) / 3.0
        if _profile_loss(observations, left) <= _profile_loss(observations, right):
            upper = right
        else:
            low = left
    return (low + upper) / 2.0


def _profile_loss(observations: list[tuple[float, float, float]], slope: float) -> float:
    return _fit_intercept(observations, slope)[1]


def _fit_intercept(
    observations: list[tuple[float, float, float]], slope: float
) -> tuple[float, float]:
    shifted = [(lower - slope * x, upper - slope * x) for x, lower, upper in observations]
    span = max(1.0, max(upper for _, upper in shifted) - min(lower for lower, _ in shifted))
    low = min(lower for lower, _ in shifted) - span
    high = max(upper for _, upper in shifted) + span

    for _ in range(90):
        intercept = (low + high) / 2.0
        derivative = sum(
            (intercept - lower) if intercept < lower else
            (intercept - upper) if intercept > upper else 0.0
            for lower, upper in shifted
        )
        if derivative > 0:
            high = intercept
        else:
            low = intercept

    intercept = (low + high) / 2.0
    loss = sum(
        (lower - intercept) ** 2 if intercept < lower else
        (intercept - upper) ** 2 if intercept > upper else 0.0
        for lower, upper in shifted
    )
    return intercept, loss
