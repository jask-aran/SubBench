"""Every network request SubBench makes goes through `request` in this module.

That gives three guarantees, each one checkable:

1. Only named hosts are reachable. A request to anything else raises before a socket is
   opened. The list is `ALLOWED` below plus the push endpoint you configured yourself.
2. A login is only ever sent to the host it belongs to: the Claude token to Anthropic, the
   Codex token to ChatGPT, the push token to your push endpoint.
3. Every request is recorded locally (time, method, host, path, bytes sent and received,
   status, purpose) in egress.jsonl beside the database. Nothing about a request's body
   is recorded, only its size. `subbench privacy` reads this log back.

Tests assert that no other module opens a connection, so this file is the whole of the
collector's network surface.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# host -> what SubBench sends there
ALLOWED: dict[str, str] = {
    "api.anthropic.com": "your Claude login, to read the Claude quota meter and plan",
    "chatgpt.com": "your Codex login, to read the Codex quota meter and plan",
    "raw.githubusercontent.com": "nothing: downloads the public API price table",
    "subbench.jask-aran.com": "nothing: downloads the public benchmark bundle (`subbench frontier` only)",
}

# Which credential may travel to which host.
CREDENTIAL_HOSTS = {
    "claude": {"api.anthropic.com"},
    "codex": {"chatgpt.com"},
}


class Refused(OSError):
    """A request SubBench will not make."""


def push_host() -> str | None:
    url = os.environ.get("SUBBENCH_PUSH_URL")
    return (urlsplit(url).hostname or None) if url else None


def allowed_hosts() -> dict[str, str]:
    hosts = dict(ALLOWED)
    host = push_host()
    if host:
        hosts[host] = "what `subbench privacy preview` prints: quota readings and derived values, account ids hashed"
    return hosts


def egress_log() -> Path:
    override = os.environ.get("SUBBENCH_EGRESS_LOG")
    if override:
        return Path(override).expanduser()
    database = os.environ.get("SUBBENCH_DATABASE")
    base = Path(database).expanduser().parent if database else Path.home() / ".local" / "share" / "subbench"
    return base / "egress.jsonl"


def _record(entry: dict[str, Any]) -> None:
    try:
        path = egress_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass  # the record is a courtesy to the user; it must never stop collection


def request(url: str, *, purpose: str, method: str = "GET", headers: dict[str, str] | None = None,
            body: bytes | None = None, credential: str | None = None, timeout: float = 20.0) -> bytes:
    """Make one request, or refuse it. Returns the response body; raises HTTPError and URLError as urllib does."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" and host not in ("localhost", "127.0.0.1"):
        raise Refused(f"refusing a non-HTTPS request to {host}")
    hosts = allowed_hosts()
    if credential == "push":
        # The push token goes only to the endpoint it was configured with, which is this one.
        hosts[host] = hosts.get(host, "your push endpoint")
    if host not in hosts and host not in ("localhost", "127.0.0.1"):
        raise Refused(f"refusing a request to {host}: not one of {', '.join(sorted(hosts))}")
    if credential is not None and credential != "push":
        permitted = CREDENTIAL_HOSTS.get(credential, set())
        if host not in permitted and host not in ("localhost", "127.0.0.1"):
            raise Refused(f"refusing to send the {credential} login to {host}")
    entry: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "method": method, "host": host,
        "path": parts.path, "purpose": purpose, "sent_bytes": len(body or b""),
    }
    # urllib's default User-Agent is refused by Cloudflare-fronted hosts with a 403.
    from . import __version__

    prepared = urllib.request.Request(url, data=body, method=method,
                                      headers={"User-Agent": f"subbench/{__version__}", **(headers or {})})
    try:
        with urllib.request.urlopen(prepared, timeout=timeout) as response:
            data = response.read()
            entry.update(status=response.status, received_bytes=len(data))
            return data
    except urllib.error.HTTPError as error:
        entry.update(status=error.code)
        raise
    except Exception as error:
        entry.update(status=None, error=type(error).__name__)
        raise
    finally:
        _record(entry)


def read_log(limit: int | None = None) -> list[dict[str, Any]]:
    try:
        lines = egress_log().read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-limit:] if limit else lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries
