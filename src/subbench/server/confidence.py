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
# enough independent readings that the estimate is not resting on a handful of them.
MIN_COVERED_QUOTA_PERCENT = 25.0
MIN_COVERAGE_PERCENT = 70.0

# Increments, not pairs. Every pair is a sum of consecutive increments, so n observations
# yield about n^2/2 pairs from n-1 independent readings: a pair count reads as far more
# evidence than it is, and it grows quadratically while the evidence grows linearly. The
# old rule asked for 50 pairs, which is about eleven observations; this asks for twelve
# readings directly.
MIN_INTERVAL_COUNT = 12

# Relative width of the bootstrap interval, as a fraction of the estimate. The interval is
# resampled from the window's independent increments, so unlike the old spread-of-pairs
# band it narrows only as real evidence arrives.
#
# Re-derived when the band changed meaning. The old 0.7 was set against a spread that
# narrowed as the square of the readings, so it was passed by almost anything given time.
# Against a real interval it is far too loose: replayed over a month of observations it
# confirmed a weekly limit at $885 whose interval ran from $498 to $1002, which is not a
# figure to headline. Half the estimate keeps every window whose value is settled to
# roughly plus or minus a quarter and drops the rest to a lower tier.
MAX_RELATIVE_BAND_WIDTH = 0.5

# Agreement between an account's short and long window is reported, never promoting.
#
# It once promoted a window to `confirmed` on its own, justified as catching systematic
# error. It cannot. Both windows divide the same recorded spend by their own meter, so a
# numerator that is wrong is wrong in both and the two still agree. What the comparison
# does test is that the two meters advance together, which is worth saying and is not
# evidence about the dollar figure.
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
    """Width of the bootstrap interval relative to the estimate, or None if unbounded."""
    if estimate.estimate_usd <= 0 or estimate.interval_count <= 0:
        return None
    if estimate.upper_usd <= 0 and estimate.lower_usd <= 0:
        return None
    return (estimate.upper_usd - estimate.lower_usd) / estimate.estimate_usd


def corroboration(
    estimate: RegressionEstimate, peers: Iterable[RegressionEstimate]
) -> tuple[float, RegressionEstimate] | None:
    """Closest agreement between this window and another window of the same account.

    Two windows of one account advance together: consuming a five-hour allowance also
    consumes part of the weekly one. The ratio of their observed quota movement converts
    a rate measured on one into a prediction for the other.

    The two are not independent measurements. They share a numerator, the same recorded
    spend, so agreement tells you the meters move together and says nothing about whether
    the spend behind them was recorded correctly. Reported, never promoting.
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
    if estimate.interval_count < MIN_INTERVAL_COUNT:
        return Confidence(
            PROVISIONAL,
            f"{estimate.interval_count} independent readings, need {MIN_INTERVAL_COUNT}",
        )

    agreement = corroboration(estimate, peers)
    note = ""
    if agreement is not None and agreement[0] <= MAX_CORROBORATION_DIFFERENCE:
        note = f"; meters agree with the {agreement[1].window} window to {agreement[0]:.1%}"

    width = relative_band_width(estimate)
    if width is None:
        return Confidence(PROVISIONAL, "not enough readings to bound the estimate" + note)
    if width <= MAX_RELATIVE_BAND_WIDTH:
        return Confidence(CONFIRMED, f"estimate is bounded to {width:.0%} of itself" + note)
    return Confidence(LIKELY, f"estimate is bounded only to {width:.0%} of itself" + note)


def annotate(estimates: Iterable[RegressionEstimate]) -> list[dict[str, Any]]:
    """Estimates as dicts, each carrying its tier and the reason for it."""
    rows = list(estimates)
    return [{**row.as_dict(), **classify(row, rows).as_dict()} for row in rows]


def best_tier(rows: Iterable[Mapping[str, Any]]) -> str:
    """Highest tier present, for summarising a group of windows."""
    order = {PROVISIONAL: 0, LIKELY: 1, CONFIRMED: 2}
    tiers = [str(row.get("tier", PROVISIONAL)) for row in rows]
    return max(tiers, key=lambda tier: order.get(tier, 0)) if tiers else PROVISIONAL
