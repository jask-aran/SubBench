#!/usr/bin/env python3
"""Print the Claude subscription usage response as JSON, for SUBBENCH_CLAUDE_USAGE_COMMAND.

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

    sys.stdout.write(body if body.endswith("\n") else body + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
