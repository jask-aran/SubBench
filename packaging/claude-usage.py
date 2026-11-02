#!/usr/bin/env python3
"""Print the Claude subscription usage response as JSON, for SUBBENCH_CLAUDE_USAGE_COMMAND.

No longer needed: the collector now reads the Claude meter itself (subbench/endpoints.py).
Kept so a unit installed before 0.3.0, which still names this file, keeps working until it
is replaced with `subbench service install`.

Claude Code exposes no `claude usage` subcommand and no local RPC equivalent to
`codex app-server`, so entitlement has to come from the OAuth usage endpoint using the
credentials Claude Code already stores locally. The access token is read at run time and
sent only to the API host it belongs to.

    export SUBBENCH_CLAUDE_USAGE_COMMAND="python3 $PWD/packaging/claude-usage.py"
    subbench watch --provider claude --once
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

CREDENTIALS = pathlib.Path(
    os.environ.get("CLAUDE_CREDENTIALS", pathlib.Path.home() / ".claude" / ".credentials.json")
)
ENDPOINT = os.environ.get("CLAUDE_USAGE_URL", "https://api.anthropic.com/api/oauth/usage")
PROFILE = os.environ.get("CLAUDE_PROFILE_URL", "https://api.anthropic.com/api/oauth/profile")
BETA = "oauth-2025-04-20"


def access_token() -> str:
    try:
        payload = json.loads(CREDENTIALS.read_text())
    except FileNotFoundError:
        raise SystemExit(f"no credentials at {CREDENTIALS}; log in with `claude` first")
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read {CREDENTIALS}: {error}")
    token = (payload.get("claudeAiOauth") or {}).get("accessToken")
    if not token:
        raise SystemExit(f"no claudeAiOauth.accessToken in {CREDENTIALS}")
    return str(token)


def main() -> int:
    request = urllib.request.Request(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {access_token()}",
            "anthropic-beta": BETA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        # 401 usually means the stored token expired; opening Claude Code refreshes it.
        raise SystemExit(f"usage endpoint returned {error.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as error:
        raise SystemExit(f"usage endpoint unreachable: {error}")

    sys.stdout.write(with_plan(body, live_tier=profile_tier(fetch_profile(access_token()))))
    return 0


def organization_tier() -> str | None:
    """The tier Claude Code itself uses. Login-time rateLimitTier can lag an upgrade."""
    path = pathlib.Path.home() / ".claude.json"
    try:
        account = json.loads(path.read_text()).get("oauthAccount") or {}
    except (OSError, json.JSONDecodeError):
        return None
    tier = account.get("organizationRateLimitTier")
    return tier.strip() if isinstance(tier, str) and tier.strip() else None


def fetch_profile(token: str) -> dict | None:
    """The live plan. The login file is rewritten only when Claude Code next fetches it."""
    request = urllib.request.Request(
        PROFILE,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def profile_tier(profile: dict | None) -> str | None:
    org = (profile or {}).get("organization") or {}
    tier = org.get("rate_limit_tier")
    return tier.strip() if isinstance(tier, str) and tier.strip() else None


def with_plan(body: str, *, live_tier: str | None = None) -> str:
    """Attach the plan. A live profile wins; the login file is only a fallback."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body if body.endswith("\n") else body + "\n"
    if not isinstance(payload, dict):
        return body if body.endswith("\n") else body + "\n"
    try:
        oauth = json.loads(CREDENTIALS.read_text()).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError):
        oauth = {}
    for key in ("subscriptionType", "rateLimitTier"):
        value = oauth.get(key)
        if isinstance(value, str) and value.strip() and key not in payload:
            payload[key] = value.strip()
    # The file can lag an upgrade by days. The profile call is the plan as of this reading.
    if live_tier:
        payload["organizationRateLimitTier"] = live_tier
    else:
        org = organization_tier()
        if org and "organizationRateLimitTier" not in payload:
            payload["organizationRateLimitTier"] = org
    text = json.dumps(payload)
    return text if text.endswith("\n") else text + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
