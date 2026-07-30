from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable, Mapping, Any


@dataclass(frozen=True)
class RegressionEstimate:
    provider: str
    window: str
    reset_key: str
    estimate_usd: float
    lower_usd: float
    upper_usd: float
    observation_count: int
    slope_count: int
    quota_span_percent: float
    api_value_span_usd: float
    latest_observed_at: str
    account_id: str | None = None
    # The same calculation over only the most recent stretch of quota. estimate_usd
    # answers "what has this window averaged so far"; this answers "what is a quota
    # point worth now". They differ whenever the model mix shifts mid-window, and the
    # window average takes hours of evidence to follow a rate that already moved.
    marginal_usd: float | None = None
    marginal_lower_usd: float | None = None
    marginal_upper_usd: float | None = None
    marginal_slope_count: int = 0
    marginal_span_percent: float = 0.0
    # quota_span_percent is how far the meter travelled; covered_quota_percent is how
    # much of that travel had recorded spend beside it. When they diverge the estimate
    # rests on far less of the window than its span suggests.
    covered_quota_percent: float = 0.0
    unobserved_quota_percent: float = 0.0

    @property
    def coverage_percent(self) -> float:
        total = self.covered_quota_percent + self.unobserved_quota_percent
        return 100.0 * self.covered_quota_percent / total if total > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "coverage_percent": self.coverage_percent}


@dataclass(frozen=True)
class SlopeContribution:
    provider: str
    window: str
    reset_key: str
    left_observed_at: str
    right_observed_at: str
    left_used_percent: float
    right_used_percent: float
    left_cost_usd: float
    right_cost_usd: float
    slope_usd: float
    account_id: str | None = None

    @property
    def quota_delta_percent(self) -> float:
        return self.right_used_percent - self.left_used_percent

    @property
    def api_value_delta_usd(self) -> float:
        return self.right_cost_usd - self.left_cost_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "quota_delta_percent": self.quota_delta_percent,
            "api_value_delta_usd": self.api_value_delta_usd,
        }


@dataclass(frozen=True)
class EstimateProgress:
    provider: str
    window: str
    reset_key: str
    observed_at: str
    estimate_usd: float
    lower_usd: float
    upper_usd: float
    slope_count: int
    account_id: str | None = None


# Providers report a stable reset boundary that wanders by a few seconds between reads,
# so rounding alone still splits one window whenever the boundary straddles a minute.
# Reset boundaries for genuinely different windows are hours apart, so collapsing
# timestamps that fall within a few minutes of each other cannot merge two real windows.
RESET_CLUSTER_MINUTES = 5.0


def _cluster_resets(reset_keys: Iterable[str]) -> dict[str, str]:
    """Map each reset key to the earliest key within the clustering tolerance."""
    parsed: list[tuple[datetime, str]] = []
    passthrough: dict[str, str] = {}
    for key in sorted(set(reset_keys)):
        try:
            parsed.append((datetime.fromisoformat(key.replace("Z", "+00:00")), key))
        except ValueError:
            passthrough[key] = key
    mapping = dict(passthrough)
    anchor: tuple[datetime, str] | None = None
    for moment, key in sorted(parsed):
        if anchor is None or (moment - anchor[0]).total_seconds() > RESET_CLUSTER_MINUTES * 60:
            anchor = (moment, key)
        mapping[key] = anchor[1]
    return mapping


def _group_points(points: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str | None, str, str], list[dict[str, Any]]]:
    prepared: list[tuple[tuple[str, str | None, str], str, dict[str, Any]]] = []
    for source_point in points:
        point = dict(source_point)
        account_id = point.get("account_id")
        account_id = account_id if isinstance(account_id, str) else None
        series = (str(point["provider"]), account_id, str(point["window"]))
        prepared.append((series, _reset_key(point.get("resets_at")), point))

    by_series: dict[tuple[str, str | None, str], list[str]] = {}
    for series, reset_key, _ in prepared:
        by_series.setdefault(series, []).append(reset_key)
    clusters = {series: _cluster_resets(keys) for series, keys in by_series.items()}

    groups: dict[tuple[str, str | None, str, str], list[dict[str, Any]]] = {}
    for series, reset_key, point in prepared:
        groups.setdefault((*series, clusters[series][reset_key]), []).append(point)
    return groups


