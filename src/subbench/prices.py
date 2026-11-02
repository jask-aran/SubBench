"""API list prices for valuing local usage, resolved the way ccusage resolves them.

The months of history recorded before the built-in reader were priced by ccusage, so the
reader prices with ccusage's own rates and rules or the spend series would step when a
machine switched. `data/prices.json` is generated from ccusage's pricing code
(packaging/prices/README.md): for every Anthropic and OpenAI model name ccusage knows, the
rate it resolves, its long-context tier, and its Fast, Flex and Ultrafast multipliers.

Claude Code (ccusage's `calculate_cost_from_pricing`):

- each token class at its own rate; a one-hour cache write at twice the input rate;
- a model with a long-context threshold bills every class of a request past it at the
  long-context rates; a model without one bills each class's tokens above 200K at the
  higher rate;
- a request recorded with `speed: "fast"` is multiplied by the model's Fast multiplier.

Codex (ccusage's `calculate_codex_bucket_cost`): a request whose input is past the model's
long-context split is billed wholly at the long-context rates, and usage recorded under a
service tier pays that tier's multiplier.

A model the table does not have is looked up in LiteLLM's public table, fetched at most
once a day (never with SUBBENCH_OFFLINE=1), and priced with plain rates.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
REFRESH_SECONDS = 24 * 3600
DEFAULT_LONG_CONTEXT = 200_000
CACHE_WRITE_1H_MULTIPLIER = 2.0
_NAME = re.compile(r"^(claude|gpt|o\d|codex|chatgpt)")


@dataclass(frozen=True)
class Rate:
    input: float
    output: float
    cache_create: float
    cache_read: float
    cache_read_explicit: bool = True
    input_above: float | None = None
    output_above: float | None = None
    cache_create_above: float | None = None
    cache_read_above: float | None = None
    threshold: int | None = None          # Claude: whole request at long rates past this
    split_threshold: int = DEFAULT_LONG_CONTEXT  # Codex: a request's input past this is long-context
    fast: float = 1.0
    flex: float = 1.0
    ultrafast: float = 1.0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Rate":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__ if row.get(name) is not None})


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base).expanduser() / "subbench" / "litellm.json"


def _bundled() -> dict[str, Rate]:
    text = resources.files("subbench").joinpath("data/prices.json").read_text()
    return {name: Rate.from_row(row) for name, row in json.loads(text)["models"].items()}


def from_litellm(table: Mapping[str, Any]) -> dict[str, Rate]:
    """Plain rates for the Anthropic and OpenAI rows of LiteLLM's table."""
    rates: dict[str, Rate] = {}
    for name, row in table.items():
        if not isinstance(row, dict) or "/" in name or not _NAME.match(name):
            continue
        if row.get("litellm_provider") not in ("anthropic", "openai"):
            continue
        number = lambda key: row.get(key) if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool) else None  # noqa: E731
        base = number("input_cost_per_token")
        if base is None:
            continue
        cache_read = number("cache_read_input_token_cost")
        rates[name] = Rate(
            input=float(base), output=float(number("output_cost_per_token") or 0.0),
            cache_create=float(number("cache_creation_input_token_cost") or base * 1.25),
            cache_read=float(cache_read if cache_read is not None else base),
            cache_read_explicit=cache_read is not None,
            input_above=number("input_cost_per_token_above_200k_tokens"),
            output_above=number("output_cost_per_token_above_200k_tokens"),
            cache_create_above=number("cache_creation_input_token_cost_above_200k_tokens"),
            cache_read_above=number("cache_read_input_token_cost_above_200k_tokens"),
        )
    return rates


def _fetch() -> dict[str, Any] | None:
    from .net import request

    try:
        table = json.loads(request(LITELLM_URL, purpose="price table", headers={"User-Agent": "subbench"}, timeout=15))
    except Exception:  # noqa: BLE001 - any failure leaves the bundled table alone
        return None
    return table if isinstance(table, dict) else None


