"""API list prices, from LiteLLM's public table, for valuing local usage.

The collector prices each request the way ccusage did, so a database that switched from
ccusage to the built-in reader keeps one continuous spend series:

- each token class at its own rate (input, cache write, cache read, output);
- a cache write with a one-hour lifetime at the one-hour rate;
- a long-context rate ("_above_200k_tokens") for every class of a request whose input
  context (input, cache writes and cache reads together) is past the threshold, as the
  provider bills it.

A snapshot of the table ships inside the package, so pricing works offline. When the
network is available the live table is fetched at most once a day and cached; set
SUBBENCH_OFFLINE=1 to use only the snapshot.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
REFRESH_SECONDS = 24 * 3600
_NAME = re.compile(r"^(claude|gpt|o\d|codex|chatgpt)")
_TIER = re.compile(r"_above_(\d+)k_tokens$")


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base).expanduser() / "subbench" / "prices.json"


def _bundled() -> dict[str, dict[str, float]]:
    text = resources.files("subbench").joinpath("data/prices.json").read_text()
    return json.loads(text)["models"]


def slim(table: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """The rows and fields this package prices with: Anthropic and OpenAI models, per-token rates."""
    keep: dict[str, dict[str, float]] = {}
    for name, row in table.items():
        if not isinstance(row, dict) or "/" in name or not _NAME.match(name):
            continue
        if row.get("litellm_provider") not in ("anthropic", "openai") or row.get("input_cost_per_token") is None:
            continue
        keep[name] = {key: float(value) for key, value in row.items()
                      if isinstance(value, (int, float)) and not isinstance(value, bool)
                      and (key.endswith("_cost") or "_cost_" in key or key.endswith("_per_token") or "_per_token_" in key)
                      and not re.search(r"batches|flex|priority|ultrafast|decisions", key)}
    return keep


def _fetch() -> dict[str, dict[str, float]] | None:
    from .net import request

    try:
        table = slim(json.loads(request(LITELLM_URL, purpose="price table", headers={"User-Agent": "subbench"}, timeout=15)))
    except Exception:  # noqa: BLE001 - any failure falls back to the snapshot
        return None
    return table or None


def load_table(*, now: float | None = None) -> dict[str, dict[str, float]]:
    """The freshest table available: today's cache, a new fetch, an old cache, then the snapshot."""
    bundled = _bundled()
    if os.environ.get("SUBBENCH_OFFLINE"):
        return bundled
    now = time.time() if now is None else now
    path = cache_path()
    cached: dict[str, dict[str, float]] | None = None
    try:
        stat = path.stat()
        cached = json.loads(path.read_text())
        if now - stat.st_mtime < REFRESH_SECONDS:
            return {**bundled, **cached}
    except (OSError, ValueError):
        pass
    fresh = _fetch()
    if fresh:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fresh, sort_keys=True))
        except OSError:
            pass
        return {**bundled, **fresh}
    return {**bundled, **(cached or {})}


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0


class Prices:
    def __init__(self, table: Mapping[str, Mapping[str, float]]) -> None:
        self.table = dict(table)
        self.unknown: set[str] = set()

    @lru_cache(maxsize=512)
    def lookup(self, model: str) -> Mapping[str, float] | None:
        if not model:
            return None
        candidates = [model, model.lower()]
        # claude-opus-4-5-20251101 and claude-opus-4-5 price the same; so do gpt-5-2025-08-07 and gpt-5.
        candidates += [re.sub(r"-\d{8}$|-\d{4}-\d{2}-\d{2}$", "", c) for c in candidates]
        # Bracketed or @-suffixed variants (a context-window marker, a provider version).
        candidates += [re.split(r"[\[@]", c)[0] for c in candidates]
        for name in candidates:
            if name in self.table:
                return self.table[name]
        # Longest known name the model extends at a word boundary: claude-sonnet-5-5-latest -> claude-sonnet-5-5.
        base = candidates[-1]
        best = max((name for name in self.table if base.startswith(name + "-") or base.startswith(name + ".")),
                   key=len, default=None)
        return self.table.get(best) if best else None

    def cost(self, model: str, tokens: Tokens) -> float:
        row = self.lookup(model)
        if row is None:
            self.unknown.add(model)
            return 0.0
        context = tokens.input + tokens.cache_write_5m + tokens.cache_write_1h + tokens.cache_read
        suffix = _long_context(row, context)
        one_hour = "cache_creation_input_token_cost_above_1hr" if "cache_creation_input_token_cost_above_1hr" in row else "cache_creation_input_token_cost"
        return (_rate(row, "input_cost_per_token", suffix) * tokens.input
                + _rate(row, "cache_creation_input_token_cost", suffix) * tokens.cache_write_5m
                + _rate(row, one_hour, suffix) * tokens.cache_write_1h
                + _rate(row, "cache_read_input_token_cost", suffix) * tokens.cache_read
                + _rate(row, "output_cost_per_token", suffix) * tokens.output)


def _long_context(row: Mapping[str, float], context: int) -> str:
    """The suffix of the highest long-context tier this request's context is past, or ""."""
    best = 0
    for name in row:
        match = _TIER.search(name)
        if match and context > int(match.group(1)) * 1000:
            best = max(best, int(match.group(1)))
    return f"_above_{best}k_tokens" if best else ""


def _rate(row: Mapping[str, float], key: str, suffix: str) -> float:
    if suffix and key + suffix in row:
        return row[key + suffix]
    if key in row:
        return row[key]
    # A model with no separate cache rates is billed at its input rate.
    return row.get("input_cost_per_token", 0.0) if key.startswith("cache") else 0.0


def default_prices() -> Prices:
    return Prices(load_table())