# Providers report quota as a whole integer percent, so a pair one point apart carries
# +/-0.5% error in the denominator: roughly +/-50% on that slope, and because the error
# enters through 1/delta the resulting distribution is heavy-tailed to the *right*. Short
# pairs also outnumber long ones quadratically, so an unweighted median is decided by the
# least reliable evidence and gets worse as observations accumulate. Weighting each slope
# by the quota it spans makes a 50-point pair count for fifty 1-point pairs, which is the
# ratio of their information content.
MIN_QUOTA_DELTA_PERCENT = 2.0

# ccusage only sees this machine's logs. Quota spent through a provider's web or cloud
# runner, or from another machine on the same account, moves the meter while leaving no
# local tokens, so the pair spanning it reports a near-zero rate over a wide quota span
# and, being wide, is weighted most heavily of all. Such a pair has an incomplete
# numerator rather than a small one: the same defect as a pair whose value did not move
# at all, one notch less extreme. Below a cent of API value per quota point, no real
# subscription is being measured -- that is a full entitlement worth under a dollar.
MIN_VALUE_PER_QUOTA_POINT = 0.01
UNOBSERVED_QUOTA_POINTS = 5.0

# The absolute floor above only asks whether value moved at all, which catches a stretch
# that was entirely off-machine and misses one that was mostly off-machine. A stretch
# running far below the window's own rate is the same defect in weaker form: local spend
# divided by local-plus-elsewhere quota, which biases the estimate down.
#
# The reference rate is derived from the window's surviving intervals, so the test is
# self-calibrating and needs no assumption about what a plan is worth. It is applied in a
# single refinement pass -- iterating to convergence would let the estimate select the
# evidence that supports it.
UNOBSERVED_RATE_FRACTION = 0.25
UNOBSERVED_RATE_MIN_QUOTA = 5.0

# Neither test may fire on a short interval. A provider's meter updates before the agent's
# logs are flushed and read, so quota routinely jumps several points a minute or two ahead
# of the spend that caused it. That looks identical to usage consumed elsewhere and is not:
# the cost arrives moments later, and pairs spanning both sides are perfectly good
# evidence. Usage genuinely consumed off-machine shows up over hours, not minutes.
UNOBSERVED_MIN_MINUTES = 30.0

# A quota reading is only usable if the cost total beside it was confirmed at roughly the
# same moment. When collection stops, quota keeps advancing while cost stands still, and
# the pair understates the value of that quota badly.
MAX_COST_AGE_MINUTES = 30.0

# How much recent quota the marginal estimate looks back over. Wide enough that several
# pairs survive the floor, narrow enough to follow a mid-window change in model mix.
MARGINAL_QUOTA_SPAN_PERCENT = 20.0


