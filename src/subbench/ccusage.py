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
_OWN_COST = object()


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
        breakdowns = _get_breakdowns(candidate)
        if breakdowns:
            aggregate_has_cost = _has_cost(candidate)
            for index, breakdown in enumerate(breakdowns):
                merged = {**candidate, **breakdown}
                row = _normalise_row(
                    merged,
                    provider=provider,
                    report=report,
                    source_path=f"{path}.modelBreakdowns[{index}]",
                    cost_source=None if aggregate_has_cost else breakdown,
                )
                _append_unique(rows, seen, row)
            if aggregate_has_cost:
                aggregate = _normalise_row(
                    {**candidate, "model": None},
                    provider=provider,
                    report=report,
                    source_path=path,
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
    value: dict[str, Any], *, provider: str, report: str, source_path: str,
    cost_source: dict[str, Any] | None | object = _OWN_COST,
) -> UsageRow | None:
    tokens = {name: _first_int(value, aliases) for name, aliases in TOKEN_ALIASES.items()}
    if not any(tokens.values()):
        return None

    # Codex cached input is included in total input. Claude cache read/write are
    # reported as separate API token classes, so cached_input_tokens is normally zero.
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
        reported_cost_usd=_first_decimal_text(
            value if cost_source is _OWN_COST else cost_source, COST_KEYS
        ) if cost_source is not None else None,
        source_path=source_path,
    )


def _append_unique(rows: list[UsageRow], seen: set[tuple[Any, ...]], row: UsageRow | None) -> None:
    if row is None:
        return
    key = tuple(row.to_record().values())
    if key not in seen:
        seen.add(key)
        rows.append(row)


def _get_breakdowns(value: dict[str, Any]) -> list[dict[str, Any]]:
    for key in BREAKDOWN_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, list) and candidate and all(isinstance(item, dict) for item in candidate):
            if any(_has_tokens(item) for item in candidate):
                return candidate
        if isinstance(candidate, dict):
            expanded: list[dict[str, Any]] = []
            for model, item in candidate.items():
                if isinstance(item, dict) and _has_tokens(item):
                    expanded.append({"model": model, **item})
            if expanded:
                return expanded
    return []


def _has_tokens(value: dict[str, Any]) -> bool:
    return any(any(alias in value for alias in aliases) for aliases in TOKEN_ALIASES.values())


def _has_cost(value: dict[str, Any]) -> bool:
    return _first_decimal_text(value, COST_KEYS) is not None


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
