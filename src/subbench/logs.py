"""Read Claude Code and Codex session logs directly, in place of ccusage.

Both readers follow ccusage 20 rule for rule (its rust/adapters/claude and
rust/adapters/codex), so a day read here is the day ccusage reported for the same logs,
and a database that switched from ccusage keeps one continuous spend series. The report
has ccusage's daily shape (the same keys and per-model breakdown), so everything recorded
before the switch reads the same way.

Claude Code ($CLAUDE_CONFIG_DIR, else $XDG_CONFIG_HOME/claude and ~/.claude; */projects/**/*.jsonl):
  - any line carrying `"usage":{` whose message has a usage: assistant replies, and
    subagent progress lines that wrap one (data.message.message);
  - a line with a malformed version, an empty session, request or message id, or a null
    in a field ccusage requires, is skipped;
  - the same response logged twice counts once: the same message and request id; without
    a request id, the same message in the same session (and instant, for non-Anthropic
    ids); and a subagent (sidechain) replay of a message in its session. The copy kept is
    the main-thread one, then the one with more tokens, then the costlier one;
  - `costUSD` recorded in the log wins over a computed cost; `speed: "fast"` pays the
    model's Fast multiplier and is reported as `<model>-fast`;
  - advisor iterations inside a reply (usage.iterations, type advisor_message) count as
    requests of their own model.

Codex: see codex_logs.

Reading is incremental. A file that has only grown is read from where the last pass
stopped; a file that shrank or was replaced is read again from the start.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import codex_logs
from .prices import Prices, Tokens, default_prices

CODEX_FALLBACK_MODEL = codex_logs.FALLBACK_MODEL


def claude_bases() -> list[Path]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured is not None and configured.strip():
        bases = []
        for part in configured.split(","):
            if not part.strip():
                continue
            path = Path(part.strip()).expanduser()
            if path.name == "projects" and path.is_dir():
                path = path.parent
            bases.append(path)
        return bases
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return [config / "claude", Path.home() / ".claude"]


def claude_roots() -> list[Path]:
    seen: list[Path] = []
    for base in claude_bases():
        if base not in seen:
            seen.append(base)
    return [base / "projects" for base in seen]


def codex_roots() -> list[Path]:
    roots: list[Path] = []
    for home in codex_logs.homes():
        roots.extend([home / "sessions", home / "archived_sessions"])
    return roots


def roots(provider: str) -> list[Path]:
    if provider == "claude":
        return claude_roots()
    if provider == "codex":
        return codex_roots()
    raise ValueError(f"unsupported provider: {provider}")


def discover(provider: str) -> list[Path]:
    if provider == "codex":
        return [path for _, path in codex_logs.discover()]
    files: list[Path] = []
    seen: set[Path] = set()
    for root in claude_roots():
        if root.is_dir():
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    if name.endswith(".jsonl"):
                        path = Path(dirpath) / name
                        if path.is_file() and path not in seen:
                            seen.add(path)
                            files.append(path)
    files.sort(key=lambda p: str(p))
    return files


@dataclass
class Entry:
    day: str
    model: str | None  # as reported: "-fast" appended for fast requests; None for <synthetic>
    tokens: Tokens
    reasoning: int = 0
    hour: str = ""  # the UTC hour of the request, e.g. 2026-11-02T14:00:00+00:00
    cost: float = 0.0
    requests: int = 1
    # Claude de-duplication
    ms: int = 0
    message_id: str | None = None
    request_id: str | None = None
    session_id: str = ""
    sidechain: bool = False
    has_speed: bool = False

    @property
    def token_total(self) -> int:
        t = self.tokens
        return t.input + t.output + t.cache_write_5m + t.cache_write_1h + t.cache_read


def _when(ms: int) -> tuple[str, str]:
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return moment.astimezone().date().isoformat(), moment.replace(minute=0, second=0, microsecond=0).isoformat()


_NOT_NULL = {"id", "cwd", "model", "speed", "costUSD", "version", "sessionId", "requestId",
             "isApiErrorMessage", "cache_read_input_tokens", "cache_creation_input_tokens"}
_SEMVER = re.compile(r"^\d+\.\d+\.\d")


def _has_bad_null(value: Any, iteration_arrays: list[Any], allow_model: bool = False) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if child is None and key in _NOT_NULL:
                if not (allow_model and key == "model"):
                    return True
                continue
            if _has_bad_null(child, iteration_arrays):
                return True
        return False
    if isinstance(value, list):
        inside = any(value is array for array in iteration_arrays)
        return any(_has_bad_null(child, iteration_arrays, inside) for child in value)
    return False


def _u64(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True)
class _Usage:
    input: int
    output: int
    cache_creation: int
    cache_read: int
    five_minute: int
    one_hour: int
    split: bool
    speed: str | None


def _usage(value: Any) -> _Usage | None:
    """ccusage's TokenUsageRaw: input and output required, the rest optional."""
    if not isinstance(value, dict):
        return None
    inp, out = _u64(value.get("input_tokens")), _u64(value.get("output_tokens"))
    if inp is None or out is None:
        return None
    creation = value.get("cache_creation_input_tokens", 0)
    read = value.get("cache_read_input_tokens", 0)
    if _u64(creation) is None or _u64(read) is None:
        return None
    speed = value.get("speed")
    if speed is not None and speed not in ("standard", "fast"):
        return None
    split = value.get("cache_creation")
    five = one = 0
    if split is not None:
        if not isinstance(split, dict):
            return None
        five, one = split.get("ephemeral_5m_input_tokens", 0), split.get("ephemeral_1h_input_tokens", 0)
        if _u64(five) is None or _u64(one) is None:
            return None
    return _Usage(inp, out, creation, read, five, one, split is not None, speed)


def _tokens(usage: _Usage) -> Tokens:
    if usage.split:
        return Tokens(input=usage.input, cache_write_5m=usage.five_minute, cache_write_1h=usage.one_hour,
                      cache_read=usage.cache_read, output=usage.output)
    return Tokens(input=usage.input, cache_write_5m=usage.cache_creation, cache_read=usage.cache_read, output=usage.output)


def _optional_text(value: Any) -> tuple[bool, str | None]:
    if value is None:
        return True, None
    return isinstance(value, str), value if isinstance(value, str) else None


def _claude_line(record: Any) -> dict[str, Any] | None:
    """A usage line as ccusage's DailyUsageLine: a direct entry, or a subagent progress wrapper."""
    if not isinstance(record, dict):
        return None

    def entry(holder: dict[str, Any], *, session: Any, version: Any) -> dict[str, Any] | None:
        message = holder.get("message")
        if not isinstance(holder.get("timestamp"), str) or not isinstance(message, dict):
            return None
        usage = _usage(message.get("usage"))
        if usage is None:
            return None
        fields = {"model": message.get("model"), "id": message.get("id"), "session": session,
                  "request": holder.get("requestId"), "version": version}
        for value in fields.values():
            if not _optional_text(value)[0]:
                return None
        cost = holder.get("costUSD")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))):
            return None
        sidechain = holder.get("isSidechain")
        if sidechain is not None and not isinstance(sidechain, bool):
            return None
        return {"timestamp": holder["timestamp"], "usage": usage, "cost": cost, "sidechain": sidechain is True, **fields}

    direct = entry(record, session=record.get("sessionId"), version=record.get("version"))
    if direct is not None:
        return direct
    data = record.get("data")
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        return entry(data["message"], session=record.get("sessionId"), version=None)
    return None


def _claude_entries(line: bytes, record: Any, prices: Prices, session_from_path: str) -> list[Entry]:
    if b"null" in line:
        iterations = []
        if isinstance(record, dict):
            for path in (("message", "usage", "iterations"), ("data", "message", "message", "usage", "iterations")):
                node: Any = record
                for key in path:
                    node = node.get(key) if isinstance(node, dict) else None
                if node is not None:
                    iterations.append(node)
        if _has_bad_null(record, iterations):
            return []
    data = _claude_line(record)
    if data is None:
        return []
    ms = codex_logs.parse_ms(data["timestamp"])
    if ms is None:
        return []
    version = data["version"]
    if version is not None and not _SEMVER.match(version):
        return []
    if any(data[key] == "" for key in ("session", "request", "id", "model")):
        return []
    usage: _Usage = data["usage"]
    model = data["model"]
    tokens = _tokens(usage)
    fast = usage.speed == "fast"
    if data["cost"] is not None:
        cost = float(data["cost"])
    else:
        # <synthetic> marks a reply Claude Code wrote itself; it has no price and costs nothing.
        cost = prices.cost(model, tokens, fast=fast) if model and model != "<synthetic>" else 0.0
    shown = None if model is None or model == "<synthetic>" else (f"{model}-fast" if fast else model)
    day, hour = _when(ms)
    session = data["session"] or session_from_path
    out = [Entry(day=day, hour=hour, model=shown, tokens=tokens, cost=cost, ms=ms, message_id=data["id"],
                 request_id=data["request"], session_id=session, sidechain=data["sidechain"],
                 has_speed=usage.speed is not None)]
    if b'"advisor_message"' in line and isinstance(record, dict):
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        usage_value = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        iterations = usage_value.get("iterations", [])
        parsed = []
        if isinstance(iterations, list):
            for item in iterations:
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    parsed = []
                    break
                inner = _usage(item)
                if inner is None or not _optional_text(item.get("model"))[0]:
                    parsed = []
                    break
                parsed.append((item["type"], item.get("model"), inner))
        advisors = [(m, u) for kind, m, u in parsed if kind == "advisor_message" and m]
        for index, (advisor_model, advisor_usage) in enumerate(advisors):
            advisor_tokens = _tokens(advisor_usage)
            out.append(Entry(day=day, hour=hour, model=advisor_model, tokens=advisor_tokens,
                             cost=prices.cost(advisor_model, advisor_tokens, fast=advisor_usage.speed == "fast"),
                             ms=ms, message_id=f"{data['id']}:advisor:{index}" if data["id"] else None,
                             request_id=data["request"], session_id=session, sidechain=data["sidechain"],
                             has_speed=advisor_usage.speed is not None))
    return out


def _session_from_path(path: Path) -> str:
    """ccusage `extract_session_parts`: the session a transcript belongs to, from its path."""
    parts = path.parts
    relative = parts[parts.index("projects") + 1:] if "projects" in parts else parts
    stem = relative[-1][:-len(".jsonl")] if relative and relative[-1].endswith(".jsonl") else ""
    if len(relative) == 2 and stem:
        return stem
    if len(relative) >= 4 and relative[-2] == "subagents":
        return relative[-3]
    return relative[-2] if len(relative) >= 2 else "unknown"


class _ClaudeDedupe:
    """ccusage `push_deduped_daily_entry`, with its keys spelled out instead of hashed.

    Each key holds a list of (position, session alias) in insertion order; a lookup takes
    the first whose *current* survivor still satisfies the key's condition, exactly as
    ccusage re-checks the survivor behind each hash bucket.
    """

    def __init__(self) -> None:
        self.kept: list[Entry] = []
        self.index: dict[tuple, list[tuple[int, str | None]]] = {}

    @staticmethod
    def _exact(entry: Entry) -> tuple:
        mid, rid = entry.message_id, entry.request_id
        if rid is not None:
            return ("exact", mid, rid)
        if mid.startswith(("msg_", "resp_")):
            return ("exact", mid, None, entry.session_id)
        return ("exact", mid, None, entry.session_id, entry.ms)

    def _push(self, key: tuple, position: int, alias: str | None = None, *, new: bool = False) -> None:
        bucket = self.index.setdefault(key, [])
        if new or (position, alias) not in bucket:
            bucket.append((position, alias))

    @staticmethod
    def _better(candidate: Entry, existing: Entry) -> bool:
        if candidate.sidechain != existing.sidechain:
            return existing.sidechain
        if candidate.token_total != existing.token_total:
            return candidate.token_total > existing.token_total
        if candidate.cost != existing.cost:
            return candidate.cost > existing.cost
        return candidate.has_speed and not existing.has_speed

    def _find(self, entry: Entry) -> int | None:
        mid, rid = entry.message_id, entry.request_id
        for position, _alias in self.index.get(self._exact(entry), []):
            existing = self.kept[position]
            if existing.message_id == mid and existing.request_id == rid and (
                    rid is not None or (existing.session_id == entry.session_id
                                        and (mid.startswith(("msg_", "resp_")) or existing.ms == entry.ms))):
                return position
        route = ("replay" if entry.sidechain else "replay-entry", mid, entry.session_id)
        for position, alias in self.index.get(route, []):
            existing = self.kept[position]
            session = alias if alias is not None else existing.session_id
            if existing.message_id == mid and session == entry.session_id and (entry.sidechain or existing.sidechain):
                return position
        return None

    def add(self, entry: Entry) -> None:
        mid = entry.message_id
        if mid is None:
            self.kept.append(entry)
            return
        found = self._find(entry)
        if found is None:
            position = len(self.kept)
            self.kept.append(entry)
            self._push(self._exact(entry), position, new=True)
            self._push(("replay", mid, entry.session_id), position, new=True)
            if entry.sidechain:
                self._push(("replay-entry", mid, entry.session_id), position, new=True)
            return
        existing = self.kept[found]
        if existing.session_id != entry.session_id:
            for session in (entry.session_id, existing.session_id):
                for route in ("replay", "replay-entry"):
                    self._push((route, mid, session), found, session)
        if self._better(entry, existing):
            self.kept[found] = entry
            if existing.message_id is None or self._exact(existing) != self._exact(entry):
                self._push(self._exact(entry), found)
            if (existing.session_id != entry.session_id or existing.sidechain != entry.sidechain
                    or existing.message_id != entry.message_id):
                self._push(("replay", mid, entry.session_id), found)
                if entry.sidechain:
                    self._push(("replay-entry", mid, entry.session_id), found)


@dataclass
class _FileState:
    inode: int
    offset: int
    entries: list[Entry] = field(default_factory=list)


class LogReader:
    """Keeps what it has read, so a long-running collector only parses what was appended."""

    def __init__(self, provider: str, prices: Prices | None = None) -> None:
        if provider not in ("claude", "codex"):
            raise ValueError(f"unsupported provider: {provider}")
        self.provider = provider
        self.prices = prices
        self._files: dict[Path, _FileState] = {}
        self._codex = codex_logs.CodexReader() if provider == "codex" else None

    def _prices(self) -> Prices:
        if self.prices is None:
            self.prices = default_prices()
        return self.prices

    def _read(self, path: Path, prices: Prices) -> list[Entry]:
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
            session = _session_from_path(path)
            for line in chunk[:end].split(b"\n"):
                # Lines that cannot carry usage (prompts, replies, tool output) are skipped
                # unparsed, so their content is never decoded at all.
                if b'"usage":{' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                state.entries.extend(_claude_entries(line, record, prices, session))
            state.offset += end
        return state.entries

    def entries(self) -> list[Entry]:
        prices = self._prices()
        if self._codex is not None:
            return [_codex_entry(item) for item in self._codex.events()]
        paths = discover("claude")
        for gone in set(self._files) - set(paths):
            self._files.pop(gone, None)
        dedupe = _ClaudeDedupe()
        for path in paths:
            for entry in self._read(path, prices):
                dedupe.add(entry)
        return dedupe.kept

    def hourly(self) -> dict[tuple[str, str], dict[str, float]]:
        prices = self._prices()
        if self._codex is not None:
            return codex_logs.hourly(self._codex.events(), prices)
        return hourly_buckets(self.entries(), prices)

    def daily(self, *, since: date | None = None) -> dict[str, Any]:
        """The ccusage `daily --json` report for this provider."""
        prices = self._prices()
        if self._codex is not None:
            report = codex_logs.daily(self._codex.events(), prices)
            if since is not None:
                report["daily"] = [row for row in report["daily"] if row["date"] >= since.isoformat()]
            return report
        return daily_report(self.entries(), prices, provider=self.provider, since=since)


