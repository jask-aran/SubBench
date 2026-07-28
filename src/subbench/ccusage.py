from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Iterable


TOKEN_ALIASES = {
    "input_tokens": ("inputTokens", "input_tokens"),
    "cached_input_tokens": ("cachedInputTokens", "cached_input_tokens"),
    "cache_write_tokens": (
        "cacheCreationTokens",
        "cache_creation_input_tokens",
        "cacheWriteTokens",
        "cache_write_tokens",
    ),
    "cache_read_tokens": ("cacheReadTokens", "cache_read_input_tokens", "cache_read_tokens"),
    "output_tokens": ("outputTokens", "output_tokens"),
    "reasoning_output_tokens": (
        "reasoningOutputTokens",
        "reasoning_output_tokens",
        "thinkingTokens",
        "thinking_tokens",
    ),
}

MODEL_KEYS = ("model", "modelName", "model_name")
COST_KEYS = ("costUSD", "costUsd", "cost_usd", "totalCost", "total_cost")
PERIOD_START_KEYS = ("periodStart", "startTime", "start", "date", "week", "month")
PERIOD_END_KEYS = ("periodEnd", "endTime", "end")
BREAKDOWN_KEYS = ("modelBreakdowns", "model_breakdowns", "models")
CONTAINER_KEYS = ("daily", "weekly", "monthly", "sessions", "blocks", "data", "items", "results")


class CcusageSchemaError(ValueError):
    """Raised when a payload has no recognisable usage rows."""


@dataclass(frozen=True, slots=True)
class UsageRow:
    provider: str
    report: str
    period_start: str | None
    period_end: str | None
    model: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    reported_cost_usd: str | None
    source_path: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def payload_digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def normalise_payload(payload: Any, *, provider: str, report: str) -> list[UsageRow]:
    if provider not in {"claude", "codex"}:
        raise ValueError("provider must be 'claude' or 'codex'")

    rows: list[UsageRow] = []
    seen: set[tuple[Any, ...]] = set()

    for path, candidate in _walk_candidates(payload):
        breakdown = _get_breakdowns(candidate)
        if breakdown:
            costs = [_first_decimal_text(item, COST_KEYS) for _, item in breakdown]
            if any(cost is not None for cost in costs) and not all(
                cost is not None for cost in costs
            ):
                raise CcusageSchemaError(
                    f"only some model breakdowns contain cost at {path}; "
                    "refusing to create an ambiguous valuation"
                )

            parent_without_cost = {
                key: item for key, item in candidate.items() if key not in COST_KEYS
            }
            for breakdown_path, breakdown_item in breakdown:
                merged = {**parent_without_cost, **breakdown_item}
                row = _normalise_row(
                    merged,
                    provider=provider,
                    report=report,
                    source_path=f"{path}.{breakdown_path}",
                )
                _append_unique(rows, seen, row)

            # Current ccusage Codex JSON exposes per-model token counts but only
            # one cost for the enclosing period. Keep that cost in a token-free
            # aggregate row so model detail is retained without multiplying the
            # period cost by the number of models.
            parent_cost = _first_decimal_text(candidate, COST_KEYS)
            if parent_cost is not None and all(cost is None for cost in costs):
                aggregate = UsageRow(
                    provider=provider,
                    report=report,
                    period_start=_first_text(candidate, PERIOD_START_KEYS),
                    period_end=_first_text(candidate, PERIOD_END_KEYS),
                    model=None,
                    input_tokens=0,
                    cached_input_tokens=0,
                    cache_write_tokens=0,
                    cache_read_tokens=0,
                    output_tokens=0,
                    reasoning_output_tokens=0,
                    reported_cost_usd=parent_cost,
                    source_path=f"{path}.{_first_key(candidate, COST_KEYS)}",
                )
                _append_unique(rows, seen, aggregate)
            continue

        row = _normalise_row(candidate, provider=provider, report=report, source_path=path)
        _append_unique(rows, seen, row)

    if not rows:
        raise CcusageSchemaError(
            "No token-bearing rows found. Save the payload and report the ccusage version so an adapter can be added."
        )

    return rows


def _walk_candidates(value: Any, path: str = "$") -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_candidates(item, f"{path}[{index}]")
        return

    if not isinstance(value, dict):
        return

    if _has_tokens(value):
        yield path, value
        return

    preferred = False
    for key in CONTAINER_KEYS:
        child = value.get(key)
        if isinstance(child, (list, dict)):
            preferred = True
            yield from _walk_candidates(child, f"{path}.{key}")

    if preferred:
        return

    for key, child in value.items():
        if isinstance(child, (list, dict)):
            yield from _walk_candidates(child, f"{path}.{key}")


def _normalise_row(
    value: dict[str, Any], *, provider: str, report: str, source_path: str
) -> UsageRow | None:
    tokens = {name: _first_int(value, aliases) for name, aliases in TOKEN_ALIASES.items()}
    if not any(tokens.values()):
        return None

    # Preserve the token classes emitted by ccusage. Current Codex reports use
    # inputTokens for uncached input and cacheReadTokens for cached input. Older
    # payloads may instead expose cachedInputTokens inside total input.
    if provider == "codex" and tokens["cached_input_tokens"] > tokens["input_tokens"]:
        raise CcusageSchemaError(
            f"cached input exceeds total input at {source_path}; refusing to create an invalid valuation row"
        )

    return UsageRow(
        provider=provider,
        report=report,
        period_start=_first_text(value, PERIOD_START_KEYS),
        period_end=_first_text(value, PERIOD_END_KEYS),
        model=_first_text(value, MODEL_KEYS),
        input_tokens=tokens["input_tokens"],
        cached_input_tokens=tokens["cached_input_tokens"],
        cache_write_tokens=tokens["cache_write_tokens"],
        cache_read_tokens=tokens["cache_read_tokens"],
        output_tokens=tokens["output_tokens"],
        reasoning_output_tokens=tokens["reasoning_output_tokens"],
        reported_cost_usd=_first_decimal_text(value, COST_KEYS),
        source_path=source_path,
    )


def _append_unique(rows: list[UsageRow], seen: set[tuple[Any, ...]], row: UsageRow | None) -> None:
    if row is None:
        return
    key = tuple(row.to_record().values())
    if key not in seen:
        seen.add(key)
        rows.append(row)


def _get_breakdowns(value: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    for key in BREAKDOWN_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, list) and candidate and all(isinstance(item, dict) for item in candidate):
            if any(_has_tokens(item) for item in candidate):
                return [
                    (f"{key}[{index}]", item)
                    for index, item in enumerate(candidate)
                ]
        if isinstance(candidate, dict):
            expanded: list[tuple[str, dict[str, Any]]] = []
            for model, item in candidate.items():
                if isinstance(item, dict) and _has_tokens(item):
                    expanded.append((f"{key}[{model!r}]", {"model": model, **item}))
            if expanded:
                return expanded
    return []


def _has_tokens(value: dict[str, Any]) -> bool:
    return any(any(alias in value for alias in aliases) for aliases in TOKEN_ALIASES.values())


def _first_int(value: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        raw = value.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number < 0:
            raise CcusageSchemaError(f"negative token count in field {key}")
        return number
    return 0


def _first_text(value: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        raw = value.get(key)
        if raw is not None and str(raw).strip():
            return str(raw)
    return None


def _first_key(value: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in value:
            return key
    raise AssertionError("expected one of the requested keys")


def _first_decimal_text(value: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        raw = value.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            return str(Decimal(str(raw)))
        except (InvalidOperation, ValueError):
            continue
    return None


def imported_at() -> str:
    return datetime.now(timezone.utc).isoformat()
