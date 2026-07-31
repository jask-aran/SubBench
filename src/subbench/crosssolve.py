"""Combine independent measurements of the same entitlement, and notice when they disagree.

Two kinds of measurement describe one underlying quantity and are currently derived in
isolation.

**Across window types.** A provider exposing both a five-hour and a weekly allowance is
metering one subscription twice. Consuming a five-hour window also consumes part of the
weekly one, at a ratio the observations reveal, so a five-hour estimate converts into a
weekly-equivalent one. That matters because the short window turns over roughly 34 times a
week while the long one turns over once: pooling them gives the weekly figure the evidence
of the short window instead of leaving it resting on a single observation period.

**Across accounts.** Two accounts on the same plan are separate entitlements, so their
*pairs* must never be pooled -- one percent of each is a different physical allowance.
Their *estimates* describe the same product and can be. Plan equality is required rather
than assumed: pooling a Plus account with a Pro one would average two different things.

Divergence is the useful by-product. Independent measurements of one quantity should
agree; when they stop agreeing, either an assumption here is wrong or something changed on
the provider's side. Neither is visible from a single series.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from .regression import RegressionEstimate, weighted_quantile

# Longer windows are the ones short windows convert *into*: they are the meter a
# subscription is usually sold against, and the one a person plans around.
WINDOW_MINUTES = {"five_hour": 300, "weekly": 10080}

# A ratio measured over too little movement is mostly rounding. Quota is reported as a
# whole percent, so a long window that advanced n points carries about 0.5/n relative
# error in the ratio -- 12% at four points, 10% at five, 2.5% at twenty. Five is the point
# where ratio error is comfortably inside the divergence threshold below, so a real
# disagreement is not drowned by measurement error in the conversion itself.
MIN_RATIO_QUOTA_PERCENT = 5.0

# Independent measurements of one entitlement disagreeing by more than this is worth
# reporting. Set well above ordinary estimator noise so it does not cry wolf: the observed
# five-hour to weekly agreement on real data was within 2%.
DIVERGENCE_THRESHOLD = 0.35


@dataclass(frozen=True)
class WindowRatio:
    provider: str
    account_id: str | None
    short_window: str
    long_window: str
    short_quota_percent: float
    long_quota_percent: float
    ratio: float          # long-window points consumed per short-window point

    def to_long_equivalent(self, short_estimate_usd: float) -> float:
        """Value of a full long window, implied by a short window's rate."""
        return short_estimate_usd / self.ratio


@dataclass(frozen=True)
class Divergence:
    provider: str
    scope: str            # "window" | "account"
    subject: str
    expected_usd: float
    observed_usd: float
    difference: float     # signed, relative to expected
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _advance(points: Iterable[Mapping[str, Any]]) -> float:
    """Total forward quota movement, ignoring the resets between windows."""
    total = 0.0
    ordered = sorted(points, key=lambda row: str(row["observed_at"]))
    for left, right in zip(ordered, ordered[1:]):
        delta = float(right["used_percent"]) - float(left["used_percent"])
        if delta > 0:
            total += delta
    return total


def window_ratios(points: Iterable[Mapping[str, Any]]) -> list[WindowRatio]:
    """How much of each long window a short window consumes, per provider and account.

    Measured over the period both windows were observed. Comparing totals from different
    periods would divide one workload's quota by another's.
    """
    grouped: dict[tuple[str, str | None], dict[str, list[Mapping[str, Any]]]] = {}
    for row in points:
        account_id = row.get("account_id")
        account_id = account_id if isinstance(account_id, str) else None
        key = (str(row["provider"]), account_id)
        grouped.setdefault(key, {}).setdefault(str(row["window"]), []).append(row)

    ratios: list[WindowRatio] = []
    for (provider, account_id), windows in grouped.items():
        known = {name: rows for name, rows in windows.items() if name in WINDOW_MINUTES}
        for short, short_rows in known.items():
            for long, long_rows in known.items():
                if WINDOW_MINUTES[short] >= WINDOW_MINUTES[long]:
                    continue
                start = max(min(str(r["observed_at"]) for r in short_rows),
                            min(str(r["observed_at"]) for r in long_rows))
                end = min(max(str(r["observed_at"]) for r in short_rows),
                          max(str(r["observed_at"]) for r in long_rows))
                if start >= end:
                    continue
                overlap = lambda rows: [r for r in rows if start <= str(r["observed_at"]) <= end]
                short_quota = _advance(overlap(short_rows))
                long_quota = _advance(overlap(long_rows))
                if short_quota < MIN_RATIO_QUOTA_PERCENT or long_quota < MIN_RATIO_QUOTA_PERCENT:
                    continue
                ratios.append(WindowRatio(
                    provider=provider,
                    account_id=account_id,
                    short_window=short,
                    long_window=long,
                    short_quota_percent=short_quota,
                    long_quota_percent=long_quota,
                    ratio=long_quota / short_quota,
                ))
    return ratios


def converted_estimates(
    estimates: Iterable[RegressionEstimate], ratios: Iterable[WindowRatio]
) -> list[RegressionEstimate]:
    """Short-window estimates expressed as long-window equivalents.

    The result is deliberately a RegressionEstimate: everything downstream -- confidence
    tiers, rolling values, regime detection -- then treats a converted measurement exactly
    like a directly measured one, because that is what it is.
    """
    index = {(r.provider, r.account_id, r.short_window): r for r in ratios}
    converted: list[RegressionEstimate] = []
    for estimate in estimates:
        ratio = index.get((estimate.provider, estimate.account_id, estimate.window))
        if ratio is None or estimate.estimate_usd <= 0:
            continue
        scale = 1.0 / ratio.ratio
        converted.append(RegressionEstimate(
            provider=estimate.provider,
            window=ratio.long_window,
            reset_key=f"{estimate.reset_key}~via~{estimate.window}",
            estimate_usd=estimate.estimate_usd * scale,
            lower_usd=estimate.lower_usd * scale,
            upper_usd=estimate.upper_usd * scale,
            observation_count=estimate.observation_count,
            slope_count=estimate.slope_count,
            # The quota this stands for is the short window's movement expressed in long
            # window points, which is what it actually evidences.
            quota_span_percent=estimate.quota_span_percent * ratio.ratio,
            api_value_span_usd=estimate.api_value_span_usd,
            latest_observed_at=estimate.latest_observed_at,
            account_id=estimate.account_id,
            marginal_usd=estimate.marginal_usd * scale if estimate.marginal_usd else None,
            marginal_slope_count=estimate.marginal_slope_count,
            marginal_span_percent=estimate.marginal_span_percent * ratio.ratio,
            covered_quota_percent=estimate.covered_quota_percent * ratio.ratio,
            unobserved_quota_percent=estimate.unobserved_quota_percent * ratio.ratio,
        ))
    return converted


def _pool(estimates: list[RegressionEstimate]) -> float:
    values = [e.estimate_usd for e in estimates]
    weights = [max(e.covered_quota_percent, 0.0) or e.quota_span_percent for e in estimates]
    return weighted_quantile(values, weights, 0.5)


def divergences(
    estimates: Iterable[RegressionEstimate],
    ratios: Iterable[WindowRatio],
    plans: Mapping[str | None, str | None] | None = None,
    *,
    threshold: float = DIVERGENCE_THRESHOLD,
) -> list[Divergence]:
    """Independent measurements of one entitlement that no longer agree.

    A divergence is not by itself a fault. It says the two things that should describe one
    quantity do not, which is either a wrong assumption here or a change on the provider's
    side -- and only ever visible by measuring the same thing two ways.
    """
    rows = list(estimates)
    found: list[Divergence] = []

    # Window types: a short window converted into long-window terms against the long
    # window measured directly.
    converted = converted_estimates(rows, ratios)
    for ratio in ratios:
        via = [e for e in converted if e.provider == ratio.provider
               and e.account_id == ratio.account_id and e.window == ratio.long_window]
        direct = [e for e in rows if e.provider == ratio.provider
                  and e.account_id == ratio.account_id and e.window == ratio.long_window]
        if not via or not direct:
            continue
        expected, observed = _pool(via), _pool(direct)
        if expected <= 0:
            continue
        difference = (observed - expected) / expected
        if abs(difference) >= threshold:
            found.append(Divergence(
                provider=ratio.provider,
                scope="window",
                subject=f"{ratio.short_window} vs {ratio.long_window}",
                expected_usd=expected,
                observed_usd=observed,
                difference=difference,
                detail=(
                    f"{ratio.short_window} implies US${expected:.2f} per {ratio.long_window} "
                    f"entitlement, measured directly it is US${observed:.2f}"
                ),
            ))

    # Accounts on the same plan measure the same product.
    if plans:
        by_plan: dict[tuple[str, str, str], dict[str | None, list[RegressionEstimate]]] = {}
        for estimate in rows:
            plan = plans.get(estimate.account_id)
            if not plan:
                continue
            key = (estimate.provider, plan, estimate.window)
            by_plan.setdefault(key, {}).setdefault(estimate.account_id, []).append(estimate)
        for (provider, plan, window), accounts in by_plan.items():
            if len(accounts) < 2:
                continue
            pooled = {account: _pool(items) for account, items in accounts.items()}
            low = min(pooled, key=lambda a: pooled[a])
            high = max(pooled, key=lambda a: pooled[a])
            if pooled[low] <= 0:
                continue
            difference = (pooled[high] - pooled[low]) / pooled[low]
            if difference >= threshold:
                found.append(Divergence(
                    provider=provider,
                    scope="account",
                    subject=f"{plan} {window}",
                    expected_usd=pooled[low],
                    observed_usd=pooled[high],
                    difference=difference,
                    detail=(
                        f"two {plan} accounts measure the same {window} entitlement at "
                        f"US${pooled[low]:.2f} and US${pooled[high]:.2f}"
                    ),
                ))
    return found


def account_plans(db) -> dict[str | None, str | None]:
    """Plan per account, as the provider reported it alongside the meter."""
    return {
        row["account_id"]: row["plan"]
        for row in db.execute("SELECT account_id, plan FROM accounts WHERE plan IS NOT NULL")
    }


def combined_estimates(
    points: Iterable[Mapping[str, Any]], estimates: Iterable[RegressionEstimate]
) -> tuple[list[RegressionEstimate], list[WindowRatio]]:
    """Direct estimates plus every short window expressed in long-window terms.

    The two share a numerator -- the same recorded spend -- while having independent
    denominators, one meter each. So this buys robustness against a single meter
    misbehaving, and more observation periods, rather than genuinely independent evidence.
    Converted estimates carry their evidence scaled into long-window points, so they are
    weighted for what they actually show and cannot swamp the direct measurement.
    """
    rows = list(estimates)
    ratios = window_ratios(points)
    return rows + converted_estimates(rows, ratios), ratios
