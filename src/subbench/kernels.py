"""The estimator's quadratic loops, vectorised, plus a cache of their results.

regression.py defines the estimator; this module only makes it fast. Every function here
reproduces the arithmetic of the loop it replaces in the same order (the same IEEE
operations on the same operands, the same stable ordering, sequential sums where the loop
summed sequentially), so the estimates are identical, not merely close. The tests compare
the two paths on generated histories.

A weekly window holds a couple of thousand readings, so several million ordered pairs. The
loops ran over those pairs in Python several times per window, and the report ran the
whole estimator again for every replay sample. Here each pass is a few numpy operations
per reading, and a window's pairs are computed once per content and reused.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

import numpy as np


def _moment(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Columns:
    used: np.ndarray
    cost: np.ndarray
    elapsed: np.ndarray  # minutes from the first dated reading; NaN where undated
    used_list: list[float]
    cost_list: list[float]


def columns(rows: list[Mapping[str, Any]]) -> Columns:
    used = [float(row["used_percent"]) for row in rows]
    cost = [float(row["cost_usd"]) for row in rows]
    moments = [_moment(row.get("observed_at")) for row in rows]
    anchor = next((moment for moment in moments if moment is not None), None)
    elapsed = [
        float("nan") if moment is None or anchor is None else (moment - anchor).total_seconds() / 60.0
        for moment in moments
    ]
    return Columns(np.array(used, dtype=np.float64), np.array(cost, dtype=np.float64),
                   np.array(elapsed, dtype=np.float64), used, cost)


def fingerprint(rows: list[Mapping[str, Any]]) -> str:
    """Identifies a window's evidence: the same readings give the same pairs.

    A digest rather than hash(), which Python salts per process: this one is stored, so a
    window estimated yesterday is recognised today.
    """
    digest = hashlib.blake2b(digest_size=16)
    for row in rows:
        digest.update(f"{row.get('observed_at')}|{float(row['used_percent'])!r}|{float(row['cost_usd'])!r}\n".encode())
    return digest.hexdigest()


def digest_of(parts: Iterable[str]) -> str:
    digest = hashlib.blake2b(digest_size=16)
    for part in parts:
        digest.update(part.encode() + b"\n")
    return digest.hexdigest()


def unobserved_flags(
    cols: Columns, *, min_value_per_quota_point: float, reference_rate: float | None,
    quota_points: float, rate_fraction: float, rate_min_quota: float, min_minutes: float,
    ceiling: float, unmetered_min_value: float,
) -> np.ndarray:
    """Which intervals some unobserved pair spans. Mirrors regression._unobserved_intervals."""
    used, cost, elapsed = cols.used, cols.cost, cols.elapsed
    count = len(used)
    flagged = np.zeros(max(count - 1, 0), dtype=bool)
    if count < 2:
        return flagged
    below = np.concatenate(([0], np.cumsum(used < ceiling)))
    # The furthest reading each start reaches through an unobserved pair; every interval
    # from the start up to it is flagged, exactly as the loop's slice assignment did.
    reach = np.full(count, -1, dtype=np.int64)
    dated = ~np.isnan(elapsed)
    for start in range(count - 1):
        ends = slice(start + 1, count)
        quota_delta = used[ends] - used[start]
        value_delta = cost[ends] - cost[start]
        unobserved = (quota_delta >= quota_points) & (value_delta < quota_delta * min_value_per_quota_point)
        if reference_rate:
            eligible = ~unobserved & (quota_delta >= rate_min_quota)
            if eligible.any():
                with np.errstate(divide="ignore", invalid="ignore"):
                    rate = value_delta / (quota_delta / 100.0)
                unobserved |= eligible & (rate < reference_rate * rate_fraction)
        if used[start] >= ceiling:
            unobserved |= (
                ~unobserved & (quota_delta <= 0) & (value_delta >= unmetered_min_value)
                & (used[ends] >= ceiling) & (below[start + 2:count + 1] == below[start])
            )
        if dated[start]:
            unobserved &= ~(dated[ends] & (elapsed[ends] - elapsed[start] < min_minutes))
        hits = np.flatnonzero(unobserved)
        if hits.size:
            reach[start] = start + 1 + int(hits[-1])
    # Interval i is flagged when a start at or before it reaches past it.
    furthest = np.maximum.accumulate(reach)[: count - 1]
    flagged = furthest > np.arange(count - 1)
    return flagged


@dataclass(frozen=True)
class Pairs:
    left: np.ndarray    # index of the earlier reading
    right: np.ndarray   # index of the later reading
    quota: np.ndarray   # quota delta, the pair's weight
    value: np.ndarray
    slope: np.ndarray

    def __len__(self) -> int:
        return int(self.slope.size)


EMPTY = Pairs(*(np.empty(0, dtype=t) for t in (np.int64, np.int64, np.float64, np.float64, np.float64)))


def valid_pairs(cols: Columns, unobserved_prefix: np.ndarray, *, floor: float) -> Pairs:
    """Every informative ordered pair, in the loop's row-major order. Mirrors regression._valid_pairs."""
    used, cost = cols.used, cols.cost
    count = len(used)
    parts: list[tuple[np.ndarray, ...]] = []
    for start in range(count - 1):
        ends = slice(start + 1, count)
        quota_delta = used[ends] - used[start]
        value_delta = cost[ends] - cost[start]
        keep = (
            (unobserved_prefix[start + 1:count] == unobserved_prefix[start])
            & ~(quota_delta < floor) & (quota_delta > 0) & (value_delta > 0)
        )
        hits = np.flatnonzero(keep)
        if hits.size:
            quota = quota_delta[hits]
            value = value_delta[hits]
            parts.append((np.full(hits.size, start, dtype=np.int64), hits + start + 1, quota, value, value / (quota / 100.0)))
    if not parts:
        return EMPTY
    return Pairs(*(np.concatenate([part[k] for part in parts]) for k in range(5)))


def weighted_quantile(values: np.ndarray, weights: np.ndarray, probability: float) -> float:
    """regression.weighted_quantile for arrays: a stable sort, the same summation order."""
    order = np.argsort(values, kind="stable")
    ordered_values = values[order]
    ordered_weights = weights[order]
    # Python's sum() of the sorted weights, as the original computes the total.
    total = sum(ordered_weights.tolist())
    if total <= 0:
        from statistics import median

        return median(values.tolist())
    threshold = total * probability
    cumulative = np.cumsum(ordered_weights)
    position = int(np.searchsorted(cumulative, threshold, side="left"))
    if position >= cumulative.size:
        return float(ordered_values[-1])
    return float(ordered_values[position])


class ByteBudgetCache:
    """An LRU cache bounded by the bytes of the arrays it holds."""

    def __init__(self, budget: int = 512 * 1024 * 1024) -> None:
        self.budget = budget
        self.size = 0
        self.items: OrderedDict[Any, tuple[Any, int]] = OrderedDict()

    def get(self, key: Any) -> Any:
        item = self.items.get(key)
        if item is None:
            return None
        self.items.move_to_end(key)
        return item[0]

    def put(self, key: Any, value: Any, nbytes: int) -> Any:
        if key in self.items:
            self.size -= self.items.pop(key)[1]
        self.items[key] = (value, nbytes)
        self.size += nbytes
        while self.size > self.budget and len(self.items) > 1:
            _, (_, freed) = self.items.popitem(last=False)
            self.size -= freed
        return value
