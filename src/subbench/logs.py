"""Read Claude Code and Codex session logs directly, in place of ccusage.

Both agents write one JSON object per line as they work. The collector reads them here and
prices them with `prices`, producing the same daily report ccusage printed (the same keys,
the same per-model breakdown, the same totals), so the rest of the pipeline and every
database recorded before the switch read it unchanged.

Claude Code (~/.claude/projects/**/*.jsonl, or each CLAUDE_CONFIG_DIR): every assistant
message carries its usage. A message is logged again when a session is resumed or
branched, so a message id and request id seen once are not counted twice.

Codex ($CODEX_HOME/sessions and archived_sessions): token_count events carry the session's
running total. Each event counts the change in that total, so an event repeated with the
same total (Codex sends one beside each rate-limit update) adds nothing. The model comes
from the turn context before it.

Reading is incremental. A file that has only grown is read from where the last pass
stopped; a file that shrank or was replaced is read again from the start.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .prices import Prices, Tokens, default_prices

CODEX_FALLBACK_MODEL = "gpt-5"


def claude_roots() -> list[Path]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        bases = [Path(part.strip()).expanduser() for part in configured.split(",") if part.strip()]
    else:
        bases = [Path.home() / ".config" / "claude", Path.home() / ".claude"]
    return [base / "projects" for base in bases]


def codex_roots() -> list[Path]:
    homes = os.environ.get("CODEX_HOME", str(Path.home() / ".codex")).split(",")
    roots: list[Path] = []
    for raw in homes:
        if raw.strip():
            home = Path(raw.strip()).expanduser()
            roots.extend([home / "sessions", home / "archived_sessions"])
    return roots


def roots(provider: str) -> list[Path]:
    if provider == "claude":
        return claude_roots()
    if provider == "codex":
        return codex_roots()
    raise ValueError(f"unsupported provider: {provider}")


def discover(provider: str) -> list[Path]:
    seen: dict[Path, None] = {}
    for root in roots(provider):
        if root.is_dir():
            for path in sorted(root.rglob("*.jsonl")):
                if path.is_file():
                    seen.setdefault(path.resolve(), None)
    return list(seen)


@dataclass(frozen=True)
class Entry:
    day: str
    model: str
    tokens: Tokens
    reasoning: int = 0
    hour: str = ""  # the UTC hour the request was made in, e.g. 2026-11-02T14:00:00+00:00
    key: str | None = None  # Claude's message and request id, for de-duplication
    cost: float | None = None  # a cost the log itself recorded, which wins over a computed one


@dataclass
class _FileState:
    inode: int
    offset: int
    entries: list[Entry] = field(default_factory=list)
    # Codex: the running total last seen, and the model in force.
    total: dict[str, int] | None = None
    model: str | None = None


def _when(timestamp: Any) -> tuple[str, str] | None:
    """The local calendar day (as ccusage reported it) and the UTC hour of a timestamp."""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    hour = moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    return moment.astimezone().date().isoformat(), hour


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _claude_entry(record: dict[str, Any]) -> Entry | None:
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    model = message.get("model")
    if not isinstance(usage, dict) or not isinstance(model, str) or not model or model == "<synthetic>":
        return None
    when = _when(record.get("timestamp"))
    if when is None:
        return None
    written = _int(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    one_hour = min(written, _int(split.get("ephemeral_1h_input_tokens")))
    tokens = Tokens(
        input=_int(usage.get("input_tokens")),
        cache_write_5m=written - one_hour,
        cache_write_1h=one_hour,
        cache_read=_int(usage.get("cache_read_input_tokens")),
        output=_int(usage.get("output_tokens")),
    )
    if not (tokens.input or written or tokens.cache_read or tokens.output):
        return None
    message_id, request_id = message.get("id"), record.get("requestId")
    key = f"{message_id}:{request_id}" if message_id and request_id else None
    cost = record.get("costUSD")
    return Entry(day=when[0], hour=when[1], model=model, tokens=tokens, key=key,
                 cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)


_CODEX_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def _codex_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {name: _int(value.get(name)) for name in _CODEX_FIELDS}


def _codex_step(state: _FileState, record: dict[str, Any]) -> Entry | None:
    kind = record.get("type")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if kind == "turn_context":
        model = payload.get("model")
        if isinstance(model, str) and model:
            state.model = model
        return None
    if kind != "event_msg" or payload.get("type") != "token_count":
        return None
    info = payload.get("info") if isinstance(payload.get("info"), dict) else None
    if info is None:
        return None
    model = info.get("model") if isinstance(info.get("model"), str) and info.get("model") else state.model
    total = _codex_usage(info.get("total_token_usage"))
    last = _codex_usage(info.get("last_token_usage"))
    if total is not None:
        previous = state.total
        state.total = total
        if previous is not None and all(total[name] >= previous[name] for name in _CODEX_FIELDS):
            delta = {name: total[name] - previous[name] for name in _CODEX_FIELDS}
        elif previous is None:
            # The first event of a session: its total is everything so far.
            delta = total
        else:
            # The total went backwards (a new session reusing the file, or a compaction):
            # this event's own usage is the only safe figure.
            delta = last or {name: 0 for name in _CODEX_FIELDS}
    elif last is not None:
        delta = last
    else:
        return None
    if not any(delta.values()):
        return None
    when = _when(record.get("timestamp"))
    if when is None:
        return None
    # Codex counts cached tokens inside input and reasoning inside output.
    cached = min(delta["cached_input_tokens"], delta["input_tokens"])
    tokens = Tokens(input=delta["input_tokens"] - cached, cache_read=cached, output=delta["output_tokens"])
    return Entry(day=when[0], hour=when[1], model=model or CODEX_FALLBACK_MODEL, tokens=tokens,
                 reasoning=delta["reasoning_output_tokens"])


class LogReader:
    """Keeps what it has read, so a long-running collector only parses what was appended."""

    def __init__(self, provider: str, prices: Prices | None = None) -> None:
        if provider not in ("claude", "codex"):
            raise ValueError(f"unsupported provider: {provider}")
        self.provider = provider
        self.prices = prices
        self._files: dict[Path, _FileState] = {}
        self._markers = (b'"usage"',) if provider == "claude" else (b'"token_count"', b'"turn_context"')

    def _read(self, path: Path) -> list[Entry]:
        try:
            stat = path.stat()
        except OSError:
            self._files.pop(path, None)
            return []
        state = self._files.get(path)
        if state is None or state.inode != stat.st_ino or stat.st_size < state.offset:
            state = _FileState(inode=stat.st_ino, offset=0)
            self._files[path] = state
        if stat.st_size > state.offset:
            try:
                with path.open("rb") as handle:
                    handle.seek(state.offset)
                    chunk = handle.read(stat.st_size - state.offset)
            except OSError:
                return state.entries
            # Only whole lines: the agent may be part-way through writing the last one.
            end = chunk.rfind(b"\n") + 1
            for line in chunk[:end].splitlines():
                # Lines that cannot carry usage (prompts, replies, tool output) are skipped
                # unparsed, so their content is never decoded at all.
                if not any(marker in line for marker in self._markers):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                entry = _claude_entry(record) if self.provider == "claude" else _codex_step(state, record)
                if entry is not None:
                    state.entries.append(entry)
            state.offset += end
        return state.entries

    def entries(self) -> list[Entry]:
        paths = discover(self.provider)
        for gone in set(self._files) - set(paths):
            self._files.pop(gone, None)
        collected: list[Entry] = []
        seen: set[str] = set()
        for path in paths:
            for entry in self._read(path):
                if entry.key is not None:
                    if entry.key in seen:
                        continue
                    seen.add(entry.key)
                collected.append(entry)
        return collected

    def hourly(self) -> dict[tuple[str, str], dict[str, float]]:
        prices = self.prices or default_prices()
        self.prices = prices
        return hourly_buckets(self.entries(), prices)

    def daily(self, *, since: date | None = None) -> dict[str, Any]:
        """The ccusage `daily --json` report for this provider."""
        prices = self.prices or default_prices()
        self.prices = prices
        return daily_report(self.entries(), prices, provider=self.provider, since=since)


def daily_report(entries: Iterable[Entry], prices: Prices, *, provider: str, since: date | None = None) -> dict[str, Any]:
    days: dict[str, dict[str, dict[str, float]]] = {}
    for entry in entries:
        if since is not None and entry.day < since.isoformat():
            continue
        cost = entry.cost if entry.cost is not None else prices.cost(entry.model, entry.tokens)
        model = days.setdefault(entry.day, {}).setdefault(entry.model, {
            "inputTokens": 0, "outputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0,
            "reasoningOutputTokens": 0, "cost": 0.0,
        })
        model["inputTokens"] += entry.tokens.input
        model["outputTokens"] += entry.tokens.output
        model["cacheCreationTokens"] += entry.tokens.cache_write_5m + entry.tokens.cache_write_1h
        model["cacheReadTokens"] += entry.tokens.cache_read
        model["reasoningOutputTokens"] += entry.reasoning
        model["cost"] += cost

    classes = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens")
    daily: list[dict[str, Any]] = []
    totals = {name: 0 for name in classes} | {"totalCost": 0.0, "totalTokens": 0}
    for day in sorted(days):
        models = days[day]
        breakdowns = [{"modelName": name, **{k: (round(v, 10) if k == "cost" else int(v)) for k, v in values.items()}}
                      for name, values in sorted(models.items())]
        row: dict[str, Any] = {"date": day}
        for name in classes:
            row[name] = sum(int(m[name]) for m in models.values())
        row["totalTokens"] = sum(row[name] for name in classes)
        row["totalCost"] = round(sum(m["cost"] for m in models.values()), 10)
        row["modelsUsed"] = sorted(models)
        row["modelBreakdowns"] = breakdowns
        daily.append(row)
        for name in classes:
            totals[name] += row[name]
        totals["totalTokens"] += row["totalTokens"]
        totals["totalCost"] += row["totalCost"]
    totals["totalCost"] = round(totals["totalCost"], 10)
    return {"daily": daily, "totals": totals, "source": f"subbench {provider} logs"}


def hourly_buckets(entries: Iterable[Entry], prices: Prices) -> dict[tuple[str, str], dict[str, float]]:
    """Spend per (UTC hour, model): the same tokens and prices as the daily report, finer."""
    buckets: dict[tuple[str, str], dict[str, float]] = {}
    for entry in entries:
        cost = entry.cost if entry.cost is not None else prices.cost(entry.model, entry.tokens)
        bucket = buckets.setdefault((entry.hour, entry.model), {
            "input_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
            "reasoning_output_tokens": 0, "requests": 0, "cost_usd": 0.0})
        bucket["input_tokens"] += entry.tokens.input
        bucket["cache_write_tokens"] += entry.tokens.cache_write_5m + entry.tokens.cache_write_1h
        bucket["cache_read_tokens"] += entry.tokens.cache_read
        bucket["output_tokens"] += entry.tokens.output
        bucket["reasoning_output_tokens"] += entry.reasoning
        bucket["requests"] += 1
        bucket["cost_usd"] += cost
    for bucket in buckets.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 10)
    return buckets


def raw(report: dict[str, Any]) -> bytes:
    """Stable bytes for a report, so an unchanged report is recognised as the same import."""
    return json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