def _extra(*, now: float | None = None) -> dict[str, Rate]:
    """LiteLLM rows for models the bundled table lacks: today's cache, a fetch, or an old cache."""
    if os.environ.get("SUBBENCH_OFFLINE"):
        return {}
    now = time.time() if now is None else now
    path = cache_path()
    cached = None
    try:
        cached = json.loads(path.read_text())
        if now - path.stat().st_mtime < REFRESH_SECONDS:
            return from_litellm(cached)
    except (OSError, ValueError):
        pass
    fresh = _fetch()
    if fresh:
        slim = {k: v for k, v in fresh.items() if isinstance(v, dict) and "/" not in k and _NAME.match(k)}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(slim, sort_keys=True))
        except OSError:
            pass
        return from_litellm(slim)
    return from_litellm(cached) if isinstance(cached, dict) else {}


def load_table(*, now: float | None = None) -> dict[str, Rate]:
    bundled = _bundled()
    return {**_extra(now=now), **bundled}


class Prices:
    def __init__(self, table: Mapping[str, Rate | Mapping[str, Any]]) -> None:
        self.table = {name: rate if isinstance(rate, Rate) else Rate.from_row(rate) for name, rate in table.items()}
        self.unknown: set[str] = set()

    @lru_cache(maxsize=1024)
    def lookup(self, model: str) -> Rate | None:
        if not model:
            return None
        candidates = [model, model.lower()]
        candidates += [re.sub(r"-\d{8}$|-\d{4}-\d{2}-\d{2}$", "", c) for c in candidates]
        candidates += [re.split(r"[\[@]", c)[0] for c in candidates]
        for name in candidates:
            if name in self.table:
                return self.table[name]
        base = candidates[-1]
        # Only a versioned name: "claude" alone would price every unknown Claude model.
        best = max((name for name in self.table if re.search(r"\d", name)
                    and (base.startswith(name + "-") or base.startswith(name + "."))),
                   key=len, default=None)
        return self.table.get(best) if best else None

    def rate(self, model: str) -> Rate | None:
        rate = self.lookup(model)
        if rate is None and model:
            self.unknown.add(model)
        return rate

    def cost(self, model: str, tokens: Tokens, *, fast: bool = False) -> float:
        """One Claude request (ccusage `calculate_cost_from_tokens`)."""
        rate = self.rate(model)
        if rate is None:
            return 0.0
        return claude_cost(rate, tokens) * (rate.fast if fast else 1.0)


def _tiered(tokens: int, base: float, above: float | None, threshold: int) -> float:
    if tokens == 0:
        return 0.0
    if above is not None and tokens > threshold:
        return threshold * base + (tokens - threshold) * above
    return tokens * base


def claude_cost(rate: Rate, tokens: Tokens) -> float:
    one_hour = rate.input * CACHE_WRITE_1H_MULTIPLIER
    one_hour_above = rate.input_above * CACHE_WRITE_1H_MULTIPLIER if rate.input_above is not None else None
    if rate.threshold is not None:
        context = tokens.input + tokens.cache_read + tokens.cache_write_5m + tokens.cache_write_1h
        long = context > rate.threshold
        pick = (lambda base, above: above if long and above is not None else base)
        return (tokens.input * pick(rate.input, rate.input_above)
                + tokens.output * pick(rate.output, rate.output_above)
                + tokens.cache_write_5m * pick(rate.cache_create, rate.cache_create_above)
                + tokens.cache_write_1h * pick(one_hour, one_hour_above)
                + tokens.cache_read * pick(rate.cache_read, rate.cache_read_above))
    t = DEFAULT_LONG_CONTEXT
    return (_tiered(tokens.input, rate.input, rate.input_above, t)
            + _tiered(tokens.output, rate.output, rate.output_above, t)
            + _tiered(tokens.cache_write_5m, rate.cache_create, rate.cache_create_above, t)
            + _tiered(tokens.cache_write_1h, one_hour, one_hour_above, t)
            + _tiered(tokens.cache_read, rate.cache_read, rate.cache_read_above, t))


