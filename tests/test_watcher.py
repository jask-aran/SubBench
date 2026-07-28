from __future__ import annotations

from unittest.mock import patch

from subbench.watcher import WatchTarget, ccusage_command, collect_target, watch


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


def test_collect_target_fails_diagnostic_run_when_entitlement_is_unavailable() -> None:
    completed = type("Completed", (), {"returncode": 0, "stdout": b"{}", "stderr": b""})()
    with (
        patch("subbench.watcher.subprocess.run", return_value=completed),
        patch("subbench.watcher.normalise_payload", return_value=[]),
        patch("subbench.watcher.save_import", return_value=(1, 0, True)),
        patch("subbench.watcher.collect_entitlements", side_effect=RuntimeError("authentication failed")),
    ):
        ok, message = collect_target(object(), target=WatchTarget("codex"), runner="npx")

    assert not ok
    assert "entitlement unavailable: authentication failed" in message
