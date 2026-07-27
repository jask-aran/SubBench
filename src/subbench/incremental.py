from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FileStamp:
    size: int
    mtime_ns: int


class LogChangeDetector:
    """Detect appended or replaced local agent logs without parsing their schemas."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self._state: dict[Path, FileStamp] = {}
        self._initialised = False

    def scan(self) -> bool:
        current: dict[Path, FileStamp] = {}
        for path in discover_logs(self.provider):
            try:
                stat = path.stat()
            except OSError:
                continue
            current[path] = FileStamp(size=stat.st_size, mtime_ns=stat.st_mtime_ns)

        if not self._initialised:
            self._state = current
            self._initialised = True
            return True  # establish a reconciled baseline on startup

        changed = current.keys() != self._state.keys()
        if not changed:
            changed = any(current[path] != self._state[path] for path in current)
        self._state = current
        return changed


def discover_logs(provider: str) -> Iterable[Path]:
    if provider == "codex":
        homes = os.environ.get("CODEX_HOME", str(Path.home() / ".codex")).split(",")
        roots: list[Path] = []
        for raw in homes:
            home = Path(raw).expanduser()
            roots.extend([home / "sessions", home / "archived_sessions"])
        patterns = ("*.jsonl",)
    elif provider == "claude":
        roots = [Path.home() / ".claude" / "projects", Path.home() / ".config" / "claude" / "projects"]
        patterns = ("*.jsonl",)
    else:
        raise ValueError(f"unsupported provider: {provider}")

    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            for path in root.rglob(pattern):
                if path.is_file() and path not in seen:
                    seen.add(path)
                    yield path
