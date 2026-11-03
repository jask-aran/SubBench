"""Quota meters, read straight from each provider with the login its agent already stored.

Claude: the OAuth usage endpoint Claude Code's own /usage screen reads, with the access
token from ~/.claude/.credentials.json (or the macOS Keychain). The profile endpoint names
the plan, which the usage response does not.

Codex: the ChatGPT backend usage endpoint the Codex CLI reads, with the token in
$CODEX_HOME/auth.json. When that fails (an expired token is the usual cause) and Codex is
installed, `codex app-server` is asked instead, since Codex refreshes its own login.

Tokens are read when a reading is taken, sent only to the host they belong to, and never
stored or refreshed here: a refresh rotates the login, and rotating it out from under the
agent would sign the user out.
"""
from __future__ import annotations

import base64
import json
import math
import os
import platform
import shutil
import subprocess
import time
import urllib.error
from pathlib import Path
from typing import Any

from . import __version__, account

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAUDE_BETA = "oauth-2025-04-20"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TIMEOUT = 20


class EndpointError(RuntimeError):
    pass


def _get(url: str, headers: dict[str, str], *, credential: str, purpose: str) -> dict[str, Any]:
    from .net import Refused, request

    try:
        payload = json.loads(request(url, headers={"Accept": "application/json", **headers}, credential=credential,
                                     purpose=purpose, timeout=TIMEOUT).decode("utf-8"))
    except Refused as error:
        raise EndpointError(str(error)) from None
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        raise EndpointError(f"{url} returned {error.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise EndpointError(f"{url} unreachable: {error}") from None
    except ValueError as error:
        raise EndpointError(f"{url} returned invalid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise EndpointError(f"{url} returned {type(payload).__name__}, expected an object")
    return payload


# ---------------------------------------------------------------- Claude

def claude_config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").split(",")[0].strip()
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def claude_credentials() -> dict[str, Any]:
    """claudeAiOauth from the credentials file, or from the Keychain on macOS."""
    override = os.environ.get("CLAUDE_CREDENTIALS")
    path = Path(override).expanduser() if override else claude_config_dir() / ".credentials.json"
    text: str | None = None
    try:
        text = path.read_text()
    except OSError:
        if platform.system() == "Darwin" and shutil.which("security"):
            result = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                                    check=False, capture_output=True, text=True)
            if result.returncode == 0:
                text = result.stdout
    if not text:
        raise EndpointError(f"no Claude login at {path}; sign in with `claude` first")
    try:
        oauth = json.loads(text).get("claudeAiOauth") or {}
    except (ValueError, AttributeError):
        raise EndpointError(f"cannot read the Claude login at {path}") from None
    if not oauth.get("accessToken"):
        raise EndpointError("the Claude login has no OAuth access token; sign in with a Claude subscription")
    return oauth


def claude_usage() -> dict[str, Any]:
    """The usage response, with the plan attached as the normaliser expects it."""
    oauth = claude_credentials()
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        raise EndpointError("the stored Claude login has expired; open Claude Code once to refresh it")
    headers = {"Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": CLAUDE_BETA,
               "User-Agent": f"subbench/{__version__}"}
    payload = _get(os.environ.get("CLAUDE_USAGE_URL", CLAUDE_USAGE_URL), headers, credential="claude", purpose="claude quota")
    for key in ("subscriptionType", "rateLimitTier"):
        value = oauth.get(key)
        if isinstance(value, str) and value.strip() and key not in payload:
            payload[key] = value.strip()
    # The plan as of this reading. The login file can lag an upgrade by days.
    try:
        profile = _get(os.environ.get("CLAUDE_PROFILE_URL", CLAUDE_PROFILE_URL), headers, credential="claude", purpose="claude plan")
    except EndpointError:
        profile = {}
    tier = _text((profile.get("organization") or {}).get("rate_limit_tier"))
    # The quota belongs to the user, not the organisation (a Team organisation has many
    # users, each with a meter). The server pools by this id, hashed, so it can tell two
    # devices on one account from two people.
    # The local id comes first: usage imports carry it too, and a reading is priced only
    # from an import of its own account. The profile's id covers a missing ~/.claude.json.
    stored = account.claude_oauth_account()
    tier = tier or _text(stored.get("organizationRateLimitTier"))
    uuid = account.active_claude_account_id() or _text((profile.get("account") or {}).get("uuid"))
    if tier:
        payload["organizationRateLimitTier"] = tier
    if uuid and "account_uuid" not in payload:
        payload["account_uuid"] = uuid
    return payload


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None



# ---------------------------------------------------------------- Codex

def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    return Path(raw.split(",")[0].strip()).expanduser()


def codex_tokens() -> dict[str, Any]:
    path = codex_home() / "auth.json"
    try:
        tokens = json.loads(path.read_text()).get("tokens") or {}
    except OSError:
        raise EndpointError(f"no Codex login at {path}; sign in with `codex login` first") from None
    except (ValueError, AttributeError):
        raise EndpointError(f"cannot read the Codex login at {path}") from None
    if not tokens.get("access_token"):
        raise EndpointError("the Codex login has no ChatGPT access token (an API-key login has no subscription quota)")
    return tokens


def _jwt_expiry(token: str) -> float | None:
    try:
        body = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return float(claims["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def codex_usage() -> dict[str, Any]:
    """The rate limits in the shape `codex app-server` returns them: {"rateLimits": {...}}."""
    tokens = codex_tokens()
    expiry = _jwt_expiry(tokens["access_token"])
    if expiry is not None and expiry < time.time():
        raise EndpointError("the stored Codex login has expired; run Codex once to refresh it")
    headers = {"Authorization": f"Bearer {tokens['access_token']}", "User-Agent": "codex-cli", "OAI-Product-Sku": "codex"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-ID"] = str(tokens["account_id"])
    return wham_to_protocol(_get(os.environ.get("SUBBENCH_CODEX_USAGE_URL", CODEX_USAGE_URL), headers,
                                 credential="codex", purpose="codex quota"))


def wham_to_protocol(payload: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """The backend's {"rate_limit": {"primary_window": ...}} as {"rateLimits": {"primary": ...}}."""
    protocol = payload.get("rate_limits") or payload.get("rateLimits")
    if isinstance(protocol, dict) and (protocol.get("primary") or protocol.get("secondary")):
        return {"rateLimits": protocol}
    now = time.time() if now is None else now
    details = payload.get("rate_limit") or payload.get("rateLimit") or {}
    limits: dict[str, Any] = {}
    plan = payload.get("plan_type") or payload.get("planType")
    if isinstance(plan, str) and plan:
        limits["planType"] = plan
    for slot, names in (("primary", ("primary_window", "primaryWindow", "primary")),
                        ("secondary", ("secondary_window", "secondaryWindow", "secondary"))):
        window = next((details.get(name) for name in names if isinstance(details.get(name), dict)), None)
        if not window:
            continue
        used = window.get("used_percent", window.get("usedPercent"))
        if used is None:
            continue
        seconds = window.get("limit_window_seconds", window.get("limitWindowSeconds"))
        reset = window.get("reset_at", window.get("resetAt", window.get("resetsAt")))
        if reset is None and window.get("reset_after_seconds") is not None:
            reset = now + float(window["reset_after_seconds"])
        limits[slot] = {
            "usedPercent": float(used),
            "windowDurationMins": math.ceil(float(seconds) / 60) if seconds else None,
            "resetsAt": reset,
        }
    if "primary" not in limits and "secondary" not in limits:
        raise EndpointError("the Codex usage response carried no rate-limit windows")
    return {"rateLimits": limits}


def codex_app_server() -> dict[str, Any]:
    """Ask a local Codex install. Used only when the direct request fails."""
    if not shutil.which("codex"):
        raise EndpointError("codex is not installed")
    process = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, bufsize=1)
    assert process.stdin is not None and process.stdout is not None
    try:
        for request in (
            {"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "subbench", "title": "SubBench", "version": __version__}, "capabilities": {}}},
            {"method": "initialized", "params": {}},
            {"method": "account/rateLimits/read", "id": 2},
        ):
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        for line in process.stdout:
            message = json.loads(line)
            if message.get("id") == 2:
                return message.get("result", {})
        raise EndpointError("codex app-server ended before returning rate limits")
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
