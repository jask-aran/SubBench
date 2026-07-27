from __future__ import annotations

from unittest.mock import patch

from subbench.watcher import WatchTarget, ccusage_command, watch


def test_ccusage_command() -> None:
    assert ccusage_command(
        runner="npx", provider="codex", report="daily"
    ) == ["npx", "--yes", "ccusage@latest", "codex", "daily", "--json"]


def test_watch_once_collects_all_targets() -> None:
    emitted: list[str] = []
    targets = [WatchTarget("claude"), WatchTarget("codex")]

    with patch(
        "subbench.watcher.collect_target",
        side_effect=[(True, "claude ok"), (True, "codex ok")],
    ) as collect:
        result = watch(
            object(),
            targets=targets,
            runner="npx",
            interval_seconds=60,
            once=True,
            emit=emitted.append,
        )

    assert result == 0
    assert collect.call_count == 2
    assert emitted[0].endswith("claude ok")
    assert emitted[1].endswith("codex ok")


def test_watch_once_reports_failure() -> None:
    with patch(
        "subbench.watcher.collect_target",
        return_value=(False, "failed"),
    ):
        result = watch(
            object(),
            targets=[WatchTarget("claude")],
            runner="npx",
            interval_seconds=60,
            once=True,
            emit=lambda _: None,
        )

    assert result == 1