def _interval_minutes(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    start = _moment(left.get("observed_at"))
    end = _moment(right.get("observed_at"))
    if start is None or end is None:
        return float("inf")  # undated rows: fall back to testing on quota alone
    return (end - start).total_seconds() / 60.0


def _moment(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _unobserved_intervals(
    rows: list[Mapping[str, Any]], *, min_value_per_quota_point: float,
    reference_rate: float | None = None,
) -> tuple[list[int], float, float]:
    """Prefix counts of adjacent intervals whose spend was never recorded locally.

    Any pair spanning one of these inherits its missing value, however far apart its own
    endpoints are, so the whole pair has to go -- not just pairs that sit entirely inside.

    Also totals the quota that advanced, and how much of it advanced unmeasured, so a
    report can say how much of the window the estimate actually rests on.
    """
    counts = [0]
    advanced = 0.0
    unmeasured = 0.0
    for left, right in zip(rows, rows[1:]):
        quota_delta = float(right["used_percent"]) - float(left["used_percent"])
        value_delta = float(right["cost_usd"]) - float(left["cost_usd"])
        if quota_delta > 0:
            advanced += quota_delta
        long_enough = _interval_minutes(left, right) >= UNOBSERVED_MIN_MINUTES
        unobserved = (
            long_enough
            and quota_delta >= UNOBSERVED_QUOTA_POINTS
            and value_delta < quota_delta * min_value_per_quota_point
        )
        if not unobserved and long_enough and reference_rate and quota_delta >= UNOBSERVED_RATE_MIN_QUOTA:
            # Proportion, not presence: spend that moved but nowhere near far enough for
            # the quota it accompanied means most of that quota was consumed elsewhere.
            rate = value_delta / (quota_delta / 100.0)
            unobserved = rate < reference_rate * UNOBSERVED_RATE_FRACTION
        if unobserved:
            unmeasured += quota_delta
        counts.append(counts[-1] + (1 if unobserved else 0))
    return counts, advanced, unmeasured


def _valid_pairs(
    rows: list[Mapping[str, Any]],
    *,
    min_quota_delta: float,
    min_value_per_quota_point: float = MIN_VALUE_PER_QUOTA_POINT,
    reference_rate: float | None = None,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], float, float, float]]:
    """Every ordered pair whose quota and value both moved far enough to be informative."""
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any], float, float, float]] = []
    floor = max(min_quota_delta, 0.0)
    unobserved, _, _ = _unobserved_intervals(
        rows, min_value_per_quota_point=min_value_per_quota_point, reference_rate=reference_rate,
    )
    for index, left in enumerate(rows):
        for offset, right in enumerate(rows[index + 1 :], start=index + 1):
            # Skip the pair if any interval between its endpoints was never observed.
            if unobserved[offset] > unobserved[index]:
                continue
            quota_delta = float(right["used_percent"]) - float(left["used_percent"])
            value_delta = float(right["cost_usd"]) - float(left["cost_usd"])
            # A pair where quota moved but recorded value did not is a stale or
            # incomplete usage import, not evidence that the quota was free.
            if quota_delta < floor or quota_delta <= 0 or value_delta <= 0:
                continue
            pairs.append((left, right, quota_delta, value_delta, value_delta / (quota_delta / 100.0)))
    return pairs


def _ordered_groups(points: Iterable[Mapping[str, Any]]):
    for key, rows in _group_points(points).items():
        yield key, sorted(rows, key=lambda row: str(row["observed_at"]))


def pairwise_slopes(
    points: Iterable[Mapping[str, Any]], *, min_quota_delta: float = MIN_QUOTA_DELTA_PERCENT
) -> list[SlopeContribution]:
    contributions: list[SlopeContribution] = []
    for (provider, account_id, window, reset_key), rows in _ordered_groups(points):
        for left, right, _, _, slope in _valid_pairs(rows, min_quota_delta=min_quota_delta):
            contributions.append(SlopeContribution(
                provider=provider,
                window=window,
                reset_key=reset_key,
                left_observed_at=str(left["observed_at"]),
                right_observed_at=str(right["observed_at"]),
                left_used_percent=float(left["used_percent"]),
                right_used_percent=float(right["used_percent"]),
                left_cost_usd=float(left["cost_usd"]),
                right_cost_usd=float(right["cost_usd"]),
                slope_usd=slope,
                account_id=account_id,
            ))
    return contributions


def estimate_progress(
    points: Iterable[Mapping[str, Any]], *, min_quota_delta: float = MIN_QUOTA_DELTA_PERCENT
) -> list[EstimateProgress]:
    progress: list[EstimateProgress] = []
    for (provider, account_id, window, reset_key), rows in _ordered_groups(points):
        for index in range(1, len(rows)):
            pairs = _valid_pairs(rows[: index + 1], min_quota_delta=min_quota_delta)
            # Only replot once the newest observation actually contributed a pair.
            if not pairs or pairs[-1][1] is not rows[index]:
                continue
            slopes = [pair[4] for pair in pairs]
            weights = [pair[2] for pair in pairs]
            progress.append(EstimateProgress(
                provider=provider,
                window=window,
                reset_key=reset_key,
                observed_at=str(rows[index]["observed_at"]),
                estimate_usd=weighted_quantile(slopes, weights, 0.5),
                lower_usd=weighted_quantile(slopes, weights, 0.10),
                upper_usd=weighted_quantile(slopes, weights, 0.90),
                slope_count=len(slopes),
                account_id=account_id,
            ))
    return progress


