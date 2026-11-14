from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from . import account
from .scopes import SCOPED_WINDOW_PREFIX, scope_slug


@dataclass(frozen=True)
class EntitlementWindow:
    provider: str
    window: str
    used_percent: float
    resets_at: str | None
    duration_minutes: int | None
    source: str
    account_id: str | None = None
    # Reported by the provider alongside the meter. Two accounts may only be pooled when
    # they are the same plan, and the registry that codex-auth maintains is not always
    # present, so the value that comes back with the quota reading is the reliable one.
    plan: str | None = None
    # Only for a scoped limit (one model's own sub-limit inside the weekly limit): the share
    # of the weekly limit that 100% of this meter equals. Claude's Fable limit is 50.
    allowance_percent: float | None = None


# A provider that does not name the plan beside its meter leaves SubBench unable to tell
# one product from another. Claude's usage endpoint returns meters only. The usage command
# asks the profile endpoint for the current tier and copies it onto the payload. The login
# file is a fallback: Claude Code rewrites it only when it next fetches the profile, which
# can be days after an upgrade. A declaration, SUBBENCH_PLAN_CLAUDE, fills in only when
# neither is present.
#
# A declared plan is asserted by the operator, not measured, so it is only ever a fallback:
# a plan the provider does report always wins, and an upgrade the provider reports is
# followed even when a stale declaration disagrees.
PLAN_ENV_PREFIX = "SUBBENCH_PLAN_"


def declared_plan(provider: str) -> str | None:
    value = os.environ.get(f"{PLAN_ENV_PREFIX}{provider.upper()}", "").strip()
    return value or None


def collect_entitlements(provider: str) -> list[EntitlementWindow]:
    if provider == "codex":
        rows = collect_codex()
    elif provider == "claude":
        rows = collect_claude()
    else:
        raise ValueError(f"unsupported provider: {provider}")
    fallback = declared_plan(provider)
    if fallback is None:
        return rows
    return [row if row.plan else replace(row, plan=fallback) for row in rows]


def collect_codex() -> list[EntitlementWindow]:
    return collect_codex_for(account.active_account_id())


def collect_codex_for(account_id: str | None) -> list[EntitlementWindow]:
    from .endpoints import EndpointError, codex_app_server, codex_usage

    try:
        payload = codex_usage()
    except EndpointError as direct:
        try:
            payload = codex_app_server()
        except (EndpointError, OSError, ValueError) as fallback:
            raise RuntimeError(f"{direct} (and {fallback})") from None
    return _normalise_codex(payload, account_id=account_id)


class MeterDeferred(RuntimeError):
    """The meter was not read this cycle, on purpose: spacing, or a rate-limit backoff."""


# Anthropic's usage endpoint answers 429 when it is read too often, and a reading it refuses
# is lost for good. Space the reads, and after a 429 wait, doubling up to half an hour.
CLAUDE_METER_MIN_SECONDS = 120.0
CLAUDE_BACKOFF_FIRST_SECONDS = 300.0
CLAUDE_BACKOFF_MAX_SECONDS = 1800.0
_CLAUDE_METER: dict[str, float] = {}


def collect_claude() -> list[EntitlementWindow]:
    now = time.monotonic()
    wait = _CLAUDE_METER.get("next", 0.0) - now
    if wait > 0:
        reason = "rate-limit backoff" if _CLAUDE_METER.get("backoff") else "spacing"
        _CLAUDE_METER["deferred"] = 1.0
        raise MeterDeferred(f"Claude meter next read in {wait:.0f} s ({reason})")
    try:
        rows = _read_claude()
    except RuntimeError as error:
        if getattr(error, "status", None) == 429:
            backoff = min(max(_CLAUDE_METER.get("backoff", 0.0) * 2, CLAUDE_BACKOFF_FIRST_SECONDS),
                          CLAUDE_BACKOFF_MAX_SECONDS)
            _CLAUDE_METER.update(backoff=backoff, next=now + backoff, deferred=1.0)
        raise
    _CLAUDE_METER.update(backoff=0.0, next=now + CLAUDE_METER_MIN_SECONDS, deferred=0.0)
    return rows


def meter_retry_in(provider: str) -> float | None:
    """Seconds until a meter read that was deferred or refused may be tried again, or None.

    A reading is taken when the logs change. When the agent goes idle right after a
    deferred read, the next change can be hours away and in another window, and the end
    of that stretch of use would never be read. The watcher collects again at this time.
    """
    if provider != "claude" or not _CLAUDE_METER.get("deferred"):
        return None
    return max(_CLAUDE_METER.get("next", 0.0) - time.monotonic(), 0.0)


