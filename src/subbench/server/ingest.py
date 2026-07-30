"""Validate and normalise pushed evidence.

Pure functions, no I/O. Validation is the reason derivation runs server-side at all: an
agent can send anything, and a bad row that reaches storage corrupts every estimate
derived from its window afterwards. A batch is accepted whole or rejected whole -- a
partial write would leave quota advancing against spend that was never recorded, which is
indistinguishable from the unobserved-usage pattern the estimator deliberately discards.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

SUPPORTED_SCHEMA_VERSION = 1
MAX_USAGE_ROWS = 5000
MAX_ENTITLEMENT_ROWS = 5000
PROVIDERS = frozenset({"claude", "codex"})

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


class IngestError(Exception):
    """A batch that must be rejected. `status` is the HTTP status to return."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Batch:
    agent_id: str
    entitlements: list[dict[str, Any]]
    usage: list[dict[str, Any]]

    @property
    def cursor(self) -> dict[str, str | None]:
        return {
            "entitlement_cursor": self.entitlements[-1]["observed_at"] if self.entitlements else None,
            "usage_cursor": self.usage[-1]["last_seen_at"] if self.usage else None,
        }


def _require(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise IngestError(400, f"missing field: {key}")
    return payload[key]


def _text(value: Any, field: str, *, allow_none: bool = False) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise IngestError(400, f"{field} must not be null")
    if not isinstance(value, str) or not value.strip():
        raise IngestError(400, f"{field} must be a non-empty string")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IngestError(400, f"{field} must be a number")
    number = int(value)
    if number < 0:
        raise IngestError(400, f"{field} must not be negative")
    return number


def _decimal_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        raise IngestError(400, f"{field} must be a decimal string") from None


def _entitlement(row: Mapping[str, Any], agent_id: str) -> dict[str, Any]:
    provider = _text(row.get("provider"), "provider")
    if provider not in PROVIDERS:
        raise IngestError(400, f"unknown provider: {provider}")

    used = row.get("used_percent")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        raise IngestError(400, "used_percent must be a number")
    if not 0.0 <= float(used) <= 100.0:
        # A meter outside 0-100 is a parse error at the source. Storing it would let a
        # single bad reading dominate every span-weighted slope in its window.
        raise IngestError(400, f"used_percent out of range: {used}")

    duration = row.get("duration_minutes")
    if duration is not None:
        duration = _non_negative_int(duration, "duration_minutes")

    account_id = _text(row.get("account_id"), "account_id", allow_none=True)
    return {
        "agent_id": agent_id,
        "observed_at": _text(row.get("observed_at"), "observed_at"),
        "provider": provider,
        "account_id": account_id,
        "account_key": account_id or "",
        "window": _text(row.get("window"), "window"),
        "used_percent": float(used),
        "resets_at": _text(row.get("resets_at"), "resets_at", allow_none=True),
        "duration_minutes": duration,
        "source": _text(row.get("source"), "source", allow_none=True) or "pushed",
    }


def _usage(row: Mapping[str, Any], agent_id: str) -> dict[str, Any]:
    provider = _text(row.get("provider"), "provider")
    if provider not in PROVIDERS:
        raise IngestError(400, f"unknown provider: {provider}")
    account_id = _text(row.get("account_id"), "account_id", allow_none=True)
    model = _text(row.get("model"), "model", allow_none=True)
    last_seen = _text(row.get("last_seen_at"), "last_seen_at")
    imported = _text(row.get("imported_at"), "imported_at", allow_none=True) or last_seen
    record = {
        "agent_id": agent_id,
        "import_key": _text(row.get("import_key"), "import_key"),
        "imported_at": imported,
        "last_seen_at": last_seen,
        "provider": provider,
        "account_id": account_id,
        "account_key": account_id or "",
        "period_start": _text(row.get("period_start"), "period_start", allow_none=True),
        "model": model,
        "model_key": model or "",
        "reported_cost_usd": _decimal_text(row.get("reported_cost_usd"), "reported_cost_usd"),
        "source_path": _text(row.get("source_path"), "source_path"),
    }
    for field in TOKEN_FIELDS:
        record[field] = _non_negative_int(row.get(field, 0), field)
    return record


def parse(payload: Mapping[str, Any]) -> Batch:
    if not isinstance(payload, Mapping):
        raise IngestError(400, "payload must be an object")

    version = _require(payload, "schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise IngestError(400, "schema_version must be an integer")
    if version > SUPPORTED_SCHEMA_VERSION:
        # Fail loudly rather than storing rows this server would misread.
        raise IngestError(409, f"schema_version {version} unsupported, this server speaks {SUPPORTED_SCHEMA_VERSION}")
    if version < SUPPORTED_SCHEMA_VERSION:
        raise IngestError(400, f"schema_version {version} is no longer accepted")

    agent_id = _text(_require(payload, "agent_id"), "agent_id")

    entitlements = payload.get("entitlements") or []
    usage = payload.get("usage") or []
    if not isinstance(entitlements, list) or not isinstance(usage, list):
        raise IngestError(400, "entitlements and usage must be arrays")
    if len(usage) > MAX_USAGE_ROWS:
        raise IngestError(413, f"{len(usage)} usage rows exceeds the {MAX_USAGE_ROWS} limit")
    if len(entitlements) > MAX_ENTITLEMENT_ROWS:
        raise IngestError(413, f"{len(entitlements)} entitlement rows exceeds the {MAX_ENTITLEMENT_ROWS} limit")

    return Batch(
        agent_id=agent_id,
        entitlements=[_entitlement(row, agent_id) for row in entitlements],
        usage=[_usage(row, agent_id) for row in usage],
    )


ENTITLEMENT_UPSERT = """
INSERT INTO entitlement_snapshots
    (agent_id, observed_at, provider, account_id, account_key, window,
     used_percent, resets_at, duration_minutes, source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (agent_id, provider, account_key, window, observed_at) DO UPDATE SET
    used_percent = excluded.used_percent,
    resets_at = excluded.resets_at,
    duration_minutes = excluded.duration_minutes
"""

ENTITLEMENT_COLUMNS = (
    "agent_id", "observed_at", "provider", "account_id", "account_key", "window",
    "used_percent", "resets_at", "duration_minutes", "source",
)

USAGE_UPSERT = """
INSERT INTO usage_rows
    (agent_id, import_key, imported_at, last_seen_at, provider, account_id, account_key,
     period_start, model, model_key, input_tokens, cached_input_tokens,
     cache_write_tokens, cache_read_tokens, output_tokens, reasoning_output_tokens,
     reported_cost_usd, source_path)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (agent_id, import_key, source_path, model_key) DO UPDATE SET
    last_seen_at = excluded.last_seen_at,
    reported_cost_usd = excluded.reported_cost_usd
"""

USAGE_COLUMNS = (
    "agent_id", "import_key", "imported_at", "last_seen_at", "provider", "account_id",
    "account_key", "period_start", "model", "model_key", *TOKEN_FIELDS,
    "reported_cost_usd", "source_path",
)

AGENT_UPSERT = """
INSERT INTO agents (agent_id, label, first_seen, last_seen) VALUES (?, NULL, ?, ?)
ON CONFLICT (agent_id) DO UPDATE SET last_seen = excluded.last_seen
"""


def bind(row: Mapping[str, Any], columns: tuple[str, ...]) -> list[Any]:
    return [row.get(name) for name in columns]
