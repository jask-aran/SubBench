"""Scoped limits: one model family's own sub-limit inside a weekly limit.

Claude meters Fable twice. Fable usage moves the weekly limit like any other model, and it
also moves a Fable meter that reads 0-100% of Fable's own allowance (50% of the weekly
limit). Fable also uses quota about 2.5 times as fast per API dollar as Opus. Measured
together, a week's value would follow the share of Fable in the mix and not the plan.

So each scoped limit is measured on its own, and taken out of the others:

- The scoped window ("weekly_fable") is priced with that family's spend only. Its estimate
  is the API value of one full Fable allowance.
- The weekly window loses the scoped meters' quota (meter percent times allowance share)
  and their spend. Its estimate is the value of the weekly limit on the other models. A
  scoped reset (the provider gives the scoped allowance back) leaves this series
  continuous, because the weekly meter and the scoped share drop by the same amount.
- The five-hour window has no scoped meter beside it, so the scoped quota inside it cannot
  be taken out. Its scoped spend is removed and marked as excluded; a pair across any
  excluded spend is rejected, as a pair across unrecorded usage is.

The same applies to a weekly reading that has no scoped reading beside it, for example
before SubBench recorded the scoped meter.

This module is pure: the store calls it on priced readings, and the estimator sees only its
output.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# A scoped window is "weekly_<scope>", for example weekly_fable.
SCOPED_WINDOW_PREFIX = "weekly_"

# The window that a scoped meter's usage also moves.
PARENT_WINDOW = "weekly"


def scope_slug(name: str) -> str:
    """The scope as model ids spell it: "Fable" becomes "fable"."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def scope_of(window: Any) -> str | None:
    text = str(window or "")
    if text.startswith(SCOPED_WINDOW_PREFIX) and len(text) > len(SCOPED_WINDOW_PREFIX):
        return text[len(SCOPED_WINDOW_PREFIX):]
    return None


def model_in_scope(model: str, scope: str) -> bool:
    """True if the model id names the scope as a whole word: claude-fable-5-2 is in "fable"."""
    return f"-{scope.replace('_', '-')}-" in f"-{model.lower()}-"


def separate(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Priced readings with each scoped limit taken out of the windows that contain it.

    Input rows carry `scoped_cost_usd` (the spend of every scoped family inside the
    reading's window) and, on scoped rows, `allowance_percent`. Output rows carry
    `excluded_cost_usd`: scoped spend whose quota could not be taken out of this reading.
    """
    rows = [dict(row) for row in rows]
    scoped_at: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = {}
    for row in rows:
        if scope_of(row.get("window")):
            scoped_at.setdefault((row["provider"], row.get("account_id"), row["observed_at"]), []).append(row)

    out: list[dict[str, Any]] = []
    for row in rows:
        if scope_of(row.get("window")):
            # Priced with its own family's spend only. Nothing in it is excluded.
            out.append({**row, "excluded_cost_usd": 0.0})
            continue
        scoped_cost = float(row.get("scoped_cost_usd") or 0.0)
        used = float(row["used_percent"])
        unmatched = scoped_cost
        if row.get("window") == PARENT_WINDOW:
            for scoped in scoped_at.get((row["provider"], row.get("account_id"), row["observed_at"]), []):
                share = scoped.get("allowance_percent")
                if share is None:
                    continue
                used -= float(scoped["used_percent"]) * float(share) / 100.0
                unmatched -= float(scoped["cost_usd"])
        out.append({
            **row,
            "used_percent": used,
            "cost_usd": float(row["cost_usd"]) - scoped_cost,
            "excluded_cost_usd": max(unmatched, 0.0),
        })
    return out
