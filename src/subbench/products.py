"""Product identity, and one estimate per product from every account that holds it.

A product is a provider and a plan together. The plan comes from the provider, beside the
meter, so SubBench never guesses it and never hardcodes it. A plan change makes a new
product: the entitlement changes size, so quota points before and after are different
quantities and their evidence must not be mixed.

Estimates are still measured inside one account, because each account has its own meter
and a pair of readings only makes sense within one of them. Two accounts on the same plan
hold the same entitlement, so their finished window estimates measure one quantity twice.
Combining them is what this module does, and it is the only place where evidence crosses
an account boundary.

Nothing here knows whose account an estimate came from. The question being answered is
what a product is worth, not what one subscriber measured, so pooling reads the plan and
ignores the source. The same functions therefore pool across many collectors unchanged if
estimates from more than one ever reach them; today a collector sees only its own machine.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .crosssolve import WINDOW_MINUTES
from .regression import weighted_quantile
from .server.confidence import CONFIRMED, LIKELY

# Display names for the provider half of a product. The plan half is never listed here;
# it arrives from the provider and is shown as reported.
PROVIDER_NAMES = {"codex": "ChatGPT", "claude": "Claude"}

# How far back a finished window may be and still describe what the subscription is worth
# now. Model prices and the model mix both move, so pooling all history would report an
# average of the past rather than the present. Five weekly cycles keeps several windows
# per account while staying inside one billing month.
POOL_DAYS = 35.0

# A trend point pools the windows that finished in one period. The period is a day for
# limits shorter than a day and a week for the rest, so each point holds several finished
# windows without flattening a month into one number.
MINUTES_IN_A_DAY = 1440

# Only the long window is projected while it is still running. A five-hour window turns
# over several times a day, so an open one is usually minutes old and says nothing that the
# finished ones beside it do not say better. A week is long enough that waiting for it to
# close means waiting days to see a change in what the subscription delivers.
OPEN_WINDOW = "weekly"


def product_label(provider: str, plan: str | None) -> str:
    """Human name for a product, for example "ChatGPT Plus" or "Claude"."""
    name = PROVIDER_NAMES.get(provider, str(provider).replace("_", " ").title())
    if not plan:
        return name
    detail = str(plan).replace("_", " ").title()
    # Some providers report the product name in full. Do not say it twice.
    if detail.lower().startswith(name.lower()):
        return detail
    return f"{name} {detail}"


@dataclass(frozen=True)
class ProductEstimate:
    provider: str
    plan: str | None
    product: str
    window: str
    estimate_usd: float
    lower_usd: float
    upper_usd: float
    window_count: int
    account_count: int
    latest_completed_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def direct_windows(
    estimates: Iterable[Any],
    *,
    now: datetime | None = None,
    within_days: float | None = POOL_DAYS,
    tiers: tuple[str, ...] = (CONFIRMED,),
    finished: bool = True,
) -> list[dict[str, Any]]:
    """Windows measured directly, at the given tiers, finished or still open.

    Converted windows carry "~via~" in their reset key. They restate a measurement that is
    already present, so pooling them would count the same evidence twice.

    A window held below `likely` never appears here whatever else is asked for. That is
    where a contaminated window lands -- one whose meter moved without local spend beside
    it -- and no amount of pooling repairs evidence that was never recorded.
    """
    moment = now or datetime.now(timezone.utc)
    floor = moment - timedelta(days=within_days) if within_days is not None else None
    rows: list[dict[str, Any]] = []
    for estimate in estimates:
        row = estimate if isinstance(estimate, dict) else estimate.as_dict()
        reset_key = str(row.get("reset_key") or "")
        if not reset_key or "~via~" in reset_key:
            continue
        if row.get("tier") not in tiers or float(row.get("estimate_usd") or 0.0) <= 0:
            continue
        resets_at = _moment(reset_key)
        if resets_at is None:
            continue
        if finished:
            if resets_at > moment:
                continue
            if floor is not None and resets_at < floor:
                continue
        elif resets_at <= moment:
            continue
        rows.append(row)
    return rows


def completed_direct(
    estimates: Iterable[Any],
    *,
    now: datetime | None = None,
    within_days: float | None = POOL_DAYS,
    tiers: tuple[str, ...] = (CONFIRMED,),
) -> list[dict[str, Any]]:
    """Finished windows measured directly, at the given tiers."""
    return direct_windows(estimates, now=now, within_days=within_days, tiers=tiers)


def _pool(rows: list[dict[str, Any]]) -> tuple[float, float, float, int, int]:
    """Combine finished windows into one figure, weighted by the quota each measured.

    A window that watched 80 points of the meter says more about the value of a full limit
    than one that watched 30, and a weighted median keeps a single odd window from moving
    the result.
    """
    values = [float(row["estimate_usd"]) for row in rows]
    # A window with no measured quota would otherwise weigh nothing and drop out.
    weights = [max(float(row.get("covered_quota_percent") or 0.0), 1e-9) for row in rows]
    accounts = {str(row.get("account_id")) for row in rows}
    # The bounds pool the windows' own bounds, not the spread between their estimates.
    # Spread is week-to-week variation in what the subscription delivered, which the chart
    # already shows; this range answers the different question of how well the figure
    # beside it is pinned down. For one window it is exactly that window's interval.
    return (
        weighted_quantile(values, weights, 0.5),
        # A row without bounds is a point with no interval, never a crash.
        weighted_quantile([float(row.get("lower_usd") or row["estimate_usd"]) for row in rows], weights, 0.5),
        weighted_quantile([float(row.get("upper_usd") or row["estimate_usd"]) for row in rows], weights, 0.5),
        len(rows),
        len(accounts),
    )


def _period_start(completed: datetime, window: str) -> datetime:
    day = completed.replace(hour=0, minute=0, second=0, microsecond=0)
    if WINDOW_MINUTES.get(window, MINUTES_IN_A_DAY) < MINUTES_IN_A_DAY:
        return day
    return day - timedelta(days=day.weekday())


@dataclass(frozen=True)
class ProductPoint:
    product: str
    provider: str
    plan: str | None
    window: str
    period_start: str
    estimate_usd: float
    window_count: int
    account_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def product_series(
    estimates: Iterable[Any],
    *,
    now: datetime | None = None,
) -> list[ProductPoint]:
    """What each product was worth over time, pooled per period across every account.

    One line per product rather than one per account. A chart that joins the windows of
    two accounts into a single line draws a trend nobody measured, and a chart that draws
    them separately answers what each subscriber saw rather than what the product is worth.
    Pooling first makes each point a measurement of the product.
    """
    grouped: dict[tuple[str, str | None, str, datetime], list[dict[str, Any]]] = {}
    for row in completed_direct(estimates, now=now, within_days=None):
        plan = row.get("plan")
        plan = plan if isinstance(plan, str) and plan else None
        completed = _moment(row["reset_key"])
        if completed is None:
            continue
        window = str(row["window"])
        key = (str(row["provider"]), plan, window, _period_start(completed, window))
        grouped.setdefault(key, []).append(row)

    points: list[ProductPoint] = []
    for (provider, plan, window, period), rows in grouped.items():
        estimate, _lower, _upper, windows, accounts = _pool(rows)
        points.append(ProductPoint(
            product=product_label(provider, plan),
            provider=provider,
            plan=plan,
            window=window,
            period_start=period.date().isoformat(),
            estimate_usd=estimate,
            window_count=windows,
            account_count=accounts,
        ))
    return sorted(points, key=lambda row: (row.product, row.window, row.period_start))


@dataclass(frozen=True)
class OpenEstimate:
    product: str
    provider: str
    plan: str | None
    window: str
    estimate_usd: float
    lower_usd: float
    upper_usd: float
    window_count: int
    account_count: int
    covered_quota_percent: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def open_estimates(
    estimates: Iterable[Any],
    *,
    now: datetime | None = None,
) -> list[OpenEstimate]:
    """What a product is delivering right now, from the windows still running.

    Accounts are not synchronised. One may have reset hours ago and another days ago, and a
    period of using one and not the other moves their boundaries apart for good. So this
    pools rates rather than windows: every open window contributes dollars per quota point
    scaled to a full limit, weighted by the quota it has measured so far, and where each
    account happens to sit in its own cycle never enters the arithmetic. The result is a
    synthetic current value for the product built from all the latest evidence, not a
    reading from whichever account happens to be furthest through its week.

    It is deliberately not treated as settled. Its purpose is timeliness: a provider that
    changes what it delivers shows up here as the figure moving away from the finished
    windows plotted beside it, days before any window closes to confirm it.
    """
    grouped: dict[tuple[str, str | None, str], list[dict[str, Any]]] = {}
    rows = direct_windows(
        estimates, now=now, within_days=None, tiers=(CONFIRMED, LIKELY), finished=False,
    )
    # One open window per account, the one observed most recently. An account has a single
    # current cycle, but a boundary that moves -- a reset applied, or a gap in use -- leaves
    # an older window whose reset has still not passed. Counting both would weigh one
    # account twice and mix a superseded cycle into the present one.
    latest: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    for row in rows:
        if str(row["window"]) != OPEN_WINDOW:
            continue
        plan = row.get("plan")
        plan = plan if isinstance(plan, str) and plan else None
        account = row.get("account_id")
        account = account if isinstance(account, str) else None
        key = (str(row["provider"]), plan, account)
        seen = latest.get(key)
        if seen is None or str(row["latest_observed_at"]) > str(seen["latest_observed_at"]):
            latest[key] = row

    for (provider, plan, _account), row in latest.items():
        grouped.setdefault((provider, plan, str(row["window"])), []).append(row)

    open_rows: list[OpenEstimate] = []
    for (provider, plan, window), members in grouped.items():
        estimate, lower, upper, windows, accounts = _pool(members)
        open_rows.append(OpenEstimate(
            provider=provider,
            plan=plan,
            product=product_label(provider, plan),
            window=window,
            estimate_usd=estimate,
            lower_usd=lower,
            upper_usd=upper,
            window_count=windows,
            account_count=accounts,
            # How far the best-measured contributor has watched its own limit.
            covered_quota_percent=max(
                float(row.get("covered_quota_percent") or 0.0) for row in members
            ),
        ))
    return sorted(open_rows, key=lambda row: (row.product, row.window))


def product_estimates(
    estimates: Iterable[Any],
    *,
    now: datetime | None = None,
    within_days: float | None = POOL_DAYS,
    tiers: tuple[str, ...] = (CONFIRMED,),
) -> list[ProductEstimate]:
    """One estimate per product and window, pooled over every account on that plan.

    Each finished window contributes its own estimate, weighted by the quota it actually
    measured. A window that watched 80 points of the meter says more about the value of a
    full limit than one that watched 30, and the weighted median keeps a single odd window
    from moving the result.
    """
    grouped: dict[tuple[str, str | None, str], list[dict[str, Any]]] = {}
    for row in completed_direct(estimates, now=now, within_days=within_days, tiers=tiers):
        plan = row.get("plan")
        plan = plan if isinstance(plan, str) and plan else None
        grouped.setdefault((str(row["provider"]), plan, str(row["window"])), []).append(row)

    pooled: list[ProductEstimate] = []
    for (provider, plan, window), rows in grouped.items():
        estimate, lower, upper, windows, accounts = _pool(rows)
        pooled.append(ProductEstimate(
            provider=provider,
            plan=plan,
            product=product_label(provider, plan),
            window=window,
            estimate_usd=estimate,
            lower_usd=lower,
            upper_usd=upper,
            window_count=windows,
            account_count=accounts,
            latest_completed_at=max(str(row["reset_key"]) for row in rows),
        ))
    return sorted(pooled, key=lambda row: (row.product, row.window))
