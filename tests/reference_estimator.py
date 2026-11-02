"""The estimator as it was before the vectorised kernels: the reference the fast path must equal.

Copied verbatim from regression.py at the commit before the change; only the module
references are adjusted. Do not "fix" anything here: it is the oracle.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from subbench.regression import (
    MARGINAL_QUOTA_SPAN_PERCENT, MIN_QUOTA_DELTA_PERCENT, MIN_VALUE_PER_QUOTA_POINT, QUOTA_CEILING_PERCENT,
    UNMETERED_MIN_VALUE_USD, UNOBSERVED_MIN_MINUTES, UNOBSERVED_QUOTA_POINTS, UNOBSERVED_RATE_FRACTION,
    UNOBSERVED_RATE_MIN_QUOTA, RegressionEstimate, SeriesKey, _marginal_estimate, _moment, _ordered_groups,
    bootstrap_band, independent_intervals, weighted_quantile,
)


def _unobserved_intervals(
    rows: list[Mapping[str, Any]], *, min_value_per_quota_point: float,
    reference_rate: float | None = None,
) -> tuple[list[int], float, float]:
    """Prefix counts of intervals whose spend was never recorded locally.

    A flagged stretch can contain several short collection intervals. The 30-minute guard
    applies to the span used to detect it, not to every meter update inside it. Each
    component interval is marked so any pair crossing the stretch inherits its missing
    value, however far apart its own endpoints are.

    Also totals the quota that advanced, and how much of it advanced unmeasured, so a
    report can say how much of the window the estimate actually rests on.
    """
    # Every column this quadratic loop reads is converted once. It used to parse both
    # endpoints' timestamps on each of its several million iterations, which cost more
    # than the arithmetic it guards.
    used = [float(row["used_percent"]) for row in rows]
    cost = [float(row["cost_usd"]) for row in rows]
    moments = [_moment(row.get("observed_at")) for row in rows]
    anchor = next((moment for moment in moments if moment is not None), None)
    # Minutes from the first dated reading, so an interval is one subtraction. An undated
    # row keeps the old meaning of an unbounded gap: it is tested on quota alone.
    elapsed: list[float | None] = [
        None if moment is None or anchor is None else (moment - anchor).total_seconds() / 60.0
        for moment in moments
    ]

    flagged = [False] * max(len(rows) - 1, 0)
    # Readings below the ceiling up to each index. A pair can sit at 100% on both ends
    # and still have left it in between: the post-reset blip, then a real week that
    # fills again. Extra credit is only spend that never left the ceiling.
    ceiling_below = [0]
    for value in used:
        ceiling_below.append(ceiling_below[-1] + (value < QUOTA_CEILING_PERCENT))
    for start in range(len(rows)):
        left_used = used[start]
        left_cost = cost[start]
        left_elapsed = elapsed[start]
        for end in range(start + 1, len(rows)):
            quota_delta = used[end] - left_used
            value_delta = cost[end] - left_cost
            right_elapsed = elapsed[end]
            if (
                left_elapsed is not None
                and right_elapsed is not None
                and right_elapsed - left_elapsed < UNOBSERVED_MIN_MINUTES
            ):
                continue
            unobserved = (
                quota_delta >= UNOBSERVED_QUOTA_POINTS
                and value_delta < quota_delta * min_value_per_quota_point
            )
            if not unobserved and reference_rate and quota_delta >= UNOBSERVED_RATE_MIN_QUOTA:
                # Proportion, not presence: spend that moved but nowhere near far enough for
                # the quota it accompanied means most of that quota was consumed elsewhere.
                rate = value_delta / (quota_delta / 100.0)
                unobserved = rate < reference_rate * UNOBSERVED_RATE_FRACTION
            if (
                not unobserved
                and quota_delta <= 0
                and value_delta >= UNMETERED_MIN_VALUE_USD
                and left_used >= QUOTA_CEILING_PERCENT
                and used[end] >= QUOTA_CEILING_PERCENT
                and ceiling_below[end + 1] == ceiling_below[start]
            ):
                # The limit is spent and work continues on extra credit. The whole span
                # has to stay at the ceiling. A 100% blip and a later real 100% have
                # both ends there, and treating that pair as extra credit discards the week.
                unobserved = True
            if unobserved:
                flagged[start:end] = [True] * (end - start)

    counts = [0]
    advanced = 0.0
    unmeasured = 0.0
    for index in range(len(rows) - 1):
        quota_delta = used[index + 1] - used[index]
        if quota_delta > 0:
            advanced += quota_delta
            if flagged[index]:
                unmeasured += quota_delta
        elif quota_delta < 0:
            # A spike that reversed (100% for a few minutes, then the real meter) was
            # counted as advance on the way up. Take it back, unmeasured first.
            back = -quota_delta
            take = min(unmeasured, back)
            unmeasured -= take
            advanced -= take
            advanced = max(0.0, advanced - (back - take))
        counts.append(counts[-1] + (1 if flagged[index] else 0))
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


def _peer_reference_rates(
    groups: list[tuple[SeriesKey, list[dict[str, Any]]]],
    *,
    min_quota_delta: float,
) -> dict[SeriesKey, float | None]:
    """Return one reference rate per window, using only other reset windows.

    A target window must not define the rate that decides whether its own evidence is
    missing. Otherwise a single under-recorded window can make its low rate look normal.
    Peer pairs use the absolute floor only; relative filtering needs a trusted peer and is
    applied later to the target window.
    """
    by_series: dict[
        tuple[str, str | None, str | None, str],
        list[tuple[SeriesKey, list[dict[str, Any]]]],
    ] = {}
    for key, rows in groups:
        by_series.setdefault(key[:4], []).append((key, rows))

    # Each window's own pairs, computed once. Every window is a peer of every other window
    # in its series, so building K reference rates by asking each target for its peers'
    # pairs made K*(K-1) calls where K suffice, and each call is quadratic in that window's
    # readings. On a month of observations that alone was most of the twenty seconds every
    # command spent before printing anything.
    contributions: dict[SeriesKey, tuple[list[float], list[float]]] = {}
    for key, rows in groups:
        slopes: list[float] = []
        weights: list[float] = []
        for _, _, quota_delta, _, slope in _valid_pairs(rows, min_quota_delta=min_quota_delta):
            slopes.append(slope)
            weights.append(quota_delta)
        contributions[key] = (slopes, weights)

    # One sort per series rather than one per target. Every target needs the weighted
    # median of the same pooled slopes minus its own, so pooling and sorting once and then
    # walking that order while skipping the target's own contributions gives each answer
    # exactly, without re-sorting tens of thousands of slopes K times over.
    references: dict[SeriesKey, float | None] = {}
    for series, members in by_series.items():
        slopes: list[float] = []
        weights: list[float] = []
        sources: list[int] = []
        totals: list[float] = []
        for index, (member_key, _rows) in enumerate(members):
            member_slopes, member_weights = contributions[member_key]
            slopes.extend(member_slopes)
            weights.extend(member_weights)
            sources.extend([index] * len(member_slopes))
            totals.append(sum(member_weights))
        order = sorted(range(len(slopes)), key=slopes.__getitem__)
        grand_total = sum(totals)

        for index, (member_key, _rows) in enumerate(members):
            threshold = (grand_total - totals[index]) * 0.5
            if threshold <= 0:
                # No peer contributed a usable pair, so this window has no reference and
                # only the absolute floor applies to it.
                references[member_key] = None
                continue
            cumulative = 0.0
            for position in order:
                if sources[position] == index:
                    continue
                cumulative += weights[position]
                if cumulative >= threshold:
                    references[member_key] = slopes[position]
                    break
            else:
                references[member_key] = slopes[order[-1]]
    return references


def robust_estimates(
    points: Iterable[Mapping[str, Any]],
    *,
    min_quota_delta: float = MIN_QUOTA_DELTA_PERCENT,
    marginal_span: float = MARGINAL_QUOTA_SPAN_PERCENT,
    groups: list | None = None,  # the one addition: estimate already-grouped windows as given
) -> list[RegressionEstimate]:
    estimates: list[RegressionEstimate] = []
    groups = list(_ordered_groups(points)) if groups is None else groups
    references = _peer_reference_rates(groups, min_quota_delta=min_quota_delta)
    for (provider, account_id, plan, window, reset_key), ordered in groups:
        reference = references[(provider, account_id, plan, window, reset_key)]
        pairs = _valid_pairs(
            ordered,
            min_quota_delta=min_quota_delta,
            reference_rate=reference,
        )
        if not pairs:
            continue
        slopes = [pair[4] for pair in pairs]
        weights = [pair[2] for pair in pairs]
        marginal, marginal_lower, marginal_upper, marginal_count, marginal_observed = (
            _marginal_estimate(pairs, marginal_span=marginal_span)
        )

        unobserved_prefix, advanced, unmeasured = _unobserved_intervals(
            ordered, min_value_per_quota_point=MIN_VALUE_PER_QUOTA_POINT,
            reference_rate=reference,
        )
        intervals = independent_intervals(ordered, unobserved=unobserved_prefix)
        lower, upper = bootstrap_band(intervals)
        quota_values = [float(row["used_percent"]) for row in ordered]
        cost_values = [float(row["cost_usd"]) for row in ordered]
        estimates.append(
            RegressionEstimate(
                provider=provider,
                window=window,
                reset_key=reset_key,
                estimate_usd=weighted_quantile(slopes, weights, 0.5),
                # None when there are too few increments to resample. The confidence tier
                # treats an absent band as "not shown to be settled", never as a narrow one.
                lower_usd=lower if lower is not None else 0.0,
                upper_usd=upper if upper is not None else 0.0,
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
                plan=plan,
                interval_count=len(intervals) if lower is not None else 0,
            )
        )

    return sorted(estimates, key=lambda row: row.latest_observed_at, reverse=True)
