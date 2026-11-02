"""What SubBench reads, what it keeps, and what leaves the machine, stated by the code itself.

`subbench privacy` prints this, then the record of every request actually made
(net.egress_log), and `subbench privacy preview` prints the exact payload the next push
would send, byte for byte, without sending it.

Account ids never leave in the clear. A provider's account id is replaced by a one-way
hash before any payload is built, so the server can tell one account's readings from
another's, and two machines on the same account still pool, but cannot recover the id.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter
from typing import Any

from . import logs, net

PSEUDONYM_VERSION = "v1"

# What is read from each local log line, and nothing else is kept. Message text, prompts,
# file contents, tool calls and paths are never stored, printed or sent.
READ_FIELDS = {
    "claude": ["timestamp", "requestId", "message.id", "message.model",
               "message.usage (input, cache write, cache read, output token counts)"],
    "codex": ["timestamp", "turn_context.model", "token_count totals (input, cached, output, reasoning)"],
}

# What a push sends. Raw usage rows (daily token counts per model) are only sent when
# SUBBENCH_PUSH_RAW_USAGE=1.
SENT = [
    "a random collector id (not derived from the machine or the account)",
    "quota readings: provider, plan, window, percent used, reset time, hashed account id",
    "derived values: API-equivalent dollars per window, pooled per product",
    "the model mix per window: model names and token totals",
]
NEVER_SENT = [
    "prompts, replies, code, file paths, project names or any log text",
    "email addresses, names or the provider's account ids",
    "your Claude or Codex login (each goes only to its own provider, to read the meter)",
]


def pseudonym(account_id: str) -> str:
    """A one-way, stable stand-in for a provider account id.

    Provider account ids are random UUIDs, so a hash of one cannot be reversed or guessed
    by enumeration. The same account hashes the same on every machine, which is what lets
    two collectors on one subscription pool their readings.
    """
    digest = hashlib.sha256(f"subbench:{PSEUDONYM_VERSION}:{account_id}".encode("utf-8")).hexdigest()
    return f"acct-{digest[:16]}"


def pseudonymise(value: Any) -> Any:
    """Replace every account id in a payload, at any depth."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "account_id" and isinstance(item, str) and item and not item.startswith("acct-"):
                out[key] = pseudonym(item)
            else:
                out[key] = pseudonymise(item)
        return out
    if isinstance(value, list):
        return [pseudonymise(item) for item in value]
    return value


def preview(db: sqlite3.Connection, url: str) -> dict[str, Any]:
    """The payload the next push to `url` would send. Changes nothing, sends nothing."""
    from .push import (MAX_ENTITLEMENT_ROWS_PER_BATCH, MAX_USAGE_ROWS_PER_BATCH, RAW_USAGE_ENV, build_payload,
                       build_reports, pending_entitlements, pending_usage)

    row = db.execute("SELECT agent_id, entitlement_cursor, usage_cursor FROM push_state WHERE endpoint = ?", (url,)).fetchone()
    agent_id = row["agent_id"] if row else "(assigned on the first push)"
    entitlements = pending_entitlements(db, row["entitlement_cursor"] if row else None, MAX_ENTITLEMENT_ROWS_PER_BATCH)
    usage = pending_usage(db, row["usage_cursor"] if row else None, MAX_USAGE_ROWS_PER_BATCH) \
        if os.environ.get(RAW_USAGE_ENV) == "1" else []
    return build_payload(agent_id, entitlements, usage, reports=build_reports(db))


def statement() -> str:
    lines = ["SubBench privacy", "", "Read on this machine, and only these fields:"]
    for provider, fields in READ_FIELDS.items():
        where = ", ".join(str(r) for r in logs.roots(provider))
        lines.append(f"  {provider}: {where}")
        lines.extend(f"    - {field}" for field in fields)
    lines += ["", "Stored locally: daily token totals and costs per model, quota readings, derived values.", ""]
    lines.append("Network: these hosts and no others (enforced in subbench/net.py):")
    for host, what in net.allowed_hosts().items():
        lines.append(f"  {host}: {what}")
    if not net.push_host():
        lines.append("  (no push endpoint configured: nothing is sent anywhere but the providers' own meters)")
    lines += ["", "A push sends:"] + [f"  - {item}" for item in SENT]
    lines += ["", "Never sent:"] + [f"  - {item}" for item in NEVER_SENT]
    lines += ["", f"Every request is recorded in {net.egress_log()}.",
              "`subbench privacy log` lists them; `subbench privacy preview` prints the next push in full."]
    return "\n".join(lines)


def log_summary(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return f"No requests recorded yet in {net.egress_log()}."
    counts = Counter((e.get("host"), e.get("purpose")) for e in entries)
    sent = Counter()
    for e in entries:
        sent[e.get("host")] += int(e.get("sent_bytes") or 0)
    lines = [f"{len(entries)} requests, {entries[0].get('at')} to {entries[-1].get('at')}", ""]
    for (host, purpose), count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {count:>6}  {host:<28} {purpose}")
    lines += ["", "Bytes sent, by host:"] + [f"  {host:<28} {total:,}" for host, total in sent.most_common()]
    return "\n".join(lines)


def run(db: sqlite3.Connection | None, args) -> int:
    command = getattr(args, "privacy_command", None) or "statement"
    if command == "statement":
        print(statement())
        return 0
    if command == "log":
        entries = net.read_log()
        if args.raw:
            for entry in entries[-args.limit:]:
                print(json.dumps(entry, sort_keys=True))
        else:
            print(log_summary(entries))
        return 0
    if command == "preview":
        url = os.environ.get("SUBBENCH_PUSH_URL")
        if not url:
            print("No push endpoint is configured, so nothing would be sent.")
            return 0
        assert db is not None
        print(json.dumps(preview(db, url), indent=2, sort_keys=True))
        return 0
    raise AssertionError(command)
