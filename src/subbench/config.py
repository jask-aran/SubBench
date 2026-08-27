"""The one configuration file, read by the command line as well as the service.

The service already loads it, through systemd's EnvironmentFile. Without this the command
line did not, so `subbench push` failed with "set SUBBENCH_PUSH_URL and SUBBENCH_PUSH_TOKEN"
on a machine that was pushing successfully every half hour. One file, both readers.

Real environment variables always win, so a value exported in a shell or set in the unit
overrides the file and nothing here can surprise a caller who set something deliberately.
"""
from __future__ import annotations

import os
from pathlib import Path

CONFIG_ENV = "SUBBENCH_CONFIG"


def config_path() -> Path:
    """Where the configuration lives, honouring XDG and an explicit override.

    The name is push.env for history: it held only the push endpoint and token at first,
    and the installed systemd units name it. It now holds anything the collector reads.
    """
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base).expanduser() / "subbench" / "push.env"


def parse(text: str) -> dict[str, str]:
    """KEY=VALUE lines, as systemd's EnvironmentFile reads them.

    Comments and blank lines are skipped, surrounding quotes are dropped, and a line
    without "=" is ignored rather than treated as an error: this file is edited by hand
    and a stray line must not stop collection.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path: Path | None = None) -> dict[str, str]:
    """Fill in any variable the environment does not already define. Returns what it set."""
    target = path or config_path()
    try:
        text = target.read_text()
    except OSError:
        return {}
    applied = {}
    for key, value in parse(text).items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied
