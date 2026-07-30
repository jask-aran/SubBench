"""How much an estimate should be trusted, and why.

Every estimate is displayed; the tier sets how prominently. Suppressing weak estimates
would leave a blank page for the first hours of collection and would hide the series that
most needs explaining -- a window can carry thousands of slopes and still rest on half the
quota it appears to, which is a fact a label conveys and a blank space does not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..regression import RegressionEstimate

# A window must have measured real quota movement, most of the movement it saw, and
# enough pairs that the weighted median is not resting on a handful of them.
MIN_COVERED_QUOTA_PERCENT = 25.0
MIN_COVERAGE_PERCENT = 70.0
MIN_SLOPE_COUNT = 50

# Relative width of the 10th-90th percentile slope band, as a fraction of the estimate.
# Set to 0.7 rather than 1.0 deliberately: at 1.0 every series observed so far passes
# (0.77, 0.61, 0.53), so the rule would be decorative -- present but never deciding
# anything. At 0.7 it filters estimates that reached `likely` but remain unstable.
MAX_RELATIVE_BAND_WIDTH = 0.7

# Two windows of the same account are independent measurements of one entitlement. When
# a short window's rate predicts the long window's to within this margin, that is
# evidence no single series can provide, because it catches systematic error rather than
# noise. Sufficient for `confirmed`, never necessary -- providers exposing one window
# must be able to reach the top tier too.
MAX_CORROBORATION_DIFFERENCE = 0.15

CONFIRMED = "confirmed"
LIKELY = "likely"
PROVISIONAL = "provisional"


@dataclass(frozen=True)
class Confidence:
    tier: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "reason": self.reason}


def relative_band_width(estimate: RegressionEstimate) -> float | None:
    if estimate.estimate_usd <= 0:
        return None
    return (estimate.upper_usd - estimate.lower_usd) / estimate.estimate_usd


def corroboration(
    estimate: RegressionEstimate, peers: Iterable[RegressionEstimate]
) -> tuple[float, RegressionEstimate] | None:
    """Closest agreement between this window and another window of the same account.

    Two windows of one account advance together: consuming a five-hour allowance also
    consumes part of the weekly one. The ratio of their observed quota movement converts
    a rate measured on one into a prediction for the other, and the two were measured
    independently, so agreement is meaningful.
    """
    if estimate.quota_span_percent <= 0 or estimate.estimate_usd <= 0:
        return None
    best: tuple[float, RegressionEstimate] | None = None
    for peer in peers:
        if peer.window == estimate.window or peer.provider != estimate.provider:
            continue
        if peer.account_id != estimate.account_id:
            continue
        if peer.quota_span_percent <= 0 or peer.estimate_usd <= 0:
            continue
        # Fraction of this window consumed while the peer consumed all of its own.
        share = peer.quota_span_percent / estimate.quota_span_percent
        if share <= 0:
            continue
        predicted = peer.estimate_usd / share
        difference = abs(predicted - estimate.estimate_usd) / estimate.estimate_usd
        if best is None or difference < best[0]:
            best = (difference, peer)
    return best


def classify(
    estimate: RegressionEstimate, peers: Iterable[RegressionEstimate] = ()
) -> Confidence:
    covered = estimate.covered_quota_percent
    coverage = estimate.coverage_percent

    if covered < MIN_COVERED_QUOTA_PERCENT:
        return Confidence(
            PROVISIONAL,
            f"only {covered:.0f} points of quota measured, need {MIN_COVERED_QUOTA_PERCENT:.0f}",
        )
    if coverage < MIN_COVERAGE_PERCENT:
        return Confidence(
            PROVISIONAL,
            f"{100 - coverage:.0f}% of quota movement had no recorded spend beside it",
        )
    if estimate.slope_count < MIN_SLOPE_COUNT:
        return Confidence(
            PROVISIONAL,
            f"{estimate.slope_count} valid pairs, need {MIN_SLOPE_COUNT}",
        )

    width = relative_band_width(estimate)
    if width is not None and width <= MAX_RELATIVE_BAND_WIDTH:
        return Confidence(CONFIRMED, f"slope band is {width:.0%} of the estimate")

    agreement = corroboration(estimate, peers)
    if agreement is not None and agreement[0] <= MAX_CORROBORATION_DIFFERENCE:
        difference, peer = agreement
        return Confidence(
            CONFIRMED,
            f"agrees with the {peer.window} window to {difference:.1%}",
        )

    detail = f"slope band is {width:.0%} of the estimate" if width is not None else "wide slope band"
    return Confidence(LIKELY, detail)


def annotate(estimates: Iterable[RegressionEstimate]) -> list[dict[str, Any]]:
    """Estimates as dicts, each carrying its tier and the reason for it."""
    rows = list(estimates)
    return [{**row.as_dict(), **classify(row, rows).as_dict()} for row in rows]


def best_tier(rows: Iterable[Mapping[str, Any]]) -> str:
    """Highest tier present, for summarising a group of windows."""
    order = {PROVISIONAL: 0, LIKELY: 1, CONFIRMED: 2}
    tiers = [str(row.get("tier", PROVISIONAL)) for row in rows]
    return max(tiers, key=lambda tier: order.get(tier, 0)) if tiers else PROVISIONAL
