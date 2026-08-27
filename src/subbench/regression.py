from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from random import Random
from statistics import median
from typing import Iterable, Mapping, Any

# provider, account_id, plan, window, reset_key. One series of comparable quota points.
SeriesKey = tuple[str, str | None, str | None, str, str]


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
    # The plan in force for every observation behind this estimate. Two accounts pool into
    # one product estimate only when this matches.
    plan: str | None = None
    # Independent increments behind the estimate. slope_count counts pairs, which grow as
    # the square of this and overstate how much evidence there is.
    interval_count: int = 0

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


def _group_points(points: Iterable[Mapping[str, Any]]) -> dict[SeriesKey, list[dict[str, Any]]]:
    """Group observations into one series per provider, account, plan, window and reset.

    The plan belongs in the key because a plan change resizes the entitlement. One quota
    point on Plus and one quota point on Pro are different quantities of value, so slopes
    measured on either side of an upgrade cannot go into the same median. Splitting here
    makes the upgrade start a new series instead of corrupting the one in progress.
    """
    prepared: list[tuple[tuple[str, str | None, str | None, str], str, dict[str, Any]]] = []
    for source_point in points:
        point = dict(source_point)
        account_id = point.get("account_id")
        account_id = account_id if isinstance(account_id, str) else None
        plan = point.get("plan")
        plan = plan if isinstance(plan, str) and plan else None
        series = (str(point["provider"]), account_id, plan, str(point["window"]))
        prepared.append((series, _reset_key(point.get("resets_at")), point))

    by_series: dict[tuple[str, str | None, str | None, str], list[str]] = {}
    for series, reset_key, _ in prepared:
        by_series.setdefault(series, []).append(reset_key)
    clusters = {series: _cluster_resets(keys) for series, keys in by_series.items()}

    groups: dict[SeriesKey, list[dict[str, Any]]] = {}
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
# The reference rate comes from other reset windows in the same provider/account/length
# series, so a target cannot define the rate that decides whether its own usage is missing.
# A peer is optional: without one, only the absolute floor below applies.
UNOBSERVED_RATE_FRACTION = 0.25
UNOBSERVED_RATE_MIN_QUOTA = 5.0

# Neither test may fire on a short interval. A provider's meter updates before the agent's
# logs are flushed and read, so quota routinely jumps several points a minute or two ahead
# of the spend that caused it. That looks identical to usage consumed elsewhere and is not:
# the cost arrives moments later, and pairs spanning both sides are perfectly good
# evidence. Usage genuinely consumed off-machine shows up over hours, not minutes.
UNOBSERVED_MIN_MINUTES = 30.0

# The mirror of the two rules above. They catch quota that moved with no spend beside it,
# which understates value. Spend that moved with no quota beside it overstates it by the
# same mechanism, and happens for real: a limit sitting at 100% while work continues on
# extra credits, or an API key billed to the same logs but charged to no meter at all.
# Measured over the month before this guard existed, 21 intervals showed it, one of them
# $41 of recorded spend against a meter that never ticked.
#
# Only spend against a meter at its ceiling counts, and the narrowness is deliberate.
#
# Two wider rules were tried against a month of real observations and both did damage. A
# rate test against the peer reference discards a healthy window measured against a
# contaminated peer, because the reference is depressed by exactly the missing usage the
# opposite rule looks for. Flagging any frozen meter is worse: cost and the meter arrive
# out of phase constantly -- 513 stalls in that month, median nine minutes -- because the
# provider ticks a whole percent while ccusage reports dollars continuously. That rule cut
# one window's widest usable pair from 100 quota points to 21 and put its estimate $80 out.
#
# Out-of-phase spend is not lost: the meter ticks later and accounts for it. Spend at the
# ceiling is different in kind. The meter cannot advance, so the quota is provably not
# being bought, and no reference rate is needed to see it. That is the extra-credit case:
# a limit reached, work continuing, dollars still recorded. It appeared as $32 of such
# spend across both providers in one month.
QUOTA_CEILING_PERCENT = 100.0
# Below this, a reading at the ceiling is rounding rather than spend charged elsewhere.
UNMETERED_MIN_VALUE_USD = 0.25

# A quota reading is only usable if the cost total beside it was confirmed at roughly the
# same moment. When collection stops, quota keeps advancing while cost stands still, and
# the pair understates the value of that quota badly.
MAX_COST_AGE_MINUTES = 30.0

# How much recent quota the marginal estimate looks back over. Wide enough that several
# pairs survive the floor, narrow enough to follow a mid-window change in model mix.
MARGINAL_QUOTA_SPAN_PERCENT = 20.0

