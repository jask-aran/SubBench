from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import median
from typing import Any, Iterable

from .regression import RegressionEstimate


@dataclass(frozen=True)
class CurrentValue:
    provider: str
    window: str
    estimate_usd: float
    lower_usd: float
    upper_usd: float
    window_count: int
    quota_span_percent: float
    first_reset: str
    latest_reset: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegimeChange:
    provider: str
    window: str
    status: str
    first_observed_reset: str
    baseline_usd: float
    recent_usd: float
    change_percent: float
    baseline_windows: int
    recent_windows: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def informative_windows(
    estimates: Iterable[RegressionEstimate], *, min_quota_span: float = 5.0
) -> list[RegressionEstimate]:
    return [
        estimate
        for estimate in estimates
        if estimate.quota_span_percent >= min_quota_span
        and estimate.estimate_usd > 0
        and estimate.reset_key != "unknown"
    ]


def rolling_values(
    estimates: Iterable[RegressionEstimate], *, min_quota_span: float = 5.0
) -> list[CurrentValue]:
    groups: dict[tuple[str, str], list[RegressionEstimate]] = {}
    for estimate in informative_windows(estimates, min_quota_span=min_quota_span):
        groups.setdefault((estimate.provider, estimate.window), []).append(estimate)

    results: list[CurrentValue] = []
    for (provider, window), rows in groups.items():
        ordered = sorted(rows, key=lambda row: row.reset_key)
        limit = 10 if window == "weekly" else 30
        recent = ordered[-limit:]
        values = [row.estimate_usd for row in recent]
        weights = [min(row.quota_span_percent, 100.0) for row in recent]
        results.append(
            CurrentValue(
                provider=provider,
                window=window,
                estimate_usd=_weighted_median(values, weights),
                lower_usd=_weighted_quantile(values, weights, 0.10),
                upper_usd=_weighted_quantile(values, weights, 0.90),
                window_count=len(recent),
                quota_span_percent=sum(weights),
                first_reset=recent[0].reset_key,
                latest_reset=recent[-1].reset_key,
            )
        )
    return sorted(results, key=lambda row: (row.provider, row.window))


def detect_regime_changes(
    estimates: Iterable[RegressionEstimate],
    *,
    min_quota_span: float = 5.0,
    recent_count: int = 3,
    minimum_change: float = 0.20,
) -> list[RegimeChange]:
    groups: dict[tuple[str, str], list[RegressionEstimate]] = {}
    for estimate in informative_windows(estimates, min_quota_span=min_quota_span):
        groups.setdefault((estimate.provider, estimate.window), []).append(estimate)

    changes: list[RegimeChange] = []
    for (provider, window), rows in groups.items():
        ordered = sorted(rows, key=lambda row: row.reset_key)
        if len(ordered) < recent_count * 2:
            continue
        recent = ordered[-recent_count:]
        baseline = ordered[:-recent_count]
        baseline_value = median(row.estimate_usd for row in baseline)
        recent_value = median(row.estimate_usd for row in recent)
        if baseline_value <= 0:
            continue
        change = recent_value / baseline_value - 1.0
        if abs(change) < minimum_change:
            continue

        direction_consistent = all(
            (row.estimate_usd > baseline_value) == (change > 0) for row in recent
        )
        if not direction_consistent:
            continue
        status = "likely" if len(baseline) >= 5 else "developing"
        changes.append(
            RegimeChange(
                provider=provider,
                window=window,
                status=status,
                first_observed_reset=recent[0].reset_key,
                baseline_usd=baseline_value,
                recent_usd=recent_value,
                change_percent=change * 100.0,
                baseline_windows=len(baseline),
                recent_windows=len(recent),
            )
        )
    return sorted(changes, key=lambda row: row.first_observed_reset, reverse=True)


def window_history(
    estimates: Iterable[RegressionEstimate], *, min_quota_span: float = 0.0
) -> list[dict[str, Any]]:
    rows = [
        estimate.as_dict()
        for estimate in estimates
        if estimate.quota_span_percent >= min_quota_span
    ]
    return sorted(rows, key=lambda row: (row["provider"], row["window"], row["reset_key"]))


def _weighted_median(values: list[float], weights: list[float]) -> float:
    return _weighted_quantile(values, weights, 0.5)


def _weighted_quantile(values: list[float], weights: list[float], probability: float) -> float:
    pairs = sorted(zip(values, weights), key=lambda pair: pair[0])
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return median(values)
    threshold = total * probability
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return pairs[-1][0]
