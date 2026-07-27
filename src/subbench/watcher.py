from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence

from .ccusage import CcusageSchemaError, normalise_payload
from .store import save_import


@dataclass(frozen=True)
class WatchTarget:
    provider: str
    report: str = "daily"


def ccusage_command(*, runner: str, provider: str, report: str) -> list[str]:
    if runner == "npx":
        prefix = ["npx", "--yes", "ccusage@latest"]
    elif runner == "bunx":
        prefix = ["bunx", "ccusage"]
    elif runner == "pnpm":
        prefix = ["pnpm", "dlx", "ccusage"]
    else:
        raise ValueError(f"unsupported runner: {runner}")
    return [*prefix, provider, report, "--json"]


def collect_target(db, *, target: WatchTarget, runner: str) -> tuple[bool, str]:
    command = ccusage_command(
        runner=runner,
        provider=target.provider,
        report=target.report,
    )
    result = subprocess.run(command, check=False, capture_output=True)
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        return False, error or f"ccusage exited with status {result.returncode}"

    try:
        payload = json.loads(result.stdout)
        rows = normalise_payload(
            payload,
            provider=target.provider,
            report=target.report,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, CcusageSchemaError, ValueError) as error:
        return False, str(error)

    import_id, row_count, created = save_import(
        db,
        raw=result.stdout,
        payload=payload,
        rows=rows,
        provider=target.provider,
        report=target.report,
        command=" ".join(command),
    )
    state = "recorded" if created else "unchanged"
    return True, f"{target.provider}: {state} import {import_id} ({row_count} rows)"


def watch(
    db,
    *,
    targets: Sequence[WatchTarget],
    runner: str,
    interval_seconds: float,
    once: bool = False,
    emit: Callable[[str], None] = print,
) -> int:
    if interval_seconds <= 0:
        raise ValueError("interval must be greater than zero")

    while True:
        failed = False
        timestamp = datetime.now(timezone.utc).isoformat()
        for target in targets:
            ok, message = collect_target(db, target=target, runner=runner)
            emit(f"{timestamp} {message}")
            failed = failed or not ok

        if once:
            return 1 if failed else 0

        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            return 0
