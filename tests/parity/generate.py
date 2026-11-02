"""Claude Code and Codex logs that exercise every rule ccusage applies, from a seed.

Used by tests/test_parity.py, whose expected values are ccusage's own output for the same
logs (tests/parity/golden; regenerate with tests/parity/record.sh).
"""
import json, random, sys, os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import zstandard
except ImportError:
    zstandard = None


def line(obj):
    return json.dumps(obj, separators=(",", ":"))


def ts(moment, style="z"):
    if style == "num":
        return int(moment.timestamp())
    if style == "ms":
        return int(moment.timestamp() * 1000)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def write(path, lines, compress=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("\n".join(lines) + "\n").encode()
    if compress:
        path = path.with_name(path.name + ".zst")
        data = zstandard.ZstdCompressor().compress(data)
    path.write_bytes(data)


CODEX_MODELS = ["gpt-5.4", "gpt-5.6-luna", "gpt-5.3-codex", "gpt-5", "codex-auto-review", "gpt-6-astra", "gpt-9-unknown", "gpt-5.5"]
TIERS = ["priority", "fast", "flex", "default", "standard", "ultrafast", "bogus"]


def usage(rng, big=False):
    inp = rng.randint(300_000, 400_000) if big else rng.randint(1, 60_000)
    cached = rng.randint(0, inp)
    out = rng.randint(0, 5000)
    reasoning = rng.randint(0, out)
    u = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
         "reasoning_output_tokens": reasoning, "total_tokens": inp + out}
    if rng.random() < 0.05:
        u = {"prompt_tokens": inp, "cached_tokens": cached, "completion_tokens": out}
    if rng.random() < 0.03:
        u["cache_creation_input_tokens"] = rng.randint(0, max(0, inp - cached))
    return u


def add(a, b):
    keys = set(a) | set(b)
    return {k: a.get(k, 0) + b.get(k, 0) for k in keys}


