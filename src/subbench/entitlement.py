from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import account


@dataclass(frozen=True)
class EntitlementWindow:
    provider: str
    window: str
    used_percent: float
    resets_at: str | None
    duration_minutes: int | None
    source: str
    account_id: str | None = None


def collect_entitlements(provider: str) -> list[EntitlementWindow]:
    if provider == "codex":
        return collect_codex()
    if provider == "claude":
        return collect_claude()
    raise ValueError(f"unsupported provider: {provider}")


def collect_codex() -> list[EntitlementWindow]:
    account_id = account.active_account_id()
    rows = collect_codex_for(account_id)
    return rows


def collect_codex_for(account_id: str | None) -> list[EntitlementWindow]:
    process = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        requests = (
            {"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "subbench", "title": "SubBench", "version": "0.1.0"}, "capabilities": {}}},
            {"method": "initialized", "params": {}},
            {"method": "account/rateLimits/read", "id": 2},
        )
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        for line in process.stdout:
            message = json.loads(line)
            if message.get("id") == 2:
                return _normalise_codex(message.get("result", {}), account_id=account_id)
        raise RuntimeError("codex app-server ended before returning rate limits")
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


def collect_claude() -> list[EntitlementWindow]:
    command = os.environ.get("SUBBENCH_CLAUDE_USAGE_COMMAND")
    if not command:
        raise RuntimeError(
            "Claude entitlement collection requires SUBBENCH_CLAUDE_USAGE_COMMAND; "
            "set it to a local command that prints the OAuth usage response as JSON"
        )
    result = subprocess.run(shlex.split(command), check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Claude usage command exited {result.returncode}")
    return _normalise_claude(json.loads(result.stdout))


def _normalise_codex(payload: dict[str, Any], *, account_id: str | None = None) -> list[EntitlementWindow]:
    limits = payload.get("rateLimits") or payload
    candidates: list[tuple[str, str, dict[str, Any], int | None]] = []
    for name, key in (("primary", "primary"), ("secondary", "secondary")):
        window = limits.get(key)
        if not isinstance(window, dict) or window.get("usedPercent") is None:
            continue
        duration = _int_or_none(window.get("windowDurationMins"))
        label = "five_hour" if duration and 240 <= duration <= 360 else "weekly" if duration and duration >= 6 * 24 * 60 else name
        candidates.append((name, label, window, duration))

    label_counts = {label: sum(1 for _, candidate_label, _, _ in candidates if candidate_label == label) for _, label, _, _ in candidates}
    rows: list[EntitlementWindow] = []
    for name, label, window, duration in candidates:
        # Disabled five-hour quotas can make both levels report a weekly duration.
        # Keep each independent series rather than losing one to the DB uniqueness key.
        unique_label = f"{label}_{name}" if label_counts[label] > 1 else label
        rows.append(EntitlementWindow(
            "codex", unique_label, float(window["usedPercent"]),
            _epoch_iso(window.get("resetsAt")), duration, "codex-app-server", account_id=account_id,
        ))
    return rows


def _normalise_claude(payload: dict[str, Any]) -> list[EntitlementWindow]:
    account_id = _first_text(payload, ("account_uuid", "accountUuid", "account_id", "accountId",
                                      "organization_uuid", "organizationUuid"))
    aliases = {
        "five_hour": "five_hour",
        "fiveHour": "five_hour",
        "seven_day": "weekly",
        "sevenDay": "weekly",
        "weekly": "weekly",
    }
    rows: list[EntitlementWindow] = []
    for key, label in aliases.items():
        value = payload.get(key)
        if not isinstance(value, dict):
            continue
        utilisation = value.get("utilization", value.get("usedPercent"))
        if utilisation is None:
            continue
        used = float(utilisation)
        if 0 <= used <= 1:
            used *= 100
        reset = value.get("resets_at", value.get("resetsAt"))
        duration = 300 if label == "five_hour" else 10080
        rows.append(EntitlementWindow(
            "claude", label, used, _time_iso(reset), duration, "claude-oauth-usage",
            account_id=account_id,
        ))
    return rows


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw
    return None


def _epoch_iso(value: Any) -> str | None:
    if value is None:
        return None
    # Codex may vary a stable reset boundary by a few seconds between reads.
    # Minute precision distinguishes actual reset windows without splitting one.
    return _round_to_minute(datetime.fromtimestamp(float(value), timezone.utc))


def _time_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _epoch_iso(value)
    # Claude returns a stable reset boundary with drifting sub-second noise, so the same
    # window would otherwise be stored under a different key on every read. Round to the
    # minute exactly as Codex timestamps are.
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return _round_to_minute(parsed)


def _round_to_minute(value: datetime) -> str:
    """Nearest minute, not truncated: a boundary that jitters either side of :00 would
    otherwise land in two different minutes and split one window in two."""
    return (value + timedelta(seconds=30)).replace(second=0, microsecond=0).isoformat()


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)
