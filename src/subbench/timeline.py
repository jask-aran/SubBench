"""How an estimate has moved over time, and when it stepped.

Two different series answer two different questions, and conflating them is the mistake
worth avoiding.

**Settled windows** — one point per reset window, each an estimate that is finished
changing. Converted to long-window terms, this is the series that detects a provider
changing an allowance: a step here is a real change in what the subscription delivers.
Claude's five-hour windows turn over roughly 34 times a week, so its *weekly* value can be
tracked at five-hour resolution. Codex, exposing only a weekly window, gets one point per
week per account.

Both window lengths are emitted, because they answer different questions and a plan
comparison needs both: the weekly allowance bounds the total, the five-hour one bounds the
burst. A generous weekly figure is not usable value if the short window throttles the rate
at which it can be spent.

Conversion only ever runs short window into long. A short window consumes a measured share
of the long one, so a short-window rate implies a long-window total. The reverse has no
physical basis -- a weekly rate does not evidence a five-hour *cap*, and inventing one
would fabricate exactly the restriction a plan comparison is trying to measure. A provider
with no five-hour meter therefore has no five-hour point until it exposes one.

**Replay** — what today's estimator would have said as of each moment inside a window.
This shows convergence, not value change, and it exists because estimates are derived and
never stored: a stored snapshot would be frozen at whatever estimator produced it, so a
later fix to the estimator would read as a change in the plan. Replaying from retained
evidence keeps the whole series self-consistent with one estimator.

That distinction is why every point carries `estimator_version`. A step in the line has two
possible causes -- the provider changed something, or we did -- and a change-detector that
cannot tell them apart will eventually make a confident wrong claim.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Iterable, Mapping

from . import crosssolve, regression
from .crosssolve import WindowRatio, window_ratios
from .regression import robust_estimates

# Every constant that changes what a plotted value is. Named rather than imported
# individually so the version hash below cannot silently omit one: a constant added to
# either module without being listed here fails the test that compares this list against
# the modules' actual decision constants.
VERSIONED_CONSTANTS: tuple[tuple[Any, str], ...] = (
    (regression, "MIN_QUOTA_DELTA_PERCENT"),
    (regression, "MIN_VALUE_PER_QUOTA_POINT"),
    (regression, "UNOBSERVED_QUOTA_POINTS"),
    (regression, "UNOBSERVED_RATE_FRACTION"),
    (regression, "UNOBSERVED_RATE_MIN_QUOTA"),
    (regression, "UNOBSERVED_MIN_MINUTES"),
    (regression, "MARGINAL_QUOTA_SPAN_PERCENT"),
    (regression, "RESET_CLUSTER_MINUTES"),
    (regression, "MAX_COST_AGE_MINUTES"),
    # The weekly series is partly built from converted short windows, so the threshold
    # that decides when a conversion is trustworthy moves the plotted value too.
    (crosssolve, "MIN_RATIO_QUOTA_PERCENT"),
)

# Estimator constants that deliberately do not version the series, each because it
# changes what is *reported alongside* a value rather than the value itself. Listed
# explicitly so the exemption is a decision rather than an omission.
UNVERSIONED_CONSTANTS: tuple[tuple[Any, str], ...] = (
    # Only sets when a disagreement between two measurements is worth surfacing.
    (crosssolve, "DIVERGENCE_THRESHOLD"),
)

# Windows a settled series is emitted for, longest first.
TARGET_WINDOWS = ("weekly", "five_hour")

# Replaying every observation is quadratic in pairs and cubic overall. The shape of a
# convergence curve survives sampling; its cost does not.
MAX_REPLAY_SAMPLES = 150

# A minimum of evidence before a replayed point is worth plotting at all.
MIN_REPLAY_QUOTA_PERCENT = 2.0


def estimator_version() -> str:
    """Identifies the estimator that produced a value.

    Derived from the constants that decide what the estimator does, so it cannot be
    forgotten when a threshold moves -- the failure mode a hand-maintained version string
    has. Two points carrying different versions are not comparable, and a step between
    them says nothing about the provider.
    """
    material = repr([(name, getattr(module, name)) for module, name in VERSIONED_CONSTANTS])
    return sha256(material.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class TimelinePoint:
    provider: str
    account_id: str | None
    window: str            # the window this value is expressed in
    source_window: str     # the window it was measured in, before conversion
    at: str                # when the evidence for this point ends
    estimate_usd: float
    lower_usd: float
    upper_usd: float
    marginal_usd: float | None
    covered_quota_percent: float
    coverage_percent: float
    slope_count: int
    scale: float           # conversion applied to reach `window`
    estimator_version: str
    # The window this measures has passed its reset, so the value cannot move again. The
    # open window is kept rather than dropped -- it is the live number -- but a step at an
    # unsettled point is convergence, not a provider changing anything.
    settled: bool = True
    resets_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ratio_for(ratios: Iterable[WindowRatio], provider: str, account_id: str | None, window: str):
    for ratio in ratios:
        if ratio.provider == provider and ratio.account_id == account_id and ratio.short_window == window:
            return ratio
    return None


def _has_passed(reset_key: str, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(reset_key.replace("Z", "+00:00")) <= now
    except ValueError:
        return False


def settled_timeline(
    points: Iterable[Mapping[str, Any]],
    *,
    target_window: str = "weekly",
    now: datetime | None = None,
) -> list[TimelinePoint]:
    """One point per reset window, expressed in `target_window` terms.

    This is the change-detection series. Each point is placed at the moment its evidence
    ends, so a step between settled points is a change in what the entitlement delivered.
    The open window is marked `settled=False`: it is still accumulating, and reading a step
    into it would mistake convergence for a provider change.

    A short window is converted up into `target_window`; a long one is never converted
    down. `window_ratios()` only ever pairs shorter into longer, so the conversion lookup
    below cannot find a long-to-short ratio -- and if it ever could, the result would be an
    invented cap rather than a measured one.
    """
    rows = list(points)
    ratios = window_ratios(rows)
    version = estimator_version()
    moment = now or datetime.now(timezone.utc)

    timeline: list[TimelinePoint] = []
    for estimate in robust_estimates(rows):
        if estimate.estimate_usd <= 0:
            continue
        scale = 1.0
        if estimate.window != target_window:
            ratio = _ratio_for(ratios, estimate.provider, estimate.account_id, estimate.window)
            if ratio is None or ratio.long_window != target_window:
                continue  # no measured conversion: showing it here would be an assumption
            scale = 1.0 / ratio.ratio
        timeline.append(TimelinePoint(
            provider=estimate.provider,
            account_id=estimate.account_id,
            window=target_window,
            source_window=estimate.window,
            at=estimate.latest_observed_at,
            estimate_usd=estimate.estimate_usd * scale,
            lower_usd=estimate.lower_usd * scale,
            upper_usd=estimate.upper_usd * scale,
            marginal_usd=estimate.marginal_usd * scale if estimate.marginal_usd else None,
            covered_quota_percent=estimate.covered_quota_percent,
            coverage_percent=estimate.coverage_percent,
            slope_count=estimate.slope_count,
            scale=scale,
            estimator_version=version,
            settled=_has_passed(estimate.reset_key, moment),
            resets_at=estimate.reset_key,
        ))
    return sorted(timeline, key=lambda row: (row.provider, str(row.account_id or ""), row.at))


def _series_key(row: Mapping[str, Any]) -> tuple[str, str | None, str]:
    account_id = row.get("account_id")
    return (str(row["provider"]), account_id if isinstance(account_id, str) else None, str(row["window"]))


def replay_series(
    points: Iterable[Mapping[str, Any]], *, max_samples: int = MAX_REPLAY_SAMPLES
) -> list[TimelinePoint]:
    """What today's estimator would have said, as of each moment.

    Recomputed from retained evidence rather than read from stored snapshots, so the whole
    curve reflects one estimator. A stored series would mix versions and a later fix would
    read as the plan changing.
    """
    grouped: dict[tuple[str, str | None, str], list[Mapping[str, Any]]] = {}
    for row in points:
        grouped.setdefault(_series_key(row), []).append(row)

    version = estimator_version()
    replayed: list[TimelinePoint] = []
    for (provider, account_id, window), rows in grouped.items():
        ordered = sorted(rows, key=lambda row: str(row["observed_at"]))
        if len(ordered) < 2:
            continue
        # Sample evenly, always keeping the final observation so the curve ends on the
        # value the rest of the page reports.
        step = max(1, len(ordered) // max_samples)
        indices = list(range(1, len(ordered), step))
        if indices[-1] != len(ordered) - 1:
            indices.append(len(ordered) - 1)

        for index in indices:
            prefix = ordered[: index + 1]
            estimates = [
                e for e in robust_estimates(prefix)
                if e.provider == provider and e.account_id == account_id and e.window == window
            ]
            if not estimates:
                continue
            # The newest window is the one still being measured.
            estimate = max(estimates, key=lambda e: e.latest_observed_at)
            if estimate.estimate_usd <= 0 or estimate.covered_quota_percent < MIN_REPLAY_QUOTA_PERCENT:
                continue
            replayed.append(TimelinePoint(
                provider=provider,
                account_id=account_id,
                window=window,
                source_window=window,
                at=str(prefix[-1]["observed_at"]),
                estimate_usd=estimate.estimate_usd,
                lower_usd=estimate.lower_usd,
                upper_usd=estimate.upper_usd,
                marginal_usd=estimate.marginal_usd,
                covered_quota_percent=estimate.covered_quota_percent,
                coverage_percent=estimate.coverage_percent,
                slope_count=estimate.slope_count,
                scale=1.0,
                estimator_version=version,
            ))
    return sorted(replayed, key=lambda row: (row.provider, str(row.account_id or ""), row.window, row.at))


def build_series(
    points: Iterable[Mapping[str, Any]], *, replay: bool = True, now: datetime | None = None
) -> dict[str, Any]:
    """Both window lengths in one flat list, distinguished by `window`.

    Flat rather than keyed by window so a provider exposing a third meter needs no schema
    change on either side of the push.
    """
    rows = list(points)
    settled: list[dict[str, Any]] = []
    for target in TARGET_WINDOWS:
        settled.extend(row.as_dict() for row in settled_timeline(rows, target_window=target, now=now))
    payload: dict[str, Any] = {
        "estimator_version": estimator_version(),
        "settled": settled,
        # How many full short windows fit in a long one. This is what says whether a
        # generous weekly allowance is actually reachable: at 9 five-hour windows per
        # week, only about a quarter of the week can be spent at the burst rate.
        "ratios": [asdict(ratio) for ratio in window_ratios(rows)],
    }
    if replay:
        payload["replay"] = [row.as_dict() for row in replay_series(rows)]
    return payload
