from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Account:
    account_id: str
    alias: str | None = None
    email: str | None = None
    plan: str | None = None


def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    # CODEX_HOME may be a comma-separated list of homes; the active auth lives in the first.
    return Path(raw.split(",")[0].strip()).expanduser()


def auth_file() -> Path:
    return codex_home() / "auth.json"


def registry_file() -> Path:
    return codex_home() / "accounts" / "registry.json"


def claude_code_state() -> dict[str, Any]:
    """Claude Code's state file (~/.claude.json), or {}."""
    try:
        state = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def claude_oauth_account() -> dict[str, Any]:
    """Claude Code's record of the signed-in account (~/.claude.json), or {}."""
    account = claude_code_state().get("oauthAccount") or {}
    return account if isinstance(account, dict) else {}


def active_claude_account_id() -> str | None:
    """The uuid of the account Claude Code is signed in to, or None.

    Usage imports and quota readings must carry the same id, because a reading is priced
    only from an import of its own account. Both therefore take it from here.
    """
    value = claude_oauth_account().get("accountUuid")
    return value.strip() if isinstance(value, str) and value.strip() else None


def active_account_id() -> str | None:
    """Return the chatgpt_account_id currently active for Codex, or None."""
    try:
        payload = json.loads(auth_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        return None
    account_id = tokens.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    # Older auth.json variants may only carry the id_token JWT. Fall back to its claim.
    id_token = tokens.get("id_token")
    if isinstance(id_token, str) and id_token.count(".") == 2:
        try:
            _header, body, _sig = id_token.split(".")
            padding = "=" * (-len(body) % 4)
            import base64

            claims = json.loads(base64.urlsafe_b64decode(body + padding))
        except (ValueError, json.JSONDecodeError):
            return None
        claim = claims.get("https://api.openai.com/auth") or {}
        candidate = claim.get("chatgpt_account_id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def lookup_account(account_id: str) -> Account | None:
    """Resolve account metadata from the local codex-auth registry."""
    try:
        payload = json.loads(registry_file().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    for entry in payload.get("accounts", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("chatgpt_account_id") == account_id:
            return Account(
                account_id=account_id,
                alias=_clean(entry.get("alias")) or None,
                email=_clean(entry.get("email")) or None,
                plan=_clean(entry.get("plan")) or None,
            )
    active_key = payload.get("active_account_key")
    if isinstance(active_key, str) and active_key.endswith(f"::{account_id}"):
        return Account(account_id=account_id)
    return Account(account_id=account_id)


def list_accounts() -> list[Account]:
    """Return every account codex-auth knows about (active or inactive)."""
    try:
        payload = json.loads(registry_file().read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    accounts: list[Account] = []
    for entry in payload.get("accounts", []):
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("chatgpt_account_id")
        if not isinstance(account_id, str) or not account_id:
            continue
        accounts.append(Account(
            account_id=account_id,
            alias=_clean(entry.get("alias")) or None,
            email=_clean(entry.get("email")) or None,
            plan=_clean(entry.get("plan")) or None,
        ))
    return accounts


def account_label(account_id: str | None) -> str:
    """Compact human label for an account_id (email > alias > short id)."""
    if not account_id:
        return "all"
    account = lookup_account(account_id)
    if account and account.email:
        return account.email
    if account and account.alias:
        return account.alias
    return account_id[:8]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()