class Rollout:
    def __init__(self, rng, start, session_id, parent=None, subagent=False):
        self.rng, self.t, self.lines, self.events = rng, start, [], []
        meta = {"id": session_id, "cwd": "/w"}
        if parent and not subagent:
            meta["forked_from_id"] = parent
        if parent and subagent:
            meta["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
        self.lines.append(line({"timestamp": ts(start), "type": "session_meta", "payload": meta}))
        self.total = None

    def tick(self, seconds=None):
        self.t += timedelta(seconds=seconds if seconds is not None else self.rng.uniform(1, 400))

    def model(self, name):
        self.tick()
        self.lines.append(line({"timestamp": ts(self.t), "type": "turn_context", "payload": {"cwd": "/w", "model": name}}))

    def tier(self, value):
        self.tick()
        settings = {"service_tier": value} if value is not None else {"approval": "x"}
        self.lines.append(line({"timestamp": ts(self.t), "type": "event_msg",
                                "payload": {"type": "thread_settings_applied", "thread_settings": settings}}))

    def count(self, u, *, mode="both", seconds=None, style="z"):
        self.tick(seconds)
        total = add(self.total or {}, u)
        info = {}
        if mode in ("both", "total"):
            info["total_token_usage"] = total
        if mode in ("both", "last"):
            info["last_token_usage"] = u
        if mode == "repeat" and self.total is not None:
            info = {"total_token_usage": self.total, "last_token_usage": u}
        else:
            self.total = total if mode != "last" else self.total
        self.lines.append(line({"timestamp": ts(self.t, style), "type": "event_msg", "payload": {"type": "token_count", "info": info}}))
        return u

    def compaction(self, rid, u, *, marker_first=False, local=False, model=None):
        self.tick()
        record = {"timestamp": ts(self.t), "type": "token_usage_record", "payload": {"response_id": rid, "usage": u}}
        if model:
            record["payload"]["model"] = model
        marker = {"timestamp": ts(self.t), "type": "compacted", "payload": {"compaction_response_id": rid}}
        if marker_first:
            self.lines.append(line(marker))
            self.lines.append(line(record))
        else:
            self.lines.append(line(record))
            if local:
                self.count(u, seconds=0.2)
            self.lines.append(line(marker))

    def headless(self, u, model=None):
        self.tick()
        rec = {"type": "turn.completed", "usage": u, "timestamp": ts(self.t)}
        if model:
            rec["model"] = model
        self.lines.append(line(rec))

    def noise(self):
        self.tick()
        self.lines.append(line({"timestamp": ts(self.t), "type": "response_item", "payload": {"type": "message", "content": "hello token_count"}}))


def codex(rng, root):
    home = root / ".codex"
    start = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)
    parents = []
    for n in range(rng.randint(3, 6)):
        sid = f"p{n}"
        r = Rollout(rng, start + timedelta(hours=rng.uniform(0, 96)), sid)
        if rng.random() < 0.7:
            r.model(rng.choice(CODEX_MODELS))
        for _ in range(rng.randint(3, 25)):
            roll = rng.random()
            if roll < 0.08:
                r.model(rng.choice(CODEX_MODELS))
            elif roll < 0.14:
                r.tier(rng.choice(TIERS + [None]))
            elif roll < 0.18:
                r.compaction(f"resp-{sid}-{_}", usage(rng), marker_first=rng.random() < 0.3, local=rng.random() < 0.5,
                             model=rng.choice([None, "gpt-5.4"]))
            elif roll < 0.21:
                r.headless(usage(rng), model=rng.choice([None, "gpt-5.3-codex"]))
            elif roll < 0.24:
                r.noise()
            elif roll < 0.28:
                r.count(usage(rng), mode="repeat")
            else:
                r.count(usage(rng, big=rng.random() < 0.15), mode=rng.choice(["both"] * 6 + ["total", "last"]),
                        style=rng.choice(["z"] * 12 + ["num", "ms"]))
        where = rng.choice(["sessions", "sessions", "archived_sessions"])
        path = home / where / "2026" / "09" / f"{28 + n % 3:02d}" / f"rollout-{sid}.jsonl"
        compress = zstandard is not None and rng.random() < 0.35
        write(path, r.lines, compress=compress)
        if rng.random() < 0.2:  # the same rollout archived too: sessions/ wins
            other = "archived_sessions" if where == "sessions" else "sessions"
            write(home / other / path.relative_to(home / where), r.lines, compress=not compress and zstandard is not None)
        if rng.random() < 0.15 and not compress and zstandard is not None:  # both encodings side by side
            write(path, r.lines + [line({"timestamp": ts(r.t), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": usage(rng)}}})], compress=True)
        parents.append((sid, r))

    # Forks and subagents replaying a parent's usage.
    for n in range(rng.randint(1, 7)):
        sid, parent = rng.choice(parents)
        subagent = rng.random() < 0.4
        kind = rng.choice(["exact", "missing", "burst"])
        fork_at = parent.t + timedelta(seconds=rng.uniform(-300, 60))
        child = Rollout(rng, fork_at, f"c{n}", parent=sid if kind != "missing" else f"gone{n}", subagent=subagent)
        child.t = fork_at
        child.model("gpt-5.4")
        # replay the parent's token counts
        for raw in parent.lines:
            obj = json.loads(raw)
            if obj.get("type") in ("token_usage_record", "compacted") and rng.random() < 0.7:
                child.lines.append(line(obj))
                continue
            if obj.get("type") == "event_msg" and obj["payload"].get("type") == "token_count":
                info = obj["payload"]["info"]
                if kind == "burst":
                    child.t += timedelta(milliseconds=rng.randint(1, 30))
                    obj["timestamp"] = ts(child.t)
                child.lines.append(line(obj))
        for _ in range(rng.randint(1, 6)):
            child.count(usage(rng), seconds=rng.uniform(6, 300))
        write(home / "sessions" / "2026" / "10" / "02" / f"rollout-c{n}.jsonl", child.lines)

    if rng.random() < 0.3:
        (home / "config.toml").parent.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(rng.choice(['service_tier = "fast"\n', 'service_tier = "flex"\n',
                                                     'profile = "x"\n[profiles.x]\nservice_tier = "priority"\n',
                                                     'model = "gpt-5.4"\n']))


CLAUDE_MODELS = ["claude-opus-4-8", "claude-sonnet-4-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-opus-4-6",
                 "claude-sonnet-4-20250514", "claude-mystery-9", "<synthetic>", "claude-opus-5-5"]


def claude(rng, root):
    projects = root / ".claude" / "projects"
    start = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)
    pool = []
    for f in range(rng.randint(3, 7)):
        session = f"sess{f}"
        lines = []
        t = start + timedelta(hours=rng.uniform(0, 96))
        sidechain = rng.random() < 0.25
        for i in range(rng.randint(3, 30)):
            t += timedelta(seconds=rng.uniform(1, 900))
            if pool and rng.random() < COPY:  # a copy of an earlier message
                rec = json.loads(rng.choice(pool))
                if rng.random() < 0.5:
                    rec["sessionId"] = session
                if rng.random() < 0.4:
                    rec["requestId"] = f"req-new-{f}-{i}"
                    rec["isSidechain"] = True
                if rng.random() < 0.3:
                    rec["message"]["usage"]["output_tokens"] += rng.randint(1, 50)
                lines.append(line(rec))
                continue
            model = rng.choice(CLAUDE_MODELS)
            inp = rng.randint(0, 5000)
            write5 = rng.randint(0, 40_000)
            write1 = rng.randint(0, 20_000) if rng.random() < 0.4 else 0
            read = rng.randint(0, 250_000 if rng.random() < 0.2 else 60_000)
            out = rng.randint(1, 4000)
            usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read}
            if rng.random() < 0.85:
                usage["cache_creation_input_tokens"] = write5 + write1
            if rng.random() < 0.7:
                usage["cache_creation"] = {"ephemeral_5m_input_tokens": write5, "ephemeral_1h_input_tokens": write1}
            if rng.random() < 0.15:
                usage["speed"] = rng.choice(["fast", "standard"])
            mid = rng.choice([f"msg_{f}_{i}", f"gw-{f}-{i}", f"msg_{f}_{i}"])
            rec = {"type": "assistant", "timestamp": ts(t), "sessionId": session, "version": "2.1.3",
                   "message": {"id": mid, "model": model, "usage": usage}}
            if rng.random() < 0.85:
                rec["requestId"] = f"req_{f}_{i}"
            if sidechain:
                rec["isSidechain"] = True
            if rng.random() < 0.05:
                rec["costUSD"] = round(rng.uniform(0, 2), 6)
            if rng.random() < 0.03:
                rec["version"] = "beta"
            if rng.random() < 0.02:
                rec["requestId"] = None
            if rng.random() < 0.06:
                usage["iterations"] = [
                    {"type": "message", "input_tokens": 1, "output_tokens": 1},
                    {"type": "advisor_message", "model": "claude-opus-4-8", "input_tokens": rng.randint(1, 900),
                     "output_tokens": rng.randint(1, 900), "cache_read_input_tokens": rng.randint(0, 9000)},
                ]
            raw = line(rec)
            if rng.random() < 0.05:  # a subagent progress line wrapping the message
                raw = line({"type": "progress", "sessionId": session, "data": {"message": {
                    "timestamp": rec["timestamp"], "requestId": rec.get("requestId"), "message": rec["message"]}}})
            lines.append(raw)
            pool.append(line(rec))
            # streaming: the same response logged again with more output
            if rng.random() < 0.2:
                rec2 = json.loads(line(rec))
                rec2["message"]["usage"]["output_tokens"] += rng.randint(1, 500)
                lines.append(line(rec2))
        if rng.random() < 0.3:
            path = projects / "proj" / f"sess{rng.randint(0, f)}" / "subagents" / f"agent-{f}.jsonl"
        else:
            path = projects / rng.choice(["proj", "other"]) / f"{session}.jsonl"
        write(path, lines)


def generate(seed, out):
    global COPY
    COPY = 0.15 if seed % 2 else 0.45
    rng = random.Random(seed)
    codex(rng, Path(out))
    claude(rng, Path(out))


if __name__ == "__main__":
    generate(int(sys.argv[1]), sys.argv[2])
