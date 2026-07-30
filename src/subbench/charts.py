from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Iterable, Mapping

import plotext as plt

from .regression import (
    MIN_QUOTA_DELTA_PERCENT,
    EstimateProgress,
    SlopeContribution,
    estimate_progress,
    pairwise_slopes,
)


DEFAULT_WIDTH = 78
DEFAULT_HEIGHT = 16


def render_value_history(
    rows: Iterable[Mapping[str, Any]],
    *,
    points: Iterable[Mapping[str, Any]] = (),
    min_pair_delta: float = MIN_QUOTA_DELTA_PERCENT,
    provider: str | None = None,
    account_id: str | None = None,
    window: str | None = None,
    width: int | None = None,
    height: int | None = None,
    show_slopes: bool = False,
) -> bool:
    point_rows = list(points)
    selected = [
        row for row in rows
        if (provider is None or row["provider"] == provider)
        and (account_id is None or row.get("account_id") == account_id)
        and (window is None or row["window"] == window)
    ]
    if not selected:
        return False

    selected_keys = {
        _series_key(row)
        for row in selected
    }
    progress = [
        row for row in estimate_progress(point_rows, min_quota_delta=min_pair_delta)
        if _series_key(row) in selected_keys
    ]
    latest_reset = _latest_reset_by_series(selected)
    current = [
        row for row in progress
        if latest_reset.get(_series_key(row)) == row.reset_key
    ]

    plt.clear_figure()
    plt.theme("clear")
    plt.plotsize(width or DEFAULT_WIDTH, height or DEFAULT_HEIGHT)
    plt.title("Current reset-period estimate")
    plt.xlabel("observations received")
    plt.ylabel("US$ per full entitlement")

    if current:
        grouped: dict[tuple[str, str, str | None, str], list[EstimateProgress]] = defaultdict(list)
        for row in current:
            grouped[_series_key(row)].append(row)
        # Position on the exact timestamp, not its minute-resolution label: two
        # observations inside one minute are distinct points and must not collapse.
        moments = sorted({row.observed_at for row in current})
        positions = {moment: index + 1 for index, moment in enumerate(moments)}
        for key, group in sorted(grouped.items()):
            ordered = sorted(group, key=lambda row: row.observed_at)
            name = _series_name(key)
            x = [positions[row.observed_at] for row in ordered]
            plt.plot(x, [row.estimate_usd for row in ordered], marker="dot", label=name)
        _apply_time_ticks(moments, _time_label)
    else:
        _plot_history(selected)

    plt.show()
    if show_slopes:
        _print_current_slopes(point_rows, selected, latest_reset, min_pair_delta=min_pair_delta)
    return True


def _plot_history(rows: list[Mapping[str, Any]]) -> None:
    grouped: dict[tuple[str, str, str | None], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["provider"]), str(row["window"]), _key_account(row))].append(row)

    # Every series shares one x axis keyed on the reset moment, so a point always sits
    # under its own date. Plotting each series from x=1 would slide the shorter ones.
    def moment_of(row: Mapping[str, Any]) -> str:
        return str(row.get("latest_observed_at") or row["reset_key"])

    moments = sorted({moment_of(row) for group in grouped.values() for row in group})
    positions = {moment: index + 1 for index, moment in enumerate(moments)}
    for key, group in sorted(grouped.items()):
        ordered = sorted(group, key=moment_of)
        plt.plot(
            [positions[moment_of(row)] for row in ordered],
            [float(row["estimate_usd"]) for row in ordered],
            marker="dot",
            label=_series_name(key),
        )
    _apply_time_ticks(moments, _date_label)


def _apply_time_ticks(moments: list[str], label: Any) -> None:
    if not moments:
        return
    tick_step = max(1, len(moments) // 6)
    ticks = list(range(1, len(moments) + 1, tick_step))
    plt.xticks(ticks, [label(moments[index - 1]) for index in ticks])


def _print_current_slopes(
    points: Iterable[Mapping[str, Any]],
    selected: list[Mapping[str, Any]],
    latest_reset: dict[tuple[str, str, str | None], str],
    *,
    min_pair_delta: float = MIN_QUOTA_DELTA_PERCENT,
) -> None:
    selected_keys = {
        (str(row["provider"]), str(row["window"]), _key_account(row), str(row["reset_key"]))
        for row in selected
    }
    contributions = [
        slope for slope in pairwise_slopes(points, min_quota_delta=min_pair_delta)
        if (*_series_key(slope), slope.reset_key) in selected_keys
        and latest_reset.get(_series_key(slope)) == slope.reset_key
    ]
    grouped: dict[tuple[str, str, str | None, str], list[SlopeContribution]] = defaultdict(list)
    for slope in contributions:
        grouped[(*_series_key(slope), slope.reset_key)].append(slope)

    print("\nCurrent-period slope contributions")
    for key, slopes in sorted(grouped.items()):
        slopes = sorted(slopes, key=lambda row: (row.right_observed_at, row.left_observed_at))
        print(f"{_series_name(key[:3])}  reset={key[3]}  slopes={len(slopes)}  median=US${median(row.slope_usd for row in slopes):.2f}")
        print("from           to             quota Δ   value Δ    slope")
        for slope in slopes:
            print(
                f"{_time_label(slope.left_observed_at):<14}"
                f"{_time_label(slope.right_observed_at):<14}"
                f"{slope.quota_delta_percent:>7.2f}%"
                f"  US${slope.api_value_delta_usd:>7.2f}"
                f"  US${slope.slope_usd:>8.2f}"
            )


def _latest_reset_by_series(rows: list[Mapping[str, Any]]) -> dict[tuple[str, str, str | None], str]:
    latest: dict[tuple[str, str, str | None], str] = {}
    for row in rows:
        key = (str(row["provider"]), str(row["window"]), _key_account(row))
        reset = str(row["reset_key"])
        if reset > latest.get(key, ""):
            latest[key] = reset
    return latest


def _series_key(row: Mapping[str, Any] | EstimateProgress | SlopeContribution) -> tuple[str, str, str | None]:
    return (str(row["provider"] if isinstance(row, Mapping) else row.provider),
            str(row["window"] if isinstance(row, Mapping) else row.window),
            _key_account(row))


def _series_name(key: tuple[str, str, str | None]) -> str:
    provider, window, account_id = key
    return f"{provider}:{window}" + (f"@{account_id[:8]}" if account_id else "")


def _date_label(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10]


def _time_label(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


def _key_account(row: Mapping[str, Any] | EstimateProgress | SlopeContribution) -> str | None:
    account_id = row.get("account_id") if isinstance(row, Mapping) else row.account_id
    if isinstance(account_id, str) and account_id:
        return account_id
    return None
