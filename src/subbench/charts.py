from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping

import plotext as plt


def render_value_history(
    rows: Iterable[Mapping[str, Any]],
    *,
    provider: str | None = None,
    account_id: str | None = None,
    window: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> bool:
    selected = [
        row for row in rows
        if (provider is None or row["provider"] == provider)
        and (account_id is None or row.get("account_id") == account_id)
        and (window is None or row["window"] == window)
    ]
    if not selected:
        return False

    grouped: dict[tuple[str, str, str | None], list[Mapping[str, Any]]] = defaultdict(list)
    for row in selected:
        grouped[(str(row["provider"]), str(row["window"]), _key_account(row))].append(row)

    plt.clear_figure()
    if width or height:
        plt.plotsize(width or 100, height or 28)
    plt.title("SubBench API-equivalent entitlement value")
    plt.xlabel("reset window")
    plt.ylabel("US$ per full entitlement")

    all_labels: list[str] = []
    series: list[tuple[str, list[str], list[float]]] = []
    for (row_provider, row_window, row_account), group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda row: str(row.get("latest_observed_at") or row["reset_key"]))
        labels = [_date_label(str(row.get("latest_observed_at") or row["reset_key"])) for row in ordered]
        values = [float(row["estimate_usd"]) for row in ordered]
        all_labels.extend(labels)
        name = f"{row_provider}:{row_window}" + (f"@{row_account[:8]}" if row_account else "")
        series.append((name, labels, values))

    labels = list(dict.fromkeys(all_labels))
    positions = {label: index + 1 for index, label in enumerate(labels)}
    for name, dates, values in series:
        plt.plot([positions[date] for date in dates], values, marker="dot", label=name)

    tick_step = max(1, len(labels) // 8)
    ticks = list(range(1, len(labels) + 1, tick_step))
    plt.xticks(ticks, [labels[index - 1] for index in ticks])
    plt.show()
    return True


def _date_label(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10]


def _key_account(row: Mapping[str, Any]) -> str | None:
    account_id = row.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    return None