@dataclass
class Bucket:
    """Codex usage summed over requests, with the long-context requests' share kept apart."""
    input: int = 0          # all input, cached and cache writes included
    cached: int = 0
    creation: int = 0
    output: int = 0
    long_input: int = 0
    long_cached: int = 0
    long_creation: int = 0
    long_output: int = 0

    def add(self, input: int, cached: int, creation: int, output: int, long: bool) -> None:
        self.input += input
        self.cached += cached
        self.creation += creation
        self.output += output
        if long:
            self.long_input += input
            self.long_cached += cached
            self.long_creation += creation
            self.long_output += output

    def plus(self, other: "Bucket") -> "Bucket":
        return Bucket(*(a + b for a, b in zip(self._values(), other._values())))

    def minus(self, other: "Bucket") -> "Bucket":
        return Bucket(*(max(0, a - b) for a, b in zip(self._values(), other._values())))

    def _values(self) -> tuple[int, ...]:
        return (self.input, self.cached, self.creation, self.output,
                self.long_input, self.long_cached, self.long_creation, self.long_output)


def codex_bucket_cost(usage: Bucket, rate: Rate) -> float:
    cache_read = rate.cache_read if rate.cache_read_explicit else rate.input
    cache_creation = rate.cache_create
    long_input_rate = rate.input_above if rate.input_above is not None else rate.input
    long_output_rate = rate.output_above if rate.output_above is not None else rate.output
    if rate.cache_read_explicit:
        long_cache_read = rate.cache_read_above if rate.cache_read_above is not None else cache_read
    else:
        long_cache_read = long_input_rate
    long_creation_rate = rate.cache_create_above if rate.cache_create_above is not None else cache_creation
    long_input = min(usage.long_input, usage.input)
    long_cached = min(usage.long_cached, usage.cached, long_input)
    long_creation = min(usage.long_creation, usage.creation, max(0, long_input - long_cached))
    long_output = min(usage.long_output, usage.output)
    short_cached = max(0, usage.cached - long_cached)
    short_creation = max(0, usage.creation - long_creation)
    short_plain = max(0, max(0, usage.input - long_input) - (short_cached + short_creation))
    long_plain = max(0, long_input - (long_cached + long_creation))
    return (short_plain * rate.input + short_cached * cache_read + short_creation * cache_creation
            + (usage.output - long_output) * rate.output
            + long_plain * long_input_rate + long_cached * long_cache_read
            + long_creation * long_creation_rate + long_output * long_output_rate)


TIERS = ("standard", "fast", "flex", "ultrafast")


@dataclass
class TieredBucket:
    total: Bucket = field(default_factory=Bucket)
    recorded: dict[str, Bucket] = field(default_factory=lambda: {tier: Bucket() for tier in TIERS})


def codex_cost(usage: TieredBucket, rate: Rate, *, default_tier: str = "standard") -> float:
    """ccusage `calculate_codex_usage_cost` under its automatic speed policy.

    Usage recorded under a tier pays that tier's multiplier; unrecorded usage pays the tier
    the Codex config selects (standard unless config.toml says otherwise).
    """
    standard = codex_bucket_cost(usage.total, rate)
    recorded = usage.recorded
    unclassified = usage.total.minus(recorded["standard"].plus(recorded["flex"].plus(recorded["fast"].plus(recorded["ultrafast"]))))
    flex, fast, ultra = recorded["flex"], recorded["fast"], recorded["ultrafast"]
    if default_tier == "flex":
        flex = flex.plus(unclassified)
    elif default_tier == "fast":
        fast = fast.plus(unclassified)
    elif default_tier == "ultrafast":
        ultra = ultra.plus(unclassified)
    return (standard
            + codex_bucket_cost(flex, rate) * (rate.flex - 1.0)
            + codex_bucket_cost(fast, rate) * (rate.fast - 1.0)
            + codex_bucket_cost(ultra, rate) * (rate.ultrafast - 1.0))


def default_prices() -> Prices:
    return Prices(load_table())