def _codex_entry(item: codex_logs.Counted) -> Entry:
    inp, cached, creation, out, reasoning, _total = item.event.usage
    day, hour = _when(item.ms)
    return Entry(day=day, hour=hour, model=item.event.model, ms=item.ms, reasoning=reasoning,
                 tokens=Tokens(input=max(0, inp - cached - creation), cache_write_5m=creation, cache_read=cached, output=out))


_CLASSES = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens")


def daily_report(entries: Iterable[Entry], prices: Prices, *, provider: str, since: date | None = None) -> dict[str, Any]:
    days: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if since is not None and entry.day < since.isoformat():
            continue
        day = days.setdefault(entry.day, {"counts": {name: 0 for name in _CLASSES}, "cost": 0.0, "models": {}})
        writes = entry.tokens.cache_write_5m + entry.tokens.cache_write_1h
        values = (entry.tokens.input, entry.tokens.output, writes, entry.tokens.cache_read)
        for name, value in zip(_CLASSES, values):
            day["counts"][name] += value
        day["cost"] += entry.cost
        if entry.model is None:
            continue  # <synthetic>: counted in the day, in no model's breakdown
        model = day["models"].setdefault(entry.model, {name: 0 for name in _CLASSES} | {"reasoningOutputTokens": 0, "cost": 0.0})
        for name, value in zip(_CLASSES, values):
            model[name] += value
        model["reasoningOutputTokens"] += entry.reasoning
        model["cost"] += entry.cost

    daily: list[dict[str, Any]] = []
    totals = {name: 0 for name in _CLASSES} | {"totalCost": 0.0, "totalTokens": 0}
    for key in sorted(days):
        day = days[key]
        breakdowns = [{"modelName": name, **{k: (round(v, 10) if k == "cost" else int(v)) for k, v in values.items()}}
                      for name, values in sorted(day["models"].items())]
        row: dict[str, Any] = {"date": key, **day["counts"]}
        row["totalTokens"] = sum(day["counts"].values())
        row["totalCost"] = round(day["cost"], 10)
        row["modelsUsed"] = sorted(day["models"])
        row["modelBreakdowns"] = breakdowns
        daily.append(row)
        for name in _CLASSES:
            totals[name] += row[name]
        totals["totalTokens"] += row["totalTokens"]
        totals["totalCost"] += row["totalCost"]
    totals["totalCost"] = round(totals["totalCost"], 10)
    return {"daily": daily, "totals": totals, "source": f"subbench {provider} logs"}


def hourly_buckets(entries: Iterable[Entry], prices: Prices) -> dict[tuple[str, str], dict[str, float]]:
    """Spend per (UTC hour, model): the same tokens and prices as the daily report, finer."""
    buckets: dict[tuple[str, str], dict[str, float]] = {}
    for entry in entries:
        bucket = buckets.setdefault((entry.hour, entry.model or "<synthetic>"), {
            "input_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "output_tokens": 0,
            "reasoning_output_tokens": 0, "requests": 0, "cost_usd": 0.0})
        bucket["input_tokens"] += entry.tokens.input
        bucket["cache_write_tokens"] += entry.tokens.cache_write_5m + entry.tokens.cache_write_1h
        bucket["cache_read_tokens"] += entry.tokens.cache_read
        bucket["output_tokens"] += entry.tokens.output
        bucket["reasoning_output_tokens"] += entry.reasoning
        bucket["requests"] += entry.requests
        bucket["cost_usd"] += entry.cost
    for bucket in buckets.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 10)
    return buckets


def raw(report: dict[str, Any]) -> bytes:
    """Stable bytes for a report, so an unchanged report is recognised as the same import."""
    return json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