def _read_claude() -> list[EntitlementWindow]:
    # An explicit command still wins, for anyone who wired their own reader.
    command = os.environ.get("SUBBENCH_CLAUDE_USAGE_COMMAND")
    if command:
        result = subprocess.run(shlex.split(command), check=False, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"Claude usage command exited {result.returncode}")
        return _normalise_claude(json.loads(result.stdout))
    from .endpoints import EndpointError, claude_usage

    try:
        return _normalise_claude(claude_usage())
    except EndpointError as error:
        failure = RuntimeError(str(error))
        failure.status = error.status  # type: ignore[attr-defined]
        raise failure from None


def _normalise_codex(payload: dict[str, Any], *, account_id: str | None = None) -> list[EntitlementWindow]:
    limits = payload.get("rateLimits") or payload
    plan = _first_text(limits, ("planType", "plan_type", "plan"))
    candidates: list[tuple[str, str, dict[str, Any], int | None]] = []
    for name, key in (("primary", "primary"), ("secondary", "secondary")):
        window = limits.get(key)
        if not isinstance(window, dict) or window.get("usedPercent") is None:
            continue
        duration = _int_or_none(window.get("windowDurationMins"))
        label = "five_hour" if duration and 240 <= duration <= 360 else "weekly" if duration and duration >= 6 * 24 * 60 else name
        candidates.append((name, label, window, duration))

    rows: list[EntitlementWindow] = []
    seen: dict[str, int] = {}
    for name, label, window, duration in candidates:
        # Disabled five-hour quotas can make both levels report a weekly duration. Each
        # needs its own series or one is lost to the uniqueness key -- but only the
        # *duplicate* is renamed. Suffixing both would rename the series a provider change
        # arrived in the middle of, orphaning every observation recorded before it and
        # splitting the window that is currently being measured.
        seen[label] = seen.get(label, 0) + 1
        unique_label = label if seen[label] == 1 else f"{label}_{name}"
        rows.append(EntitlementWindow(
            "codex", unique_label, float(window["usedPercent"]),
            _epoch_iso(window.get("resetsAt")), duration, "codex-app-server",
            account_id=account_id, plan=plan,
        ))
    return rows


def _claude_plan(payload: dict[str, Any]) -> str | None:
    # default_claude_max_5x is the provider's tier name. The product key is the part after
    # that prefix, matching the max_20x form Codex already reports.
    tier = _first_text(payload, ("organizationRateLimitTier", "rateLimitTier", "rate_limit_tier"))
    if tier:
        prefix = "default_claude_"
        return tier[len(prefix):] if tier.startswith(prefix) else tier
    return _first_text(payload, ("subscription_type", "subscriptionType", "plan", "tier"))


def _normalise_claude(payload: dict[str, Any]) -> list[EntitlementWindow]:
    account_id = _first_text(payload, ("account_uuid", "accountUuid", "account_id", "accountId",
                                      "organization_uuid", "organizationUuid"))
    plan = _claude_plan(payload)
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
            account_id=account_id, plan=plan,
        ))
    rows.extend(_claude_scoped(payload, account_id=account_id, plan=plan))
    return rows


def _claude_scoped(payload: dict[str, Any], *, account_id: str | None, plan: str | None) -> list[EntitlementWindow]:
    """One model's own weekly sub-limit, from the `limits` list.

    Usage of that model also moves the weekly limit, and its meter reads 0-100% of its own
    allowance. `weekly_scoped_shares` gives that allowance as a share of the weekly limit,
    which is what lets the estimator take the model's quota back out of the weekly meter.
    A limit scoped to a surface (an app, not a model) is skipped: no model id can price it.
    """
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return []
    shares = payload.get("weekly_scoped_shares")
    allowance: dict[int, float] = {}
    for share in shares if isinstance(shares, list) else []:
        if isinstance(share, dict) and isinstance(share.get("limit_index"), int):
            value = share.get("allowance_percent_of_weekly")
            if isinstance(value, (int, float)) and value > 0:
                allowance[share["limit_index"]] = float(value)
    rows: list[EntitlementWindow] = []
    for index, limit in enumerate(limits):
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        scope = limit.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        slug = scope_slug(name) if isinstance(name, str) else ""
        percent = limit.get("percent")
        if not slug or not isinstance(percent, (int, float)):
            continue
        rows.append(EntitlementWindow(
            "claude", SCOPED_WINDOW_PREFIX + slug, float(percent), _time_iso(limit.get("resets_at")), 10080,
            "claude-oauth-usage", account_id=account_id, plan=plan, allowance_percent=allowance.get(index),
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
