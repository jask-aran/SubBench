from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def robust_estimates(points: Iterable[Mapping[str, Any]]) -> list[RegressionEstimate]:
    groups: dict[tuple[str, str | None, str, str], list[dict[str, Any]]] = {}
    for source_point in points:
        # sqlite3.Row implements the mapping protocol but does not provide .get().
        # Normalising once also gives the rest of the estimator a stable input type.
        point = dict(source_point)
        reset_key = _reset_key(point.get("resets_at"))
        account_id = point.get("account_id")
        account_id = account_id if isinstance(account_id, str) else None
        key = (str(point["provider"]), account_id, str(point["window"]), reset_key)
        groups.setdefault(key, []).append(point)

    estimates: list[RegressionEstimate] = []
    for (provider, account_id, window, reset_key), rows in groups.items():
        ordered = sorted(rows, key=lambda row: str(row["observed_at"]))
        slopes: list[float] = []
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                quota_delta = float(right["used_percent"]) - float(left["used_percent"])
                value_delta = float(right["cost_usd"]) - float(left["cost_usd"])
                if quota_delta <= 0 or value_delta < 0:
                    continue
                slopes.append(value_delta / (quota_delta / 100.0))

        if not slopes:
            continue

        quota_values = [float(row["used_percent"]) for row in ordered]
        cost_values = [float(row["cost_usd"]) for row in ordered]
        estimates.append(
            RegressionEstimate(
                provider=provider,
                window=window,
                reset_key=reset_key,
                estimate_usd=median(slopes),
                lower_usd=_quantile(slopes, 0.10),
                upper_usd=_quantile(slopes, 0.90),
                observation_count=len(ordered),
                slope_count=len(slopes),
                quota_span_percent=max(quota_values) - min(quota_values),
                api_value_span_usd=max(cost_values) - min(cost_values),
                latest_observed_at=str(ordered[-1]["observed_at"]),
                account_id=account_id,
            )
        )

    return sorted(estimates, key=lambda row: row.latest_observed_at, reverse=True)


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
    return timestamp.replace(second=0, microsecond=0).isoformat()