def _marginal_estimate(
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any], float, float, float]],
    *,
    marginal_span: float,
) -> tuple[float | None, float | None, float | None, int, float]:
    """The weighted estimate over pairs lying entirely in the most recent quota stretch."""
    if not pairs or marginal_span <= 0:
        return None, None, None, 0, 0.0
    latest = max(float(pair[1]["used_percent"]) for pair in pairs)
    threshold = latest - marginal_span
    recent = [pair for pair in pairs if float(pair[0]["used_percent"]) >= threshold]
    if not recent:
        return None, None, None, 0, 0.0
    slopes = [pair[4] for pair in recent]
    weights = [pair[2] for pair in recent]
    earliest = min(float(pair[0]["used_percent"]) for pair in recent)
    return (
        weighted_quantile(slopes, weights, 0.5),
        weighted_quantile(slopes, weights, 0.10),
        weighted_quantile(slopes, weights, 0.90),
        len(recent),
        latest - earliest,
    )


def robust_estimates(
    points: Iterable[Mapping[str, Any]],
    *,
    min_quota_delta: float = MIN_QUOTA_DELTA_PERCENT,
    marginal_span: float = MARGINAL_QUOTA_SPAN_PERCENT,
) -> list[RegressionEstimate]:
    estimates: list[RegressionEstimate] = []
    for (provider, account_id, window, reset_key), ordered in _ordered_groups(points):
        pairs = _valid_pairs(ordered, min_quota_delta=min_quota_delta)
        if not pairs:
            continue
        # Second pass: re-test intervals against the rate the first pass implies, so a
        # stretch that was only partly off-machine is excluded too.
        reference = weighted_quantile([p[4] for p in pairs], [p[2] for p in pairs], 0.5)
        refined = _valid_pairs(ordered, min_quota_delta=min_quota_delta, reference_rate=reference)
        if refined:
            pairs = refined
        slopes = [pair[4] for pair in pairs]
        weights = [pair[2] for pair in pairs]
        marginal, marginal_lower, marginal_upper, marginal_count, marginal_observed = (
            _marginal_estimate(pairs, marginal_span=marginal_span)
        )

        _, advanced, unmeasured = _unobserved_intervals(
            ordered, min_value_per_quota_point=MIN_VALUE_PER_QUOTA_POINT,
            reference_rate=reference,
        )
        quota_values = [float(row["used_percent"]) for row in ordered]
        cost_values = [float(row["cost_usd"]) for row in ordered]
        estimates.append(
            RegressionEstimate(
                provider=provider,
                window=window,
                reset_key=reset_key,
                estimate_usd=weighted_quantile(slopes, weights, 0.5),
                lower_usd=weighted_quantile(slopes, weights, 0.10),
                upper_usd=weighted_quantile(slopes, weights, 0.90),
                observation_count=len(ordered),
                slope_count=len(slopes),
                quota_span_percent=max(quota_values) - min(quota_values),
                api_value_span_usd=max(cost_values) - min(cost_values),
                latest_observed_at=str(ordered[-1]["observed_at"]),
                account_id=account_id,
                marginal_usd=marginal,
                marginal_lower_usd=marginal_lower,
                marginal_upper_usd=marginal_upper,
                marginal_slope_count=marginal_count,
                marginal_span_percent=marginal_observed,
                covered_quota_percent=advanced - unmeasured,
                unobserved_quota_percent=unmeasured,
            )
        )

    return sorted(estimates, key=lambda row: row.latest_observed_at, reverse=True)


def weighted_quantile(values: list[float], weights: list[float], probability: float) -> float:
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


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _reset_key(value: Any) -> str:
    if value is None:
        return "unknown"
    raw = str(value)
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return (timestamp + timedelta(seconds=30)).replace(second=0, microsecond=0).isoformat()
