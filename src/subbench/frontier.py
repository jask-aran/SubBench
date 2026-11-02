"""What a subscription buys on the benchmark frontier.

The site's overview divides each product's measured weekly value by what one benchmark task
costs on each model. This is the same arithmetic for the terminal, over the local database
and the LLM-Pareto benchmark bundle the site serves (data/frontier.json). The matching and
frontier rules mirror web/src/lib/frontier.js; tests hold the two to the same cases.

It is a throughput, not a promise: a real workload spends different tokens per task than a
benchmark does, and a subscription's meter does not price every model alike.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SITE_BUNDLE_URL = "https://subbench.jask-aran.com/data/frontier.json"
REPO_BUNDLE = Path(__file__).resolve().parents[2] / "web" / "public" / "data" / "frontier.json"


def load_bundle(source: str | None = None, timeout: float = 20.0) -> dict[str, Any]:
    """The benchmark bundle from a path or URL; by default the repository copy, then the site."""
    candidates = [source] if source else [str(REPO_BUNDLE), SITE_BUNDLE_URL]
    errors = []
    for candidate in candidates:
        try:
            if re.match(r"https?://", candidate):
                with urllib.request.urlopen(candidate, timeout=timeout) as response:
                    return json.load(response)
            return json.loads(Path(candidate).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError("Could not load the benchmark bundle: " + "; ".join(errors))


def normalise_model_name(name: str) -> str:
    """Usage logs say "gpt-5.6-sol" or "claude-opus-5-20260724"; the bundle "gpt-5-6-sol"."""
    value = str(name or "").lower().strip()
    value = re.sub(r"^(openai|anthropic)[/:]", "", value)
    value = re.sub(r"[._\s]+", "-", value)
    value = re.sub(r"-(20\d{6}|\d{4}-\d{2}-\d{2})$", "", value)
    value = re.sub(r"-latest$", "", value)
    return re.sub(r"-+", "-", value)


def match_model(name: str, model_ids: Iterable[str]) -> str | None:
    """The bundle id a usage model corresponds to, or None for a model the bundle predates."""
    slug = normalise_model_name(name)
    if not slug:
        return None
    index: dict[str, str] = {}
    for model_id in model_ids:
        index.setdefault(normalise_model_name(model_id), model_id)
    if slug in index:
        return index[slug]
    for suffix in ("-xhigh", "-high", "-medium"):
        if slug + suffix in index:
            return index[slug + suffix]
    stem = re.sub(r"-(xhigh|high|medium|low|max|thinking|reasoning)$", "", slug)
    return match_model(stem, index.values()) if stem != slug else None


@dataclass(frozen=True)
class Buy:
    model_id: str
    label: str
    score: float
    cost_per_task: float
    tasks: float
    frontier: bool
    yours: bool


def frontier_ids(points: list[Mapping[str, Any]]) -> set[str]:
    """Ids on the cost-score Pareto frontier: nothing cheaper scores as well."""
    ordered = sorted(points, key=lambda p: (p["cost"], -p["score"]))
    best = float("-inf")
    keep: set[str] = set()
    for point in ordered:
        if point["score"] > best + 1e-12:
            keep.add(point["key"])
            best = point["score"]
    return keep


def buys(bundle: Mapping[str, Any], metric_id: str, weekly_value: float, used_models: Iterable[str] = ()) -> list[Buy]:
    """Tasks one weekly limit buys, for the frontier of a metric plus the models you use."""
    labels = {entity["id"]: entity.get("label") or entity["id"] for entity in bundle["entities"]["model"]}
    mine = {match_model(name, labels) for name in used_models} - {None}
    points = []
    for item in bundle["observations"]:
        cost = (item.get("resources") or {}).get("cost")
        if item.get("metric_id") != metric_id or not isinstance(cost, (int, float)) or cost <= 0:
            continue
        key = item["entity_id"] + "|" + (item.get("variant") or "")
        points.append({"key": key, "id": item["entity_id"], "score": item["score"], "cost": cost, "variant": item.get("variant")})
    on_frontier = frontier_ids(points)
    rows = [
        Buy(
            model_id=point["id"],
            label=labels.get(point["id"], point["id"]) + (f" ({point['variant']})" if point["variant"] and point["variant"] not in labels.get(point["id"], "") else ""),
            score=point["score"], cost_per_task=point["cost"], tasks=weekly_value / point["cost"],
            frontier=point["key"] in on_frontier, yours=point["id"] in mine,
        )
        for point in points
        if point["key"] in on_frontier or point["id"] in mine
    ]
    return sorted(rows, key=lambda row: -row.score)
