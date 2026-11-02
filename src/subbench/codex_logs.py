"""Codex rollouts read the way ccusage 20 reads them (rust/adapters/codex).

Each rule below is ccusage's, so a day read here matches the day ccusage reported for the
same logs; tests/test_codex_logs.py holds the cases, and `packaging/prices/README.md`
describes the comparison against the ccusage binary itself.

Files
  $CODEX_HOME/sessions and archived_sessions (CODEX_HOME may list several homes, comma
  separated; a home with neither directory is read whole). Rollouts are `.jsonl` or
  zstd-compressed `.jsonl.zst`. A plain and compressed copy in one directory: the plain one
  is read. The same rollout under sessions/ and archived_sessions/ of one home: sessions/.

Usage events
  `event_msg` / `token_count`: `last_token_usage` when the cumulative total moved, else the
  change in `total_token_usage`. Field spellings vary (input_tokens / prompt_tokens / input,
  cached_input_tokens / cache_read_input_tokens / cached_tokens, ...); cached tokens are
  capped at input, and a missing total is input plus output.
  `turn_context` sets the model; usage before any model is priced as gpt-5 (a fallback).
  `codex-auto-review` is priced as the model it routed to on that date.
  `thread_settings_applied` sets the service tier usage is recorded under.
  Compactions: a `token_usage_record` counts once its `compacted` marker is seen (either
  order), unless a local compaction's own token_count already covered it.
  Headless `codex exec --json` lines carry `usage` directly.

Forks and subagents
  A rollout whose session_meta names a parent (forked_from_id, or a subagent's
  parent_thread_id) starts by replaying the parent's history. Leading usage equal to the
  parent's usage up to the fork instant is skipped; when the parent cannot anchor it, the
  burst written within a second at the fork instant is skipped instead.

Across files
  An event seen twice (the same response id, or the same instant, model and tokens) counts
  once; its service tier is merged across the copies.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .prices import Bucket, Prices, TieredBucket, codex_cost

FALLBACK_MODEL = "gpt-5"
AUTO_REVIEW = "codex-auto-review"
# ccusage's codex-auto-review-fallbacks.json: the model behind the alias from each date.
AUTO_REVIEW_FALLBACKS = (
    ("2026-07-30", "gpt-5.6-luna"), ("2026-03-05", "gpt-5.4"), ("2026-02-05", "gpt-5.3-codex"),
    ("2025-12-11", "gpt-5.2-codex"), ("2025-11-13", "gpt-5.1-codex"), ("2025-09-15", "gpt-5-codex"),
    ("2025-08-07", "gpt-5"),
)
BURST_PAUSE_MS = 1000
TIER_NAMES = {"default": "standard", "standard": "standard", "fast": "fast", "priority": "fast",
              "flex": "flex", "ultrafast": "ultrafast"}
# ccusage merges copies conservatively: Standard over Ultrafast over Fast over Flex.
_TIER_RANK = ("standard", "ultrafast", "fast", "flex")


# ---------------------------------------------------------------------------- files

def homes() -> list[Path]:
    raw = os.environ.get("CODEX_HOME")
    if raw is not None:
        return [Path(part.strip()).expanduser() for part in raw.split(",") if part.strip()]
    return [Path.home() / ".codex"]


@dataclass(frozen=True)
class Source:
    dir: Path
    scope: Path


def sources() -> list[Source]:
    found: list[Source] = []
    seen: set[Path] = set()
    for home in homes():
        usage = False
        for name in ("sessions", "archived_sessions"):
            path = home / name
            if path.is_dir():
                if path not in seen:
                    seen.add(path)
                    found.append(Source(path, home))
                usage = True
        if not usage and home not in seen:
            seen.add(home)
            found.append(Source(home, home))
    return found


def is_rollout(path: Path) -> bool:
    name = path.name
    return name.endswith(".jsonl") or name.endswith(".jsonl.zst")


def plain_path(path: Path) -> Path:
    return path.with_name(path.name[:-4]) if path.name.endswith(".jsonl.zst") else path


def _collect(directory: Path) -> list[Path]:
    files: list[Path] = []
    for root, _dirs, names in os.walk(directory):
        for name in names:
            path = Path(root) / name
            if is_rollout(path) and path.is_file():
                files.append(path)
    files.sort(key=lambda p: str(p))
    seen: set[Path] = set()
    kept = []
    for path in files:  # x.jsonl sorts before x.jsonl.zst, so the plain copy wins
        key = plain_path(path)
        if key not in seen:
            seen.add(key)
            kept.append(path)
    return kept


def discover() -> list[tuple[Path, Path]]:
    """(sessions dir, file) for every rollout to read, in ccusage's order and precedence."""
    seen: set[tuple[Path, Path]] = set()
    out: list[tuple[Path, Path]] = []
    for source in sources():
        for path in _collect(source.dir):
            try:
                relative = path.relative_to(source.dir)
            except ValueError:
                relative = path
            key = (source.scope, plain_path(relative))
            if key not in seen:
                seen.add(key)
                out.append((source.dir, path))
    return out


def _decompress(data: bytes) -> bytes:
    try:
        from compression import zstd  # Python 3.14

        return zstd.decompress(data)
    except ImportError:
        pass
    import zstandard

    with zstandard.ZstdDecompressor().stream_reader(data) as reader:
        return reader.read()


def read_bytes(path: Path) -> tuple[bytes, Path]:
    """The rollout's text, decompressed; Codex may (de)compress it between listing and reading."""
    try:
        data = path.read_bytes()
        actual = path
    except FileNotFoundError:
        actual = plain_path(path) if path.name.endswith(".zst") else path.with_name(path.name + ".zst")
        data = actual.read_bytes()
    if actual.name.endswith(".jsonl.zst"):
        data = _decompress(data)
    return data, actual


# ---------------------------------------------------------------------------- values

def parse_ms(text: Any) -> int | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(round(moment.timestamp() * 1000))


def format_ms(ms: int) -> str:
    moment = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def _epoch_text(raw: Any) -> str | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return format_ms(raw if raw > 10_000_000_000 else raw * 1000)


def normalize_timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        ms = parse_ms(value)
        return format_ms(ms) if ms is not None else None
    return _epoch_text(value)


def session_timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return _epoch_text(value)


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _valid_date(text: str) -> str | None:
    if not _DATE.match(text):
        return None
    try:
        datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return text[:10]


def raw_or_normalized(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _valid_date(text):
            return text
        return normalize_timestamp(text)
    return normalize_timestamp(value)


def _u64(value: Any) -> int | None:
    """serde's lossy u64: a non-negative integer, a whole float, or a numeric string."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None
    if isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            return None
        return number if number >= 0 else None
    return None


Usage = tuple[int, int, int, int, int, int]  # input, cached, creation, output, reasoning, total


def raw_usage(value: Any) -> Usage | None:
    if not isinstance(value, dict):
        return None
    get = lambda *names: next((v for v in (_u64(value.get(n)) for n in names) if v is not None), None)  # noqa: E731
    inp = get("input_tokens", "prompt_tokens", "input") or 0
    out = get("output_tokens", "completion_tokens", "output") or 0
    reasoning = get("reasoning_output_tokens", "reasoning_tokens") or 0
    cached = min(get("cached_input_tokens", "cache_read_input_tokens", "cached_tokens") or 0, inp)
    creation = min(get("cache_write_input_tokens", "cache_creation_input_tokens") or 0, max(0, inp - cached))
    total = _u64(value.get("total_tokens"))
    if not total:
        total = inp + out
    return (inp, cached, creation, out, reasoning, total)


def normalize(usage: Usage) -> Usage:
    inp, cached, creation, out, reasoning, total = usage
    cached = min(cached, inp)
    creation = min(creation, max(0, inp - cached))
    return (inp, cached, creation, out, reasoning, total)


def has_tokens(usage: Usage) -> bool:
    return any(usage[:5])


def subtract(current: Usage, previous: Usage | None) -> Usage:
    if previous is None:
        return normalize(current)
    return normalize(tuple(max(0, a - b) for a, b in zip(current, previous)))  # type: ignore[arg-type]


def model_from(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("model", "model_name"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            return text.strip()
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        text = metadata.get("model")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def auto_review_model(timestamp: str) -> str:
    day = _valid_date(timestamp)
    if day is None:
        return FALLBACK_MODEL
    return next((model for released, model in AUTO_REVIEW_FALLBACKS if day >= released), FALLBACK_MODEL)


def merge_tiers(current: str | None, incoming: str | None) -> str | None:
    for tier in _TIER_RANK:
        if current == tier or incoming == tier:
            return tier
    return None


# ---------------------------------------------------------------------------- lines

_TYPE_VALUE = re.compile(rb'"type"\s*:\s*"([^"\\]*)"')


def line_kind(line: bytes) -> str | None:
    """ccusage `codex_line_usage_kind`: "session", "headless", or None (not usage)."""
    has_event_msg = b'"type":"event_msg"' in line
    if (b'"type":"turn_context"' in line
            or (has_event_msg and (b'"type":"token_count"' in line or b'"type":"thread_settings_applied"' in line))
            or b'"type":"token_usage_record"' in line or b'"type":"compacted"' in line):
        return "session"
    has_compact_type = b'"type":' in line
    nested_token = not has_event_msg and has_compact_type and len(line) < 65536 and b"token_count" in line
    nested_settings = not has_event_msg and has_compact_type and len(line) < 65536 and b"thread_settings_applied" in line
    compaction = b"token_usage_record" in line or b"compacted" in line
    if has_event_msg or b"turn_context" in line or nested_token or nested_settings or compaction or not has_compact_type:
        types = {m.group(1) for m in _TYPE_VALUE.finditer(line)}
        if (b"turn_context" in types or b"token_usage_record" in types or b"compacted" in types
                or (b"event_msg" in types and (b"token_count" in types or b"thread_settings_applied" in types))):
            return "session"
    if b'"usage":' in line or b'"input_tokens":' in line or b'"prompt_tokens":' in line:
        return "headless"
    return None


# ---------------------------------------------------------------------------- one file

@dataclass
class Event:
    response_id: str | None
    timestamp: str
    model: str
    usage: Usage
    fallback: bool
    tier: str | None


@dataclass
class _Pending:
    event: Event
    thread_usage: Usage | None


@dataclass
class FileParse:
    """Everything one rollout yields, kept so a growing file is only read from where it stopped."""
    mtime_ns: int = 0
    size: int = 0
    offset: int = 0
    inode: int = 0
    compressed: bool = False
    fallback_timestamp: str = ""
    events: list[Event] = field(default_factory=list)
    session_id: str | None = None
    parent_id: str | None = None
    started_ms: int | None = None
    meta_read: bool = False
    previous_totals: Usage | None = None
    model: str | None = None
    model_fallback: bool = False
    tier: str | None = None
    compacted_ids: set[str] = field(default_factory=set)
    pending: dict[str, _Pending] = field(default_factory=dict)
    emitted_ids: set[str] = field(default_factory=set)
    latest_record_id: str | None = None
    recorded_ms: dict[str, int | None] = field(default_factory=dict)

    def compactions(self) -> dict[str, int | None]:
        """Matched compaction ids and when they were requested, for forks of this rollout."""
        return {rid: ms for rid, ms in self.recorded_ms.items() if rid in self.compacted_ids}

    # -- model resolution, as resolve_codex_usage_model
    def _resolve(self, parsed: str | None, timestamp: str, *, scratch: bool = False) -> tuple[str, bool]:
        model, is_fallback = self.model, self.model_fallback
        if parsed is not None:
            model, is_fallback = parsed, False
        fallback = False
        chosen = parsed if parsed is not None else model
        if chosen is None:
            fallback = True
            is_fallback = True
            model = FALLBACK_MODEL
            chosen = model
        if chosen is not None and model is not None and is_fallback:
            fallback = True
        if chosen == AUTO_REVIEW:
            chosen = auto_review_model(timestamp)
            fallback = True
        if not scratch:
            self.model, self.model_fallback = model, is_fallback
        return chosen, fallback

    def feed(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else None
        if kind == "compacted":
            rid = payload.get("compaction_response_id") if payload else None
            rid = rid.strip() if isinstance(rid, str) else ""
            if not rid:
                return
            self.compacted_ids.add(rid)
            pending = self.pending.pop(rid, None)
            if pending is not None and rid not in self.emitted_ids:
                self.emitted_ids.add(rid)
                self.events.append(pending.event)
            return
        if kind == "token_usage_record":
            if payload is None:
                return
            rid = payload.get("response_id")
            rid = rid.strip() if isinstance(rid, str) else ""
            if not rid or rid in self.emitted_ids:
                return
            timestamp = session_timestamp(record.get("timestamp"))
            if timestamp is None:
                return
            usage = raw_usage(payload.get("usage"))
            if usage is None or not any(usage):
                return
            self.recorded_ms.setdefault(rid, parse_ms(timestamp))
            parsed = model_from(payload)
            # A request record never changes the surrounding turn's model.
            model, is_fallback = self._resolve(parsed, timestamp, scratch=True)
            event = Event(rid, timestamp, model, usage, is_fallback or parsed is None, self.tier)
            self.latest_record_id = rid
            if rid in self.compacted_ids:
                if rid not in self.emitted_ids:
                    self.emitted_ids.add(rid)
                    self.events.append(event)
            elif rid not in self.pending:
                thread = raw_usage(payload.get("thread_token_usage"))
                self.pending[rid] = _Pending(event, normalize(thread) if thread else None)
            return
        if kind == "turn_context":
            model = model_from(payload)
            if model is not None:
                self.model, self.model_fallback = model, False
            return
        if kind != "event_msg" or payload is None:
            return
        timestamp = session_timestamp(record.get("timestamp"))
        if timestamp is None:
            return
        ptype = payload.get("type")
        if ptype == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict) and isinstance(settings.get("service_tier"), str):
                self.tier = TIER_NAMES.get(settings["service_tier"])
            return
        if ptype != "token_count":
            return
        info = payload.get("info") if isinstance(payload.get("info"), dict) else None
        total = raw_usage(info.get("total_token_usage")) if info else None
        last = raw_usage(info.get("last_token_usage")) if info else None
        advanced = total is None or self.previous_totals != total
        if last is not None and advanced:
            delta: Usage | None = last
        elif total is not None:
            delta = subtract(total, self.previous_totals)
        else:
            delta = None
        if total is not None:
            self.previous_totals = total
        if delta is None:
            return
        delta = normalize(delta)
        if not has_tokens(delta):
            return
        rid = self.latest_record_id
        if advanced and rid is not None and rid in self.pending:
            pending = self.pending[rid]
            if pending.event.usage == delta or (pending.thread_usage is not None and total is not None
                                                 and pending.thread_usage == normalize(total)):
                del self.pending[rid]
                self.emitted_ids.add(rid)
        parsed = model_from(payload) or (model_from(info) if info else None)
        model, is_fallback = self._resolve(parsed, timestamp)
        self.events.append(Event(None, timestamp, model, delta, is_fallback, self.tier))

    def feed_headless(self, record: Any) -> None:
        if not isinstance(record, dict):
            return
        usage = None
        for holder in (record, record.get("data"), record.get("result"), record.get("response")):
            if isinstance(holder, dict) and isinstance(holder.get("usage"), dict):
                usage = raw_usage(holder["usage"])
                if usage is not None:
                    break
        if usage is None:
            return
        usage = normalize(usage)
        if not any(usage):
            return
        holders = [h for h in (record, record.get("data"), record.get("result"), record.get("response")) if isinstance(h, dict)]
        parsed = next((m for m in (model_from(h) for h in holders) if m), None)

        def first(convert: Callable[[Any], str | None]) -> str | None:
            for holder in holders:
                for key in ("timestamp", "created_at", "createdAt"):
                    found = convert(holder.get(key))
                    if found:
                        return found
            return None

        event_ts = first(normalize_timestamp) or self.fallback_timestamp
        model_ts = first(raw_or_normalized) or self.fallback_timestamp
        model, is_fallback = self._resolve(parsed, model_ts)
        self.events.append(Event(None, event_ts, model, usage, is_fallback, None))

    def read_meta(self, first_line: bytes) -> None:
        self.meta_read = True
        try:
            value = json.loads(first_line)
        except ValueError:
            return
        if not isinstance(value, dict):
            return
        self.started_ms = parse_ms(normalize_timestamp(value.get("timestamp")))
        payload = value.get("payload") if value.get("type") == "session_meta" else None
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("id"), str):
            self.session_id = payload["id"]
        parent = payload.get("forked_from_id")
        if not isinstance(parent, str):
            source = payload.get("source")
            parent = None
            if isinstance(source, dict):
                sub = source.get("subagent")
                spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
                if isinstance(spawn, dict) and isinstance(spawn.get("parent_thread_id"), str):
                    parent = spawn["parent_thread_id"]
        self.parent_id = parent or None


def _lines(chunk: bytes) -> Iterator[bytes]:
    for line in chunk.split(b"\n"):
        if line.endswith(b"\r"):
            line = line[:-1]
        if line:
            yield line


def _parse_lines(state: FileParse, chunk: bytes) -> None:
    for line in _lines(chunk):
        kind = line_kind(line)
        if kind is None:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if kind == "session":
            if isinstance(record, dict):
                state.feed(record)
        else:
            state.feed_headless(record)


def _mtime_text(path: Path) -> str:
    try:
        return format_ms(int(path.stat().st_mtime * 1000))
    except OSError:
        return format_ms(0)


def detect_burst(data: bytes) -> int | None:
    """ccusage `detect_rewritten_burst`: where a dense replayed burst at the fork instant starts."""
    first: int | None = None
    started: int | None = None
    for line in _lines(data):
        if b"session_meta" in line:
            try:
                value = json.loads(line)
            except ValueError:
                value = None
            if isinstance(value, dict) and value.get("type") == "session_meta":
                if started is None:
                    started = parse_ms(session_timestamp(value.get("timestamp")))
                continue
        if line_kind(line) != "session":
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("type") != "event_msg":
            continue
        payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
        if payload.get("type") != "token_count" or not isinstance(payload.get("info"), dict):
            continue
        timestamp = session_timestamp(value.get("timestamp"))
        if timestamp is None:
            continue
        info = payload["info"]
        usage = raw_usage(info.get("last_token_usage")) if isinstance(info.get("last_token_usage"), dict) else None
        if usage is None:
            usage = raw_usage(info.get("total_token_usage"))
        if usage is None or not has_tokens(normalize(usage)):
            continue
        ms = parse_ms(timestamp)
        if ms is None:
            continue
        if first is None:
            first = ms
            continue
        dense = 0 <= ms - first <= BURST_PAUSE_MS
        at_fork = started is None or abs(first - started) <= BURST_PAUSE_MS
        return first if dense and at_fork else None
    return None


# ---------------------------------------------------------------------------- all files

@dataclass
class Counted:
    event: Event
    ms: int


class CodexReader:
    def __init__(self) -> None:
        self._files: dict[Path, FileParse] = {}
        self._bursts: dict[Path, tuple[int, int, int | None]] = {}
        self._last: tuple[tuple, list[Counted]] | None = None

    def _parse(self, path: Path) -> FileParse | None:
        try:
            stat = path.stat()
        except OSError:
            self._files.pop(path, None)
            return None
        state = self._files.get(path)
        compressed = path.name.endswith(".zst")
        if compressed:
            if state is not None and state.mtime_ns == stat.st_mtime_ns and state.size == stat.st_size:
                return state
            try:
                data, actual = read_bytes(path)
            except (OSError, Exception):  # noqa: BLE001 - an unreadable archive counts nothing
                return state
            state = FileParse(mtime_ns=stat.st_mtime_ns, size=stat.st_size, compressed=True,
                              fallback_timestamp=_mtime_text(actual))
            head = data.split(b"\n", 1)[0]
            state.read_meta(head)
            _parse_lines(state, data)
            self._files[path] = state
            return state
        if state is None or state.inode != stat.st_ino or stat.st_size < state.offset:
            state = FileParse(inode=stat.st_ino)
            self._files[path] = state
        state.fallback_timestamp = _mtime_text(path)
        state.mtime_ns, state.size = stat.st_mtime_ns, stat.st_size
        if stat.st_size > state.offset:
            try:
                with path.open("rb") as handle:
                    handle.seek(state.offset)
                    chunk = handle.read(stat.st_size - state.offset)
            except OSError:
                return state
            end = chunk.rfind(b"\n") + 1
            if not state.meta_read and end:
                state.read_meta(chunk.split(b"\n", 1)[0])
            _parse_lines(state, chunk[:end])
            state.offset += end
        return state

    def _burst(self, path: Path) -> int | None:
        state = self._files.get(path)
        stamp = (state.size, state.mtime_ns) if state is not None else (-1, -1)
        cached = self._bursts.get(path)
        if cached is not None and cached[:2] == stamp:
            return cached[2]
        try:
            found = detect_burst(read_bytes(path)[0])
        except Exception:  # noqa: BLE001 - an unreadable file anchors nothing
            found = None
        self._bursts[path] = (*stamp, found)
        return found

    def events(self) -> list[Counted]:
        files = discover()
        paths = [path for _, path in files]
        for gone in set(self._files) - set(paths):
            self._files.pop(gone, None)
            self._bursts.pop(gone, None)
        parsed = {path: self._parse(path) for path in paths}
        signature = tuple((path, state.size, state.mtime_ns, len(state.events)) if state else (path,)
                          for path, state in parsed.items())
        if self._last is not None and self._last[0] == signature:
            return [Counted(Event(c.event.response_id, c.event.timestamp, c.event.model, c.event.usage,
                                  c.event.fallback, c.event.tier), c.ms) for c in self._last[1]]
        by_session: dict[str, list[Path]] = {}
        for path in paths:
            state = parsed[path]
            if state is not None and state.session_id:
                by_session.setdefault(state.session_id, []).append(path)

        counted: list[Counted] = []
        records: dict[Any, dict[str, Any]] = {}
        for index, path in enumerate(paths):
            state = parsed[path]
            if state is None:
                continue
            for event in self._own_events(path, state, parsed, by_session):
                ms = parse_ms(event.timestamp)
                if ms is None:
                    continue
                key: Any = ("response", event.response_id) if event.response_id else ("usage", ms, event.model, event.usage)
                record = records.get(key)
                if record is not None:
                    record["tier"] = merge_tiers(record["tier"], event.tier)
                    if event.response_id and index < record["index"]:
                        record.update(index=index, event=event, ms=ms)
                    continue
                records[key] = {"tier": event.tier, "index": index, "event": event, "ms": ms,
                                "position": len(counted) if not event.response_id else None}
                if not event.response_id:
                    counted.append(Counted(event, ms))
        # Copies agree on tokens; the tier is what all of them said together. Compactions are
        # counted from the first file that carried them.
        for key, record in records.items():
            if record["position"] is not None:
                counted[record["position"]].event.tier = record["tier"]
            else:
                event = record["event"]
                counted.append(Counted(Event(event.response_id, event.timestamp, event.model, event.usage,
                                             event.fallback, record["tier"]), record["ms"]))
        self._last = (signature, counted)
        return [Counted(Event(c.event.response_id, c.event.timestamp, c.event.model, c.event.usage,
                              c.event.fallback, c.event.tier), c.ms) for c in counted]

    def _own_events(self, path: Path, state: FileParse, parsed: dict[Path, FileParse | None],
                    by_session: dict[str, list[Path]]) -> Iterable[Event]:
        events = [Event(e.response_id, e.timestamp, e.model, e.usage, e.fallback, e.tier) for e in state.events]
        if state.parent_id is None:
            return events
        parent_path = next((p for p in by_session.get(state.parent_id, []) if p != path), None)
        parent = parsed.get(parent_path) if parent_path else None
        if parent is not None:
            stream = [e for e in parent.events if e.response_id is None]
            if state.started_ms is not None:
                cut = next((i for i, e in enumerate(stream)
                            if (ms := parse_ms(e.timestamp)) is not None and ms > state.started_ms), len(stream))
                stream = stream[:cut]
            prefix = [e.usage for e in stream]
            compactions = parent.compactions()
        else:
            prefix, compactions = [], {}
        kept: list[Event] = []
        mode, index, previous = "match", 0, 0
        for event in events:
            if event.response_id is not None:
                stamp = compactions.get(event.response_id, "absent")
                if stamp != "absent" and (state.started_ms is None or stamp is None or stamp <= state.started_ms):
                    continue  # the parent's compaction, copied with the history
                kept.append(event)
                continue
            while True:
                if mode == "match":
                    if index < len(prefix) and prefix[index] == event.usage:
                        index += 1
                        break
                    burst = self._burst(path) if index == 0 else None
                    if burst is not None:
                        mode, previous = "burst", burst
                    else:
                        mode = "done"
                    continue
                if mode == "burst":
                    ms = parse_ms(event.timestamp)
                    if ms is not None and 0 <= ms - previous <= BURST_PAUSE_MS:
                        previous = ms
                        break
                    mode = "done"
                    continue
                kept.append(event)
                break
        return kept


# ---------------------------------------------------------------------------- reports

def config_tier() -> str:
    """The tier unclassified usage is priced at: Codex config.toml's service_tier (ccusage auto)."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11 ships it
        return "standard"
    tiers = []
    for home in homes():
        try:
            config = tomllib.loads((home / "config.toml").read_text())
        except (OSError, ValueError):
            continue
        tier = None
        profile = config.get("profile")
        if isinstance(profile, str) and isinstance(config.get("profiles"), dict):
            selected = config["profiles"].get(profile)
            if isinstance(selected, dict):
                tier = selected.get("service_tier")
        if tier is None:
            tier = config.get("service_tier")
        if isinstance(tier, str) and tier in ("default", "standard", "fast", "priority", "flex"):
            tiers.append(TIER_NAMES[tier])
    if "fast" in tiers:
        return "fast"
    if "flex" in tiers:
        return "flex"
    return "standard"


@dataclass
class ModelDay:
    tokens: TieredBucket = field(default_factory=TieredBucket)
    reasoning: int = 0
    total: int = 0
    requests: int = 0
    fallback: bool = False


def _local_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().date().isoformat()


def _utc_hour(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def group(events: Iterable[Counted], prices: Prices, *, by: Callable[[int], str]) -> dict[tuple[str, str], ModelDay]:
    groups: dict[tuple[str, str], ModelDay] = {}
    for item in events:
        event = item.event
        rate = prices.rate(event.model)
        split = rate.split_threshold if rate is not None else 200_000
        inp, cached, creation, out, reasoning, total = event.usage
        long = inp > split
        cell = groups.setdefault((by(item.ms), event.model), ModelDay())
        cell.tokens.total.add(inp, cached, creation, out, long)
        if event.tier is not None:
            cell.tokens.recorded[event.tier].add(inp, cached, creation, out, long)
        cell.reasoning += reasoning
        cell.total += total
        cell.requests += 1
        cell.fallback |= event.fallback
    return groups


def cell_cost(model: str, cell: ModelDay, prices: Prices, tier: str) -> float:
    rate = prices.rate(model)
    return codex_cost(cell.tokens, rate, default_tier=tier) if rate is not None else 0.0


def daily(events: Iterable[Counted], prices: Prices, *, tier: str | None = None) -> dict[str, Any]:
    """The `ccusage codex daily --json` report, in the per-model breakdown shape SubBench stores."""
    tier = config_tier() if tier is None else tier
    cells = group(events, prices, by=_local_day)
    days: dict[str, list[dict[str, Any]]] = {}
    for (day, model), cell in sorted(cells.items()):
        t = cell.tokens.total
        days.setdefault(day, []).append({
            "modelName": model,
            "inputTokens": max(0, t.input - (t.cached + t.creation)),
            "outputTokens": t.output,
            "cacheCreationTokens": t.creation,
            "cacheReadTokens": t.cached,
            "reasoningOutputTokens": cell.reasoning,
            "totalTokens": cell.total,
            "cost": round(cell_cost(model, cell, prices, tier), 10),
            "isFallback": cell.fallback,
        })
    report = []
    totals = {"inputTokens": 0, "outputTokens": 0, "cacheCreationTokens": 0, "cacheReadTokens": 0,
              "reasoningOutputTokens": 0, "totalTokens": 0, "totalCost": 0.0}
    for day in sorted(days):
        models = days[day]
        row: dict[str, Any] = {"date": day}
        for name in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens",
                     "reasoningOutputTokens", "totalTokens"):
            row[name] = sum(int(m[name]) for m in models)
            totals[name] += row[name]
        row["totalCost"] = round(sum(m["cost"] for m in models), 10)
        totals["totalCost"] += row["totalCost"]
        row["modelsUsed"] = [m["modelName"] for m in models]
        row["modelBreakdowns"] = models
        report.append(row)
    totals["totalCost"] = round(totals["totalCost"], 10)
    return {"daily": report, "totals": totals, "source": "subbench codex logs"}


def hourly(events: Iterable[Counted], prices: Prices, *, tier: str | None = None) -> dict[tuple[str, str], dict[str, float]]:
    tier = config_tier() if tier is None else tier
    out: dict[tuple[str, str], dict[str, float]] = {}
    for (hour, model), cell in group(events, prices, by=_utc_hour).items():
        t = cell.tokens.total
        out[(hour, model)] = {
            "input_tokens": max(0, t.input - (t.cached + t.creation)),
            "cache_write_tokens": t.creation,
            "cache_read_tokens": t.cached,
            "output_tokens": t.output,
            "reasoning_output_tokens": cell.reasoning,
            "requests": cell.requests,
            "cost_usd": round(cell_cost(model, cell, prices, tier), 10),
        }
    return out