# The band is a bootstrap interval over the window's independent increments, not a spread
# of its pairs.
#
# Every ordered pair is a sum of consecutive intervals, so n observations give about n^2/2
# pairs but only n-1 independent readings. A spread taken over the pairs therefore narrows
# as n^2 while the evidence grows as n, and it reports a window as settled long before it
# is. Resampling the increments instead keeps the dependence honest: the interval narrows
# with the square root of the number of increments, which is the rate real evidence buys.
#
# Resampling is seeded, so the same evidence always produces the same band. Two hundred
# resamples is ample for a tenth and a ninetieth percentile.
BOOTSTRAP_SAMPLES = 200
BOOTSTRAP_SEED = 20260826
# Below this many increments a resampled interval says more about the resampling than the
# window, so no band is reported and the window cannot reach the top tier on width.
MIN_BOOTSTRAP_INTERVALS = 8


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
            ):
                # The limit is spent and work continues on extra credit. A pair spanning
                # this divides real dollars by quota that could not move, so it reads as a
                # far richer subscription than the one being measured.
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
        if flagged[index] and quota_delta > 0:
            unmeasured += quota_delta
        counts.append(counts[-1] + (1 if flagged[index] else 0))
    return counts, advanced, unmeasured


def independent_intervals(
    rows: list[Mapping[str, Any]],
    *,
    unobserved: list[int],
    min_quota_delta: float = MIN_QUOTA_DELTA_PERCENT,
) -> list[tuple[float, float]]:
    """The window as consecutive non-overlapping (quota, value) blocks.

    These are the window's independent evidence: every valid pair is a sum of a run of
    them, so they carry the same information without counting any of it twice.

    A block closes once the meter has moved far enough to be worth dividing by, never on
    every reading. Requiring both to move within one reading looks equivalent and is not:
    the provider ticks a whole percent at a time while ccusage reports dollars
    continuously, so the two arrive out of phase and most readings move one or the other.
    On one real window that filter kept 97 blocks holding $26.56 of the $93.57 actually
    spent, and valued a full limit at a third of its true rate.
    """
    intervals: list[tuple[float, float]] = []
    start = 0
    for index in range(1, len(rows)):
        if unobserved[index] > unobserved[index - 1]:
            # A flagged step contaminates the block being built, so drop it and restart.
            start = index
            continue
        quota_delta = float(rows[index]["used_percent"]) - float(rows[start]["used_percent"])
        value_delta = float(rows[index]["cost_usd"]) - float(rows[start]["cost_usd"])
        if quota_delta >= min_quota_delta and value_delta > 0:
            intervals.append((quota_delta, value_delta))
            start = index
    return intervals


def bootstrap_band(
    intervals: list[tuple[float, float]],
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None]:
    """A tenth-to-ninetieth interval for the window's rate, by resampling increments.

    The statistic is total value over total quota, which is what the widest pair of a
    window measures and what the quota-weighted median of its pairs approximates. Both
    describe one full limit, so the interval is on the same scale as the estimate.
    """
    if len(intervals) < MIN_BOOTSTRAP_INTERVALS:
        return None, None
    rng = Random(seed)
    count = len(intervals)
    rates: list[float] = []
    for _ in range(samples):
        quota = value = 0.0
        for _ in range(count):
            drawn_quota, drawn_value = intervals[rng.randrange(count)]
            quota += drawn_quota
            value += drawn_value
        if quota > 0:
            rates.append(value / (quota / 100.0))
    if not rates:
        return None, None
    rates.sort()
    pick = lambda p: rates[min(int(p * (len(rates) - 1) + 0.5), len(rates) - 1)]
    return pick(0.10), pick(0.90)


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


def _ordered_groups(points: Iterable[Mapping[str, Any]]):
    for key, rows in _group_points(points).items():
        yield key, sorted(rows, key=lambda row: str(row["observed_at"]))


def pairwise_slopes(
    points: Iterable[Mapping[str, Any]], *, min_quota_delta: float = MIN_QUOTA_DELTA_PERCENT
) -> list[SlopeContribution]:
    contributions: list[SlopeContribution] = []
    groups = list(_ordered_groups(points))
    references = _peer_reference_rates(groups, min_quota_delta=min_quota_delta)
    for (provider, account_id, plan, window, reset_key), rows in groups:
        for left, right, _, _, slope in _valid_pairs(
            rows,
            min_quota_delta=min_quota_delta,
            reference_rate=references[(provider, account_id, plan, window, reset_key)],
        ):
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
    groups = list(_ordered_groups(points))
    for (provider, account_id, plan, window, reset_key), rows in groups:
        for index in range(1, len(rows)):
            current_at = str(rows[index]["observed_at"])
            visible_peers: list[tuple[SeriesKey, list[dict[str, Any]]]] = []
            for peer_key, peer_rows in groups:
                if peer_key[:4] != (provider, account_id, plan, window) or peer_key[4] == reset_key:
                    continue
                visible = [row for row in peer_rows if str(row["observed_at"]) <= current_at]
                if visible:
                    visible_peers.append((peer_key, visible))
            reference = _peer_reference_rates(
                [((provider, account_id, plan, window, reset_key), rows[: index + 1]), *visible_peers],
                min_quota_delta=min_quota_delta,
            )[(provider, account_id, plan, window, reset_key)]
            pairs = _valid_pairs(
                rows[: index + 1],
                min_quota_delta=min_quota_delta,
                reference_rate=reference,
            )
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
    groups = list(_ordered_groups(points))
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
