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
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .regression import weighted_quantile
from .server.confidence import CONFIRMED

# Display names for the provider half of a product. The plan half is never listed here;
# it arrives from the provider and is shown as reported.
PROVIDER_NAMES = {"codex": "ChatGPT", "claude": "Claude"}

# How far back a finished window may be and still describe what the subscription is worth
# now. Model prices and the model mix both move, so pooling all history would report an
# average of the past rather than the present. Five weekly cycles keeps several windows
# per account while staying inside one billing month.
POOL_DAYS = 35.0


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


def completed_direct(
    estimates: Iterable[Any],
    *,
    now: datetime | None = None,
    within_days: float | None = POOL_DAYS,
) -> list[dict[str, Any]]:
    """Finished windows that were measured directly and reached the top tier.

    Converted windows carry "~via~" in their reset key. They restate a measurement that is
    already present, so pooling them would count the same evidence twice.
    """
    moment = now or datetime.now(timezone.utc)
    floor = moment - timedelta(days=within_days) if within_days is not None else None
    rows: list[dict[str, Any]] = []
    for estimate in estimates:
        row = estimate if isinstance(estimate, dict) else estimate.as_dict()
        reset_key = str(row.get("reset_key") or "")
        if not reset_key or "~via~" in reset_key:
            continue
        if row.get("tier") != CONFIRMED or float(row.get("estimate_usd") or 0.0) <= 0:
            continue
        completed = _moment(reset_key)
        if completed is None or completed > moment:
            continue
        if floor is not None and completed < floor:
            continue
        rows.append(row)
    return rows


def product_estimates(
    estimates: Iterable[Any],
    *,
    now: datetime | None = None,
    within_days: float | None = POOL_DAYS,
) -> list[ProductEstimate]:
    """One estimate per product and window, pooled over every account on that plan.

    Each finished window contributes its own estimate, weighted by the quota it actually
    measured. A window that watched 80 points of the meter says more about the value of a
    full limit than one that watched 30, and the weighted median keeps a single odd window
    from moving the result.
    """
    grouped: dict[tuple[str, str | None, str], list[dict[str, Any]]] = {}
    for row in completed_direct(estimates, now=now, within_days=within_days):
        plan = row.get("plan")
        plan = plan if isinstance(plan, str) and plan else None
        grouped.setdefault((str(row["provider"]), plan, str(row["window"])), []).append(row)

    pooled: list[ProductEstimate] = []
    for (provider, plan, window), rows in grouped.items():
        values = [float(row["estimate_usd"]) for row in rows]
        # A window with no measured quota would otherwise weigh nothing and drop out.
        weights = [max(float(row.get("covered_quota_percent") or 0.0), 1e-9) for row in rows]
        accounts = {str(row.get("account_id")) for row in rows}
        pooled.append(ProductEstimate(
            provider=provider,
            plan=plan,
            product=product_label(provider, plan),
            window=window,
            estimate_usd=weighted_quantile(values, weights, 0.5),
            lower_usd=weighted_quantile(values, weights, 0.10),
            upper_usd=weighted_quantile(values, weights, 0.90),
            window_count=len(rows),
            account_count=len(accounts),
            latest_completed_at=max(str(row["reset_key"]) for row in rows),
        ))
    return sorted(pooled, key=lambda row: (row.product, row.window))